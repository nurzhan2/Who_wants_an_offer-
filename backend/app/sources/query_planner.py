"""Turning a candidate profile into the handful of searches a run can afford.

The naive plan is every skill crossed with every region. A twenty-skill,
three-region profile makes sixty queries per source, several hundred once each
is paginated — and the results overlap almost completely, because "python",
"django", "fastapi" and "celery" return nearly the same postings in the same
city. The cost of that plan is real (rate limits, daily quotas, minutes) and
the coverage it buys over a small one is close to zero.

So the plan is built from a limited set of keyword GROUPS rather than from
skills. A skill's group is data: every entry in ``skills_min.yaml`` carries
one, and the planner reads it through the canonicaliser instead of inventing a
clustering of its own. The strongest five to eight groups describe the core of
the stack in the words a vacancy is actually titled with.

Three decisions here are load-bearing.

*The cap is enforced by truncation.* ``max_queries_per_run`` cuts the plan and
the cut queries are not in the returned object at all, so no caller can
execute them. A plan that is merely advised to be small is a plan that grows.

*The cross product is walked diagonally*, ordered by the sum of the group's
rank and the placement's rank, so truncation eats into both dimensions at
once. A budget spent entirely on the top placement returns eight overlapping
views of one city; spent entirely on the top group it misses every other
market the candidate said they would work in.

*No salary, country or language filter is ever set.* Each would narrow the
search on data this module cannot read correctly, and each is skipped at the
point where the reason can be stated.

This module lives in ``app/sources/`` but is not a connector. The registry's
discovery imports every module in the package, this one included; importing it
registers nothing.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.core.logging import get_logger
from app.db.enums import RemoteType, SkillEvidence
from app.resume.enricher import LEVEL_ORDER, slugify
from app.resume.skills import SkillCanonicalizer, default_canonicalizer
from app.schemas.profile import CandidateProfileRead, SkillRead
from app.schemas.vacancy import MAX_POSTED_WITHIN_DAYS
from app.sources.base import SearchQuery

logger = get_logger(__name__)

#: Widest fan-out over places one profile may produce. A placement multiplies
#: the whole keyword set, and a resume distinguishes only three of them: where
#: the candidate lives, remote, and anywhere they would move to. A fourth is a
#: neighbouring city, whose feed the first three have already returned.
MAX_PLACEMENTS = 3

#: Bounds on how many keyword groups one plan draws from. Below five the plan
#: describes a corner of the stack rather than the stack; above eight the
#: groups being added are the ones the candidate is weakest in, and their
#: postings are the ones already returned by the groups above them.
MIN_KEYWORD_GROUPS = 5
MAX_KEYWORD_GROUPS = 8

#: Terms taken from one group, strongest first. Past the third the terms are
#: ones the candidate barely claims, and they dilute the query rather than
#: widen it.
MAX_KEYWORDS_PER_GROUP = 3

#: How well a group names the job rather than describes it. Vacancies are
#: titled after a language or a layer, mention their databases and
#: infrastructure in the body, and never advertise a tool or a working practice
#: as the role. Keyed by the dictionary's own group names; a group a later
#: dictionary adds scores DEFAULT_GROUP_SEARCH_VALUE, so it is usable the day
#: it lands instead of invisible until this table is edited.
GROUP_SEARCH_VALUE: dict[str, int] = {
    "language": 3,
    "backend": 3,
    "frontend": 3,
    "ml": 3,
    "data": 3,
    "database": 2,
    "devops": 2,
    "cloud": 2,
    "queue": 2,
    "testing": 2,
    "ci": 2,
    "tool": 1,
    "practice": 1,
    "os": 1,
}
DEFAULT_GROUP_SEARCH_VALUE = 2

#: The programming language gets a place in every plan, ahead of the ranking.
#:
#: Not a preference — a measurement. Across 36 postings captured from arbeitnow
#: and remotive, "python" appeared in 9 bodies while "django", "fastapi" and
#: "sqlalchemy" appeared in none at all. The score below rewards a group for
#: holding several strong skills, and a candidate has one primary language and
#: several frameworks, so the language group can never win on size however
#: central it is. Left to the ranking, a Python engineer's plan searched for
#: three terms that occur in zero real postings and never for the one that
#: occurs in a quarter of them.
GUARANTEED_GROUP = "language"

#: What ``slugify`` returns for a name it cannot slug, which is the fate of
#: every Cyrillic-only skill. Derived from the function that produces it so the
#: two cannot drift apart. Never used as a keyword: it matches nothing.
UNKNOWN_SLUG = slugify("")

#: Labels for the two fallback keyword sets, prefixed so neither can collide
#: with a group name coming out of the dictionary.
FALLBACK_SKILLS_LABEL = "fallback:skills"
FALLBACK_HEADLINE_LABEL = "fallback:headline"
#: Label of a group made of one job title the owner typed. Every title shares
#: it: a label is safe to log, and the titles themselves are the owner's words.
TARGET_TITLE_LABEL = "target:title"

#: Mirrors ``SearchQuery.headline``.
MAX_INTENT_CHARS = 300
#: Between two titles in the intent line. A slash, because hh's ranking reads
#: words and a slash is not one.
INTENT_SEPARATOR = " / "

#: Mirrors ``SearchQuery.area``. Anything longer than the field accepts is a
#: sentence rather than a place, and truncating it would search for half a
#: sentence, so such a location is skipped instead.
MAX_AREA_CHARS = 100

#: A headline is a job title. Anything longer is a summary paragraph, and as a
#: search term it matches nothing.
MAX_HEADLINE_CHARS = 100

#: Identity of a plan entry: the keywords as an unordered set, then every other
#: field as it stands.
type PlanKey = tuple[frozenset[str], tuple[tuple[str, Any], ...]]


@dataclass(frozen=True, slots=True)
class Placement:
    """One place to look: a named area, a remote filter, or neither.

    ``country`` is deliberately absent. The profile stores locations as free
    text a person typed, and turning that into an ISO 3166 code needs a
    gazetteer this module has no business embedding — guessing it wrong
    narrows the search to nothing, silently. A connector knows the country it
    serves.
    """

    area: str | None = None
    remote: RemoteType | None = None


@dataclass(frozen=True, slots=True)
class KeywordGroup:
    """The terms one group contributes to the plan, strongest first."""

    label: str
    keywords: tuple[str, ...]


class QueryPlan(BaseModel):
    """The searches a run may issue for one profile, and what was cut.

    Frozen, because a plan that can be appended to after the cap was applied is
    not a cap.
    """

    model_config = ConfigDict(frozen=True)

    queries: tuple[SearchQuery, ...] = ()
    #: Keyword groups the surviving queries draw from, in plan order. Group
    #: names come from the dictionary, so they are safe to log; the keywords
    #: themselves are resume content and are not.
    groups: tuple[str, ...] = ()
    #: How many places the plan looks in.
    placements: int = Field(default=0, ge=0)
    #: Duplicate entries collapsed before the cap was applied.
    collapsed: int = Field(default=0, ge=0)
    #: How many queries the cap cut. A count, deliberately not the queries:
    #: carrying the objects would put an executable plan back within reach of
    #: any caller that iterates the whole model, which is the one thing the cap
    #: exists to prevent.
    dropped: int = Field(default=0, ge=0)
    #: The cap that applied, copied in so a report can quote it without
    #: reaching for the settings.
    limit: int = Field(default=0, ge=0)

    @property
    def is_truncated(self) -> bool:
        """Whether the profile wanted more searches than the run may issue."""
        return self.dropped > 0

    @property
    def summary(self) -> str:
        """One line for the run report, in the language the dashboard speaks."""
        parts = [f"Запросов в плане: {len(self.queries)}"]
        if self.groups:
            parts.append(f"групп ключевых слов: {len(self.groups)}")
        parts.append(f"мест поиска: {self.placements}")
        if self.collapsed:
            parts.append(f"дублей свёрнуто: {self.collapsed}")
        if self.dropped:
            parts.append(f"отброшено сверх лимита {self.limit}: {self.dropped}")
        return ", ".join(parts) + "."


def _rank_key(skill: SkillRead) -> tuple[int, Decimal, int, str]:
    """Sort key putting the strongest, best-evidenced skill first.

    ``SkillLevel`` is a StrEnum, so sorting on the member itself orders the
    levels alphabetically — basic, expert, strong, working — and quietly
    promotes the weakest skills into the plan. ``LEVEL_ORDER`` is the
    proficiency order; the name breaks the remaining ties so two runs over the
    same profile produce the same plan.
    """
    corroborated = 1 if skill.evidence is SkillEvidence.CORROBORATED else 0
    return (
        -LEVEL_ORDER.index(skill.level),
        -(skill.years or Decimal(0)),
        -corroborated,
        skill.canonical_name,
    )


def _group_score(group: str, skills: Sequence[SkillRead]) -> Decimal:
    """How much of the candidate's core one group is.

    Only the terms that would actually be used count, so a group holding
    sixteen half-remembered languages does not outrank the group holding the
    two the candidate is expert in. Years break ties without being able to
    overturn a level, and the group's own search value scales the result: an
    expert-level practice is still not what a vacancy is titled after.
    """
    top = skills[:MAX_KEYWORDS_PER_GROUP]
    strength = sum(LEVEL_ORDER.index(skill.level) + 1 for skill in top)
    years = sum((skill.years or Decimal(0) for skill in top), Decimal(0))
    value = GROUP_SEARCH_VALUE.get(group, DEFAULT_GROUP_SEARCH_VALUE)
    return Decimal(value * strength) + years / 10


def keyword_groups_for(
    profile: CandidateProfileRead,
    *,
    canonicalizer: SkillCanonicalizer | None = None,
    limit: int = MAX_KEYWORD_GROUPS,
) -> tuple[KeywordGroup, ...]:
    """The groups this profile searches under, strongest first.

    Two things the enricher leaves behind have to be handled here. A skill the
    dictionary did not recognise carries a slug of its original spelling and
    has no group, so it cannot form one; and a Cyrillic-only name slugs to the
    literal ``unknown``, which is not a word any posting contains. Both are
    kept out of the groups, and the unrecognised ones become the first fallback
    rather than being lost — a dictionary of a hundred entries misses real
    technologies, and a profile made entirely of them still deserves a search.
    """
    resolver = canonicalizer or default_canonicalizer()
    grouped: dict[str, list[SkillRead]] = {}
    ungrouped: list[SkillRead] = []

    for skill in profile.skills:
        name = skill.canonical_name.strip()
        if not name or name == UNKNOWN_SLUG:
            continue
        # group_of takes what canonicalize produced, which is exactly what the
        # profile stores; it answers None for a slug it never issued.
        group = resolver.group_of(name)
        if group is None:
            ungrouped.append(skill)
            continue
        grouped.setdefault(group, []).append(skill)

    if grouped:
        for skills in grouped.values():
            skills.sort(key=_rank_key)
        ranked = sorted(
            grouped.items(),
            # The guaranteed group sorts first; everything else by strength.
            # See GUARANTEED_GROUP for the measurement behind the exception.
            key=lambda item: (item[0] != GUARANTEED_GROUP, -_group_score(*item), item[0]),
        )
        return tuple(
            KeywordGroup(
                label=group,
                keywords=tuple(skill.canonical_name for skill in skills[:MAX_KEYWORDS_PER_GROUP]),
            )
            for group, skills in ranked[:limit]
        )

    if ungrouped:
        ungrouped.sort(key=_rank_key)
        return (
            KeywordGroup(
                label=FALLBACK_SKILLS_LABEL,
                keywords=tuple(
                    skill.canonical_name for skill in ungrouped[:MAX_KEYWORDS_PER_GROUP]
                ),
            ),
        )

    headline = " ".join((profile.headline or "").split())
    if headline and len(headline) <= MAX_HEADLINE_CHARS:
        # Last resort, and the best single term there is when it exists: a
        # headline is the title of the job the candidate is applying for.
        return (KeywordGroup(label=FALLBACK_HEADLINE_LABEL, keywords=(headline,)),)
    return ()


def title_groups_for(profile: CandidateProfileRead) -> tuple[KeywordGroup, ...]:
    """One group per job title the owner typed, in their order.

    A title is ONE keyword, not its words: «Python Developer» sent to a search
    API is the phrase the owner means, and split into ``python`` and
    ``developer`` the second word alone matches every posting in a feed. The
    sources that filter locally match a phrase by all of its words; see
    ``app.sources.base.mentions``.
    """
    return tuple(
        KeywordGroup(label=TARGET_TITLE_LABEL, keywords=(title,))
        for title in profile.target_titles
        if title.strip()
    )


def intent_for(profile: CandidateProfileRead) -> str | None:
    """What the owner is looking for, as the one line a source ranks by.

    The typed titles come first and the resume headline after them, so the
    titles weigh at least as much as the headline everywhere the line is read —
    hh's slug ranking reads every word of it with the same top weight. Without
    titles this is the headline alone, which is the previous behaviour.
    """
    parts = [*profile.target_titles]
    headline = " ".join((profile.headline or "").split())
    if headline and headline.casefold() not in {part.casefold() for part in parts}:
        parts.append(headline)
    line = ""
    for part in parts:
        candidate = f"{line}{INTENT_SEPARATOR}{part}" if line else part
        if len(candidate) > MAX_INTENT_CHARS:
            # Cut at a whole title: half a title is a different job.
            break
        line = candidate
    return line or None


def placements_for(profile: CandidateProfileRead) -> tuple[Placement, ...]:
    """Where to look, most promising first, at most :data:`MAX_PLACEMENTS`.

    A fully remote candidate is not bound to a city, so the remote slot leads
    and the cities follow it; anyone else is searched where they live first.
    Relocation adds one unconstrained slot at the end, which lets each source
    search the whole region it covers.

    City slots carry no remote filter. The city already constrains the search,
    and a format filter on top of it drops every posting whose source did not
    label its format, which is most of them.
    """
    ordered: list[Placement] = []
    if profile.remote_pref is RemoteType.FULL:
        ordered.append(Placement(remote=RemoteType.FULL))
    for location in profile.locations:
        area = " ".join(location.split())
        if not area or len(area) > MAX_AREA_CHARS:
            continue
        ordered.append(Placement(area=area))
    if profile.relocation:
        ordered.append(Placement())
    if not ordered:
        # Nothing usable was said about where. One unconstrained slot lets each
        # source search the region it serves, which beats planning nothing.
        ordered.append(Placement())
    return tuple(dict.fromkeys(ordered))[:MAX_PLACEMENTS]


def _query(
    group: KeywordGroup,
    placement: Placement,
    *,
    posted_within_days: int,
    headline: str | None = None,
) -> SearchQuery:
    """One search: a group's terms, in one place, under the candidate's own title.

    The headline is the same on every query of a plan, which is the point: the
    groups say what this profile knows and differ from each other, and the
    headline says what it is looking for and does not. A source with something
    to rank uses the difference; see ``SearchQuery.headline``.

    ``salary_min``, ``country`` and ``language`` are left unset on purpose.
    ``SearchQuery`` carries no currency, so a figure from a profile quoted in
    tenge would be read as whatever currency the source works in and filter out
    everything the candidate could actually take. The country cannot be derived
    from free-text locations, and the profile's languages are an input to
    matching rather than a filter: picking one of them here hides every posting
    written in the others.
    """
    return SearchQuery(
        keywords=group.keywords,
        headline=headline,
        area=placement.area,
        remote=placement.remote,
        posted_within_days=posted_within_days,
    )


def _plan_key(query: SearchQuery) -> PlanKey:
    """Identity of a plan entry, insensitive to the order of its keywords.

    Order is significant to a source — it ranks by relevance, and relevance is
    not symmetric in the order of the terms — but two queries built from the
    same words in a different order are one entry in a plan, and issuing both
    fetches the same postings twice. Everything except the keywords is taken
    from the model rather than listed here, so a field added to ``SearchQuery``
    later cannot silently stop distinguishing two queries.
    """
    # Any: the remaining fields are heterogeneous (Decimal, enums, ints, str)
    # and this key only ever compares them for equality.
    rest: dict[str, Any] = query.model_dump(exclude={"keywords"})
    return (
        frozenset(word.casefold() for word in query.keywords),
        tuple(sorted(rest.items())),
    )


def plan_queries(
    profile: CandidateProfileRead,
    *,
    canonicalizer: SkillCanonicalizer | None = None,
) -> QueryPlan:
    """Build the searches one run may issue for this profile.

    Pure and synchronous: no session, no request, no clock. The settings are
    read here rather than at import time, so a changed cap applies to the next
    call instead of to the next process.
    """
    budget = settings.max_queries_per_run
    # Clamped because the pipeline's own age limit is unbounded above while the
    # query field is not, and a validation error inside a pure planner would be
    # a configuration typo surfacing as a crash three layers away.
    posted_within_days = min(settings.max_vacancy_age_days, MAX_POSTED_WITHIN_DAYS)

    placements = placements_for(profile)
    # The ideal plan is sized to the budget before it is cut to it: a run that
    # can afford more queries should buy wider keyword coverage, not a deeper
    # pagination of the same one. The bounds keep it recognisable as a plan
    # either way.
    wanted = min(MAX_KEYWORD_GROUPS, max(MIN_KEYWORD_GROUPS, math.ceil(budget / len(placements))))
    # The owner's titles replace the skill groups rather than joining them.
    # Joined, the skills would still put Go and PHP in the plan beside «Python
    # Developer», which is the behaviour the titles exist to end. No titles is
    # the previous plan, unchanged.
    groups = title_groups_for(profile) or keyword_groups_for(
        profile, canonicalizer=canonicalizer, limit=wanted
    )
    intent = intent_for(profile)

    if not groups:
        # Every skill missing from the dictionary and no usable headline. A
        # keyword-less plan is not a search, it is a full download of every
        # feed, so nothing is planned and the run reports why.
        logger.warning(
            "query_planner.no_keywords",
            profile_id=str(profile.id),
            skills=len(profile.skills),
        )
        return QueryPlan(placements=len(placements), limit=budget)

    # Diagonal walk of groups x placements: entries whose two ranks sum to the
    # same number are equally good, and the sum grows one step at a time, so
    # truncation removes the weakest of both dimensions rather than all of one.
    ordered: list[tuple[str, SearchQuery]] = []
    for rank in range(len(groups) + len(placements) - 1):
        for group_index, group in enumerate(groups):
            placement_index = rank - group_index
            if 0 <= placement_index < len(placements):
                query = _query(
                    group,
                    placements[placement_index],
                    posted_within_days=posted_within_days,
                    headline=intent,
                )
                ordered.append((group.label, query))

    # Deduplicate before truncating, never after: a duplicate that survives to
    # the cap spends budget a distinct query would have used.
    unique: dict[PlanKey, tuple[str, SearchQuery]] = {}
    for label, query in ordered:
        unique.setdefault(_plan_key(query), (label, query))
    collapsed = len(ordered) - len(unique)

    entries = list(unique.values())[:budget]
    dropped = len(unique) - len(entries)
    if dropped:
        logger.warning(
            "query_planner.truncated",
            profile_id=str(profile.id),
            kept=len(entries),
            dropped=dropped,
            limit=budget,
            groups=len(groups),
            placements=len(placements),
        )
    else:
        logger.debug(
            "query_planner.planned",
            profile_id=str(profile.id),
            queries=len(entries),
            groups=len(groups),
            placements=len(placements),
        )

    return QueryPlan(
        queries=tuple(query for _, query in entries),
        groups=tuple(dict.fromkeys(label for label, _ in entries)),
        placements=len(placements),
        collapsed=collapsed,
        dropped=dropped,
        limit=budget,
    )
