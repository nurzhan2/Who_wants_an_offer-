"""The vacancies screen: the ranked list, and one vacancy explained.

The list is a thin pass-through to the repository — filters in, a keyset page
out. The card is where the work is, and all of it is in one question:

    a requirement this candidate does not "have" — do they not have it, or does
    this CV not say so?

The scorer cannot tell the difference and does not try to. It compares a
vacancy's requirement list against ``profile_skill``, and anything not there
counts as missing, because a score has to be computed from something and that
is the only something there is. But the two cases lead to opposite actions.
"Postgres is not in this resume" is a line to add before applying. "I have never
written Go" is a job to skip. A screen that showed both as *missing* would hide
the difference between an afternoon's editing and a career change, on the one
screen whose whole job is to decide where to spend an afternoon.

So the middle case is separated here, from evidence and never from a guess. Two
kinds of evidence exist in this database and both are used:

* another resume of the same owner lists the skill — an earlier CV is a claim
  the person made about themselves, in writing;
* the words are in *this* resume's text and extraction did not turn them into a
  skill row — the CV does say so, and the pipeline missed it.

Anything else is reported as absent, with no evidence, which is the honest
reading. This module never infers a skill from a related one: the scorer already
credits related technologies with partial coverage, and doing it twice — once as
a number, once as a claim on the screen — would let a candidate read "you have
Kafka" off a row that says "you have RabbitMQ".
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConfigurationError
from app.core.logging import get_logger
from app.db.models import CandidateProfile, Match, VacancySource
from app.db.repositories.application import ApplicationRepository
from app.db.repositories.match import MatchRepository
from app.db.repositories.profile import ProfileRepository
from app.db.repositories.vacancy import VacancyRepository
from app.db.seed_rows import is_seed_url, payload_size
from app.resume.skills import SkillCanonicalizer, default_canonicalizer
from app.schemas.common import CursorPage
from app.schemas.dashboard import (
    MatchSummary,
    RequirementBreakdown,
    RequirementStanding,
    SkillElsewhere,
    VacancyCard,
)
from app.schemas.match import MatchComponentScores, MatchedSkill, MissingSkill
from app.schemas.vacancy import VacancyFilter, VacancyListItem, VacancyRead, VacancySourceRead
from app.sources.registry import get_source

logger = get_logger(__name__)

#: Where the evidence for an uncovered requirement came from. Strings rather
#: than an enum in the schema because they are read by a template and never
#: compared against a database column; they are constants here so that the
#: service and its tests cannot spell them differently.
FROM_OTHER_PROFILE = "other_profile"
FROM_RESUME_TEXT = "resume_text"

#: What counts as one word of a resume when hunting for a skill name. Keeps the
#: characters that are *part of* the names — ``c++``, ``c#``, ``node.js``,
#: ``asp.net`` — and cuts on everything else, because the dictionary folds
#: separators away and a token split on ``.`` would never match ``node.js``.
_WORD = re.compile(r"[0-9A-Za-zЀ-ӿ+#._-]+")

#: How many words a skill's name may span. Two covers what the dictionary
#: actually holds ("apache kafka", "github actions"); three is where the cost
#: starts being paid on every noun phrase in a resume for no further hits.
_MAX_WORDS = 2

#: A fragment of the resume shown as evidence, in characters either side of the
#: name found. Enough to recognise the sentence; short enough that the card
#: cannot become a way to read somebody's whole CV out of a vacancy endpoint.
_CONTEXT = 60


async def list_page(
    session: AsyncSession,
    filters: VacancyFilter,
    *,
    cursor: str | None = None,
    limit: int = 50,
    with_total: bool = False,
    with_facets: bool = False,
) -> CursorPage[VacancyListItem]:
    """One page of the ranked list, for the active profile.

    The profile is resolved here rather than taken from the caller: there is one
    active resume, the score means nothing without it, and a list endpoint that
    accepted a profile id would be a list endpoint that silently returns
    unscored rows when somebody passes the wrong one.
    """
    profile = await ProfileRepository(session).get_active()
    return await VacancyRepository(session).list_filtered(
        filters,
        profile_id=profile.id if profile is not None else None,
        cursor=cursor,
        limit=limit,
        with_total=with_total,
        with_facets=with_facets,
    )


async def card(session: AsyncSession, vacancy_id: UUID) -> VacancyCard | None:
    """One vacancy, its score, its requirements split three ways, its letter."""
    vacancy = await VacancyRepository(session).get(vacancy_id)
    if vacancy is None:
        return None

    profiles = ProfileRepository(session)
    profile = await profiles.get_active()
    match = (
        await MatchRepository(session).get(profile.id, vacancy_id) if profile is not None else None
    )

    requirements = RequirementBreakdown()
    if profile is not None and match is not None:
        elsewhere = await profiles.skills_elsewhere(profile.id)
        requirements = _split(
            match,
            profile=profile,
            elsewhere={row.canonical_name: row for row in elsewhere},
        )

    return VacancyCard(
        vacancy=_with_sources(VacancyRead.model_validate(vacancy), vacancy.sources),
        match=_summary(match),
        requirements=requirements,
        letter=await ApplicationRepository(session).letter_brief(vacancy_id),
    )


def _with_sources(read: VacancyRead, rows: Sequence[VacancySource]) -> VacancyRead:
    """The card's source list, fullest first, each with its publisher and marks.

    The order is the one the list uses (``app.db.seed_rows.fullest_first``),
    computed here in Python over rows already loaded: real rows before seed
    rows, then the larger payload, then the older row.
    """
    ordered = sorted(
        rows,
        key=lambda row: (
            is_seed_url(row.url),
            -payload_size(row.raw),
            row.created_at,
            str(row.id),
        ),
    )
    sources = [
        VacancySourceRead.model_validate(row).model_copy(
            update={
                "publisher": _publisher(row),
                "is_primary": index == 0,
                "is_seed": is_seed_url(row.url),
            }
        )
        for index, row in enumerate(ordered)
    ]
    return read.model_copy(update={"sources": sources})


def _publisher(row: VacancySource) -> str | None:
    """Ask the row's own connector who published it; None for a slug nobody registers.

    ``telegram`` is such a slug: seed rows carry it and no connector does.
    """
    try:
        source = get_source(row.source_slug)
    except ConfigurationError:
        return None
    return source.publisher_of(row.raw)


def _summary(match: Match | None) -> MatchSummary | None:
    """The score and the reasoning behind it, or None when nothing scored this."""
    if match is None:
        return None
    return MatchSummary(
        score=match.score,
        rule_score=match.rule_score,
        semantic_score=match.semantic_score,
        llm_score=match.llm_score,
        bucket=match.bucket,
        components=MatchComponentScores.model_validate(match.component_scores),
        red_flags=[str(flag) for flag in match.red_flags],
        experience_gap_years=match.experience_gap_years,
        verdict=match.verdict,
        application_angle=match.application_angle,
        scored_at=match.scored_at,
    )


@dataclass(frozen=True, slots=True)
class _Mention:
    """A skill name found in the text of a resume, with where it was found."""

    canonical_name: str
    fragment: str


def _split(
    match: Match,
    *,
    profile: CandidateProfile,
    elsewhere: dict[str, SkillElsewhere],
) -> RequirementBreakdown:
    """Covered, not-in-this-CV, absent — from the match row and the evidence.

    The covered list is the scorer's own ``matched_skills`` and is not
    recomputed. Two implementations of "does this candidate cover this" would
    drift, and the one on the screen would be the one nobody tests.
    """
    spellings = {
        skill.canonical_name: _spelling(skill.raw_names, skill.canonical_name)
        for skill in profile.skills
    }
    mentions = {
        mention.canonical_name: mention
        for mention in _mentioned(profile.raw_text)
        if mention.canonical_name not in spellings
    }

    covered = [
        RequirementStanding(
            canonical_name=matched.canonical_name,
            is_required=matched.is_required,
            coverage=matched.coverage,
            spelling=spellings.get(matched.canonical_name),
            source=matched.source,
        )
        for matched in _matched(match)
    ]

    not_in_cv: list[RequirementStanding] = []
    absent: list[RequirementStanding] = []
    for missing, required in _missing(match):
        standing = RequirementStanding(
            canonical_name=missing.canonical_name,
            is_required=required,
            weight=missing.weight,
            source=missing.source,
        )
        other = elsewhere.get(missing.canonical_name)
        mention = mentions.get(missing.canonical_name)
        if other is not None:
            not_in_cv.append(
                standing.model_copy(
                    update={
                        "evidence": FROM_OTHER_PROFILE,
                        "evidence_detail": _names(other),
                    }
                )
            )
        elif mention is not None:
            not_in_cv.append(
                standing.model_copy(
                    update={
                        "evidence": FROM_RESUME_TEXT,
                        "evidence_detail": mention.fragment,
                    }
                )
            )
        else:
            absent.append(standing)

    return RequirementBreakdown(covered=covered, not_in_this_cv=not_in_cv, absent=absent)


def _names(other: SkillElsewhere) -> str | None:
    """Which resume made the claim: its filename, failing that its owner's name."""
    if other.resume_filename and other.resume_filename.strip():
        return other.resume_filename.strip()
    return other.profile_name.strip() if other.profile_name else None


def _matched(match: Match) -> list[MatchedSkill]:
    """``matched_skills`` as models. JSONB is a promise nothing enforced."""
    return [MatchedSkill.model_validate(entry) for entry in match.matched_skills]


def _missing(match: Match) -> list[tuple[MissingSkill, bool]]:
    """Everything the vacancy wanted and the score did not credit, required first.

    The two stored lists are flattened into one with a flag rather than kept
    apart, because the split this screen cares about cuts the other way: a
    nice-to-have the candidate demonstrably has is still a line worth adding to
    a CV, and a required skill they have never touched is still the reason to
    skip. Required leads so that the reason to skip is read first.
    """
    return [
        *((MissingSkill.model_validate(entry), True) for entry in match.missing_required),
        *((MissingSkill.model_validate(entry), False) for entry in match.missing_nice),
    ]


def _spelling(raw_names: list[str], canonical: str) -> str:
    """How the resume actually wrote a skill.

    ``canonical_name`` is a lookup key — lowercase, stripped of punctuation — so
    rendering it would show "postgresql" where the person wrote "PostgreSQL".
    """
    for name in raw_names:
        if isinstance(name, str) and name.strip():
            return name.strip()
    return canonical


def _mentioned(
    raw_text: str | None, canonicalizer: SkillCanonicalizer | None = None
) -> list[_Mention]:
    """Skill names that appear in the resume's own text.

    Runs the same canonicaliser the extractor runs, over words and word pairs of
    the text layer. A hit means the CV *does* name the skill and the extraction
    step did not produce a row for it — which is the second kind of evidence the
    module docstring describes, and also a standing bug report about extraction.

    Pairs as well as single words because the dictionary holds names that are
    two words ("apache kafka"), and the words on their own are either a
    different skill or nothing at all.

    Deliberately not fuzzy. Only spellings the dictionary already knows count,
    so a resume that says "kafka-like queues" produces nothing here rather than
    a claim the person has Kafka.
    """
    if not raw_text:
        return []
    resolve = canonicalizer or default_canonicalizer()
    words = list(_WORD.finditer(raw_text))
    found: dict[str, _Mention] = {}
    for index, word in enumerate(words):
        for span in range(1, _MAX_WORDS + 1):
            last = index + span - 1
            if last >= len(words):
                break
            phrase = raw_text[word.start() : words[last].end()]
            canonical = resolve.canonicalize(phrase)
            if canonical is not None and canonical not in found:
                found[canonical] = _Mention(
                    canonical_name=canonical,
                    fragment=_fragment(raw_text, word.start(), words[last].end()),
                )
    return list(found.values())


def _fragment(text: str, start: int, end: int) -> str:
    """The words around a hit, on one line, so a person can see where it was."""
    left = max(0, start - _CONTEXT)
    right = min(len(text), end + _CONTEXT)
    return " ".join(text[left:right].split())
