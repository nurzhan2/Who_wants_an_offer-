"""The query planner: what stops one resume from spending a whole run.

Nothing here touches a database, a network or a clock. The planner is pure
arithmetic over a profile and the skill dictionary, which is the only reason a
budget decision this important can be asserted exactly rather than sampled.

Four things are defended below, because each of them fails silently.

**The cap is a cut, not a warning.** ``max_queries_per_run`` has to remove
queries from the returned object. A plan that merely reports being over budget
is executed in full by the first caller that iterates it, and the cost lands as
rate-limit bans on a source that is hard to get back.

**The cut queries are gone.** ``dropped`` is a count. If the objects were kept
anywhere on the model — a spare field, a nested structure — a caller iterating
the plan would run them, and the cap would be decoration.

**Order comes from proficiency.** ``SkillLevel`` is a ``StrEnum``, so the
obvious ``sorted(skills, key=...level)`` yields basic, expert, strong, working
and quietly promotes the weakest skills into the plan. The failure is invisible:
the run still succeeds, still returns vacancies, and matches against the wrong
half of the stack. The one documented exception to the ranking — the language
group leading every plan — is pinned by its own test, because an exception with
no test is indistinguishable from a bug next time somebody tidies the sort key.

**Keywords come from groups.** Twenty skills must not become twenty searches.
The plan is built from dictionary groups, so the searches read like job titles
instead of like a tag cloud.
"""

import inspect
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
import structlog
from pydantic import BaseModel, ValidationError

from app.core.config import Settings, settings
from app.db.enums import ParseStatus, RemoteType, Seniority, SkillEvidence, SkillLevel
from app.resume.enricher import slugify
from app.resume.skills import default_canonicalizer
from app.schemas.profile import CandidateProfileRead, SkillRead
from app.schemas.vacancy import MAX_POSTED_WITHIN_DAYS
from app.sources.base import SearchQuery
from app.sources.query_planner import (
    FALLBACK_HEADLINE_LABEL,
    FALLBACK_SKILLS_LABEL,
    GUARANTEED_GROUP,
    MAX_AREA_CHARS,
    MAX_KEYWORD_GROUPS,
    MAX_KEYWORDS_PER_GROUP,
    MAX_PLACEMENTS,
    UNKNOWN_SLUG,
    Placement,
    QueryPlan,
    keyword_groups_for,
    placements_for,
    plan_queries,
)

pytestmark = pytest.mark.unit

#: Fixed instant: nothing in the planner reads a clock, and a plan that changed
#: with the time of day would be untestable.
FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

#: The cap as shipped, read from the field rather than typed in, so raising the
#: default in configuration updates the expectation instead of failing here for
#: the wrong reason.
DEFAULT_CAP: int = Settings.model_fields["max_queries_per_run"].default

#: Latin spellings on purpose. A location is free text the planner passes
#: through untouched, and using transliterations here is one more assertion
#: that nothing in the module is special-cased to one market's alphabet.
CITIES = ("Almaty", "Astana", "Shymkent")

#: A skill named the way half this market's resumes name them. What matters is
#: that ``slugify`` keeps nothing of it, not which word it is.
CYRILLIC_SKILL = "Аналитика"


def skill(
    name: str,
    level: SkillLevel = SkillLevel.WORKING,
    years: str | None = None,
    *,
    evidence: SkillEvidence = SkillEvidence.STATED,
) -> SkillRead:
    """One skill of a profile, keyed by the canonical name the planner reads."""
    return SkillRead(
        id=uuid5(NAMESPACE_URL, f"skill:{name}"),
        canonical_name=name,
        raw_names=[name.title()],
        years=Decimal(years) if years is not None else None,
        level=level,
        evidence=evidence,
        last_used_year=2026,
    )


def candidate(
    *,
    skills: Sequence[SkillRead] = (),
    locations: Sequence[str] = (),
    relocation: bool = False,
    remote_pref: RemoteType | None = None,
    headline: str | None = None,
    salary_min: Decimal | None = None,
    salary_currency: str | None = None,
    languages: Sequence[dict[str, Any]] = (),
) -> CandidateProfileRead:
    """A profile built in memory, exactly as the API would answer one.

    Built directly rather than round-tripped through the repository: the planner
    reads a schema, not a row, and a database would only add a way for this
    file to fail for reasons that have nothing to do with planning.
    """
    return CandidateProfileRead(
        id=uuid5(NAMESPACE_URL, "profile:planner"),
        name="Nurzhan",
        headline=headline,
        seniority=Seniority.SENIOR,
        total_years=Decimal("7.0"),
        summary=None,
        locations=list(locations),
        relocation=relocation,
        remote_pref=remote_pref,
        salary_min=salary_min,
        salary_currency=salary_currency,
        languages=list(languages),
        is_active=True,
        parse_status=ParseStatus.READY,
        parse_error=None,
        parse_started_at=FIXED_TIME,
        resume_filename="cv.pdf",
        resume_size_bytes=120_000,
        resume_format="pdf",
        created_at=FIXED_TIME,
        updated_at=FIXED_TIME,
        skills=list(skills),
    )


#: Twenty skills over seven dictionary groups: the profile the naive plan turns
#: into sixty searches. Levels and years are spread so every group's rank and
#: every keyword's rank is decided rather than tied.
WIDE_SKILLS: tuple[SkillRead, ...] = (
    # language
    skill("python", SkillLevel.EXPERT, "6"),
    skill("go", SkillLevel.STRONG, "3"),
    skill("java", SkillLevel.WORKING, "2"),
    skill("typescript", SkillLevel.WORKING, "1"),
    skill("bash", SkillLevel.BASIC),
    # backend
    skill("django", SkillLevel.EXPERT, "4"),
    skill("fastapi", SkillLevel.STRONG, "3"),
    skill("sqlalchemy", SkillLevel.WORKING, "2"),
    skill("celery", SkillLevel.WORKING, "1"),
    # database
    skill("postgresql", SkillLevel.STRONG, "4"),
    skill("redis", SkillLevel.WORKING, "2"),
    skill("mongodb", SkillLevel.BASIC, "1"),
    # devops
    skill("docker", SkillLevel.STRONG, "3"),
    skill("kubernetes", SkillLevel.WORKING, "2"),
    skill("nginx", SkillLevel.BASIC, "1"),
    # testing
    skill("pytest", SkillLevel.STRONG, "3"),
    skill("selenium", SkillLevel.BASIC, "1"),
    # frontend
    skill("react", SkillLevel.BASIC, "1"),
    skill("vue", SkillLevel.BASIC, "1"),
    # practice
    skill("agile", SkillLevel.BASIC),
)


def wide_profile(**overrides: Any) -> CandidateProfileRead:
    """The twenty-skill, three-city profile the cap exists for."""
    fields: dict[str, Any] = {"skills": WIDE_SKILLS, "locations": CITIES}
    fields.update(overrides)
    return candidate(**fields)


class StubCanonicalizer:
    """A canonicaliser whose answers are data, standing in for phase 4's.

    ``SkillCanonicalizer`` is a protocol precisely so the dictionary can be
    replaced; the planner accepts one as an argument, and a test that wants a
    grouping the bundled YAML cannot express injects it here rather than
    editing the shipped dictionary.
    """

    def __init__(self, groups: Mapping[str, str]) -> None:
        self._groups = dict(groups)

    def canonicalize(self, raw: str) -> str | None:
        """Recognise exactly the names this stub was built with."""
        return raw if raw in self._groups else None

    def group_of(self, canonical: str) -> str | None:
        """The group this stub assigns, or None for a name it does not know."""
        return self._groups.get(canonical)


def search_queries_inside(value: object) -> list[SearchQuery]:
    """Every :class:`SearchQuery` reachable from ``value``, however nested.

    Walks the model rather than one named field, because the question the cap
    raises is not "what does ``queries`` hold" but "can a caller that iterates
    the whole object get at a query the budget refused".
    """
    if isinstance(value, SearchQuery):
        return [value]
    if isinstance(value, BaseModel):
        # type(value), never the instance: reading model_fields off an instance
        # is deprecated in pydantic 2.11 and this suite turns warnings into
        # failures.
        return [
            found
            for name in type(value).model_fields
            for found in search_queries_inside(getattr(value, name))
        ]
    if isinstance(value, Mapping):
        return [found for item in value.values() for found in search_queries_inside(item)]
    if isinstance(value, str | bytes):
        return []
    if isinstance(value, Iterable):
        return [found for item in value for found in search_queries_inside(item)]
    return []


def keyword_sets(plan: QueryPlan) -> set[frozenset[str]]:
    """The distinct searches the plan makes, ignoring keyword order."""
    return {frozenset(query.keywords) for query in plan.queries}


def all_keywords(plan: QueryPlan) -> set[str]:
    """Every term the plan would send to a source."""
    return {word for query in plan.queries for word in query.keywords}


# ── the cap ───────────────────────────────────────────────────────────


def test_the_cap_as_configured_is_the_one_the_planner_enforces() -> None:
    """The shipped configuration must already be safe, with no test setup helping it.

    Every other test here patches the cap to pin an exact number down. If only
    those existed, a planner that read the wrong setting — or none — would stay
    green while production issued sixty queries per source per run.
    """
    plan = plan_queries(wide_profile())

    assert len(plan.queries) <= settings.max_queries_per_run
    assert plan.limit == settings.max_queries_per_run
    # The naive plan is a query per skill per city; the point of the module is
    # that the real one is an order of magnitude smaller.
    assert len(plan.queries) < len(WIDE_SKILLS) * MAX_PLACEMENTS


@pytest.mark.parametrize(
    ("cap", "kept", "dropped"),
    [
        (3, 3, 12),
        (DEFAULT_CAP, DEFAULT_CAP, 15 - DEFAULT_CAP),
        (20, 20, 1),
        (200, 21, 0),
    ],
)
def test_the_cap_is_enforced_by_truncation_and_dropped_counts_what_it_removed(
    monkeypatch: pytest.MonkeyPatch, cap: int, kept: int, dropped: int
) -> None:
    """The budget decides the size of the plan at every setting, not just the default.

    Both halves matter. A plan cut to the cap is what keeps a run inside the
    quota; a plan that *grows* when the budget grows is what stops a generous
    cap from buying eight paginations of one city instead of wider keyword
    coverage. The row at 200 is the other boundary: with budget to spare
    nothing is cut at all, so ``dropped`` cannot be a constant.
    """
    monkeypatch.setattr(settings, "max_queries_per_run", cap)

    plan = plan_queries(wide_profile())

    assert len(plan.queries) <= settings.max_queries_per_run
    assert len(plan.queries) == kept
    assert plan.dropped == dropped
    assert plan.limit == cap
    assert plan.is_truncated is (dropped > 0)
    # Nothing here is a duplicate: the cap is the only thing removing queries.
    assert plan.collapsed == 0
    assert len(set(plan.queries)) == kept


def test_a_cut_plan_spends_its_budget_on_both_dimensions_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three queries must not all be the same city, nor all the same keywords.

    A budget spent entirely on the top group returns one view of every city and
    misses the rest of the stack; spent entirely on the top city it returns the
    whole stack in one market the candidate may not even be able to work in.
    The diagonal walk is what buys coverage of both, and it is invisible unless
    the plan is cut hard enough for the order to matter.
    """
    monkeypatch.setattr(settings, "max_queries_per_run", 3)

    plan = plan_queries(wide_profile())

    assert len({query.area for query in plan.queries}) == 2
    assert len(keyword_sets(plan)) == 2
    # The strongest group leads and is the one that gets the second place.
    assert plan.groups == ("language", "backend")


def test_truncating_the_plan_is_reported_as_a_warning_with_the_numbers_in_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silently halved plan is diagnosed months later as "the sources got worse".

    The run report and the dashboard both read this: coverage was cut because
    of a budget, not because the sources had nothing, and the difference is one
    configuration line away from being fixed.
    """
    monkeypatch.setattr(settings, "max_queries_per_run", 3)

    with structlog.testing.capture_logs() as captured:
        plan = plan_queries(wide_profile())

    warnings = [entry for entry in captured if entry["log_level"] == "warning"]
    assert [entry["event"] for entry in warnings] == ["query_planner.truncated"]
    assert warnings[0]["dropped"] == plan.dropped == 12
    assert warnings[0]["kept"] == len(plan.queries) == 3
    assert warnings[0]["limit"] == 3
    # The summary carries the same facts to the dashboard.
    assert "12" in plan.summary and "3" in plan.summary


def test_the_queries_the_cap_cut_are_not_reachable_from_the_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``dropped`` is a number on purpose; keeping the objects would undo the cap.

    A caller that walks the whole model looking for something to execute — a
    report, a debug endpoint, a future runner — would find twelve extra
    searches and issue them. The count says how much coverage was lost without
    handing anybody the means to buy it back, and the model is frozen so the
    cut cannot be reversed by appending to the plan afterwards.
    """
    monkeypatch.setattr(settings, "max_queries_per_run", 3)

    plan = plan_queries(wide_profile())

    assert plan.dropped == 12
    assert type(plan.dropped) is int
    assert QueryPlan.model_fields["dropped"].annotation is int
    # The only SearchQuery objects anywhere on the model are the surviving ones.
    assert search_queries_inside(plan) == list(plan.queries)
    with pytest.raises(ValidationError):
        plan.queries = ()  # type: ignore[misc]


# ── duplicates ────────────────────────────────────────────────────────


def test_duplicate_queries_collapse_before_the_cap_rather_than_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate that survives to the cap spends budget a real search wanted.

    Two spellings of one name landing in two groups is what a richer dictionary
    makes possible, and the two queries it produces fetch exactly the same
    postings. Collapsing them after truncation would leave this two-query run
    searching for "docker" twice and never for "pytest" at all; collapsing
    first turns the wasted slot back into a search the candidate did not
    otherwise get.
    """
    monkeypatch.setattr(settings, "max_queries_per_run", 2)
    canonicalizer = StubCanonicalizer({"Docker": "devops", "docker": "cloud", "pytest": "testing"})
    profile = candidate(
        skills=[
            skill("Docker", SkillLevel.STRONG, "5"),
            skill("docker", SkillLevel.STRONG, "5"),
            skill("pytest", SkillLevel.STRONG, "5"),
        ],
        locations=("Almaty",),
    )

    plan = plan_queries(profile, canonicalizer=canonicalizer)

    assert plan.collapsed == 1
    assert plan.dropped == 0
    assert len(plan.queries) == 2
    # Case-insensitively, because which of the two spellings survives is a
    # tie-break; that one of them is gone and "pytest" got the slot is not.
    assert {frozenset(word.casefold() for word in query.keywords) for query in plan.queries} == {
        frozenset({"docker"}),
        frozenset({"pytest"}),
    }
    # A collapse is a fact about the profile's skill set, so it reaches the run
    # report rather than being swallowed by the planner.
    assert plan.summary != plan.model_copy(update={"collapsed": 0}).summary


# ── ranking ───────────────────────────────────────────────────────────


def test_the_group_slots_go_to_the_strongest_skills_not_the_first_level_alphabetically() -> None:
    """Sorting on ``SkillLevel`` itself puts "basic" first and looks like it worked.

    ``SkillLevel`` is a ``StrEnum``, so the obvious sort orders the levels
    basic, expert, strong, working — and the three keywords that reach a source
    become the ones the candidate is weakest in. Nothing fails: the run returns
    vacancies, they are simply for a stack this person does not have.
    """
    # The trap, stated rather than described.
    assert [level.value for level in sorted(SkillLevel)] == [
        "basic",
        "expert",
        "strong",
        "working",
    ]
    profile = candidate(
        skills=[
            skill("bash", SkillLevel.BASIC, "1"),
            skill("java", SkillLevel.WORKING, "2"),
            skill("go", SkillLevel.STRONG, "3"),
            skill("python", SkillLevel.EXPERT, "6"),
        ]
    )

    groups = keyword_groups_for(profile)

    assert len(groups) == 1
    assert groups[0].label == "language"
    assert groups[0].keywords == ("python", "go", "java")
    assert len(groups[0].keywords) == MAX_KEYWORDS_PER_GROUP
    # The alphabetically-first level is exactly the one that had to be cut.
    assert "bash" not in groups[0].keywords


def test_a_group_the_candidate_is_expert_in_outranks_one_they_have_only_touched() -> None:
    """Group order decides what the plan is about, and the plan is what gets cut.

    Neither group here is the guaranteed one, so this is the ranking working
    alone. "frontend" sorts before "testing" alphabetically and its group is
    worth more per skill, so a planner that ranked by name — or that compared
    levels as the strings a ``StrEnum`` makes them — would title a QA
    engineer's searches after three frameworks they have barely touched.
    """
    assert "frontend" < "testing"
    profile = candidate(
        skills=[
            skill("react", SkillLevel.BASIC, "1"),
            skill("vue", SkillLevel.BASIC, "1"),
            skill("angular", SkillLevel.BASIC, "1"),
            skill("pytest", SkillLevel.EXPERT, "6"),
            skill("selenium", SkillLevel.STRONG, "3"),
            skill("playwright", SkillLevel.STRONG, "2"),
        ]
    )

    groups = keyword_groups_for(profile)

    assert [group.label for group in groups] == ["testing", "frontend"]
    assert groups[0].keywords == ("pytest", "selenium", "playwright")


def test_the_language_group_leads_every_plan_even_when_it_is_the_weakest() -> None:
    """The one term that actually appears in postings must not lose on group size.

    A candidate has one primary language and several frameworks, so the
    language group can never win a ranking that rewards a group for holding
    several strong skills — and a plan led by "django, fastapi, sqlalchemy"
    searches for words that occur in almost no posting body while never
    searching the word that occurs in a quarter of them. The guarantee is the
    fix; without a test it is one sort-key cleanup away from being lost again.
    """
    profile = candidate(
        skills=[
            skill("python", SkillLevel.BASIC, "1"),
            skill("django", SkillLevel.EXPERT, "8"),
            skill("fastapi", SkillLevel.EXPERT, "6"),
            skill("sqlalchemy", SkillLevel.STRONG, "5"),
        ]
    )

    groups = keyword_groups_for(profile)

    assert [group.label for group in groups] == [GUARANTEED_GROUP, "backend"]
    assert plan_queries(profile).groups[0] == GUARANTEED_GROUP
    # The ranking still holds below the guaranteed slot: this is an exception
    # for one group, not an abandonment of the score.
    assert groups[1].keywords == ("django", "fastapi", "sqlalchemy")


# ── what never becomes a keyword ──────────────────────────────────────


def test_a_skill_the_dictionary_never_heard_of_does_not_become_a_keyword() -> None:
    """An in-house system name matches no posting and wastes a whole search slot.

    The enricher keeps unrecognised skills under a slug of their own spelling so
    the matcher can still compare them textually. As a search term
    "internal-billing-core" returns nothing at all, and with the budget in
    single digits, one such query is an eighth of the run.
    """
    profile = wide_profile(
        skills=[*WIDE_SKILLS, skill("internal-billing-core", SkillLevel.EXPERT, "5")]
    )

    plan = plan_queries(profile)

    assert "internal-billing-core" not in all_keywords(plan)
    assert plan.groups  # the recognised skills still planned a run
    assert FALLBACK_SKILLS_LABEL not in plan.groups


def test_a_cyrillic_only_skill_that_slugged_to_unknown_is_never_searched_for() -> None:
    """The literal "unknown" is what a Cyrillic-only name slugs to, and it matches nothing.

    ``slugify`` strips everything outside ``[a-z0-9]``, so a skill written only
    in Cyrillic arrives at the planner named the literal "unknown". Searching
    for that word returns nothing, and — worse — it would look like a real
    keyword in the logs and in the run report.
    """
    assert slugify(CYRILLIC_SKILL) == UNKNOWN_SLUG == "unknown"
    profile = wide_profile(skills=[*WIDE_SKILLS, skill(UNKNOWN_SLUG, SkillLevel.EXPERT, "9")])

    plan = plan_queries(profile)

    assert UNKNOWN_SLUG not in all_keywords(plan)


def test_a_profile_of_nothing_but_unrecognised_skills_is_still_searched_for() -> None:
    """A hundred-entry dictionary misses real technologies; that is not the candidate's fault.

    Dropping the unknown skills *and* planning nothing would leave a working
    resume with an empty dashboard and no explanation. They are only ever the
    fallback — the "unknown" slug still never travels, because it is a
    placeholder rather than a word.
    """
    profile = candidate(
        skills=[
            skill("internal-billing-core", SkillLevel.EXPERT, "5"),
            skill("legacy-erp", SkillLevel.WORKING, "2"),
            skill(UNKNOWN_SLUG, SkillLevel.EXPERT, "9"),
        ],
        locations=("Almaty",),
    )

    plan = plan_queries(profile)

    assert plan.groups == (FALLBACK_SKILLS_LABEL,)
    assert all_keywords(plan) == {"internal-billing-core", "legacy-erp"}
    assert UNKNOWN_SLUG not in all_keywords(plan)


# ── groups, not skills ────────────────────────────────────────────────


def test_twenty_skills_do_not_become_twenty_searches() -> None:
    """One query per skill is the plan this module exists to refuse.

    Sixty searches per source is minutes of wall clock, a rate-limit ban and,
    because "python", "django" and "celery" return nearly the same postings in
    the same city, almost no extra coverage. Grouping is what turns them into a
    handful of searches that read like the job titles a vacancy is posted under.
    """
    resolver = default_canonicalizer()

    plan = plan_queries(wide_profile())

    assert len(WIDE_SKILLS) == 20
    assert len(keyword_sets(plan)) <= MAX_KEYWORD_GROUPS
    # Not a single-skill query anywhere: every search carries its group's terms.
    assert min(len(query.keywords) for query in plan.queries) >= 2
    assert max(len(query.keywords) for query in plan.queries) == MAX_KEYWORDS_PER_GROUP
    for query in plan.queries:
        assert len({resolver.group_of(word) for word in query.keywords}) == 1
    # The weakest groups are the ones left out, not an arbitrary five.
    assert set(plan.groups) <= {"language", "backend", "database", "devops", "testing"}
    assert "practice" not in plan.groups


# ── where to look ─────────────────────────────────────────────────────


def test_a_fully_remote_candidate_is_searched_remotely_before_any_city() -> None:
    """Someone not bound to a city should not have their budget spent on cities first.

    The remote slot leading is the difference between a remote-only candidate
    seeing remote postings in the top of a truncated plan and seeing three
    views of the city they happen to live in.
    """
    profile = wide_profile(locations=CITIES[:2], remote_pref=RemoteType.FULL)

    places = placements_for(profile)

    assert places == (
        Placement(remote=RemoteType.FULL),
        Placement(area="Almaty"),
        Placement(area="Astana"),
    )
    plan = plan_queries(profile)
    assert plan.placements == 3
    assert plan.queries[0].remote is RemoteType.FULL
    assert plan.queries[0].area is None
    # A city slot carries no format filter: most sources do not label the
    # format, and filtering on it there would drop nearly every posting.
    assert all(query.remote is None for query in plan.queries if query.area is not None)


def test_relocation_adds_one_unconstrained_slot_after_the_cities() -> None:
    """Willingness to move is a search of the whole region a source serves.

    It has to come after the cities, though: the unconstrained slot is the
    broadest and least specific one there is, and putting it first would make
    the top of every truncated plan a nationwide search.
    """
    profile = wide_profile(locations=("Almaty",), relocation=True)

    assert placements_for(profile) == (Placement(area="Almaty"), Placement())


def test_the_places_one_profile_fans_out_over_are_capped_and_deduplicated() -> None:
    """Each place multiplies the whole keyword set, so places are where a plan explodes.

    Five cities is five times the plan for a candidate who, past the third
    entry, is being searched in markets they mentioned in passing. The repeated
    spelling is the other half: "Almaty" and "  Almaty  " are one place, and
    keeping both would silently buy the same postings twice.
    """
    profile = wide_profile(locations=("Almaty", "  Almaty  ", "Astana", "Shymkent", "Karaganda"))

    places = placements_for(profile)

    assert len(places) == MAX_PLACEMENTS == 3
    assert places == (
        Placement(area="Almaty"),
        Placement(area="Astana"),
        Placement(area="Shymkent"),
    )
    assert plan_queries(profile).placements == MAX_PLACEMENTS


def test_a_location_too_long_to_be_a_place_is_skipped_rather_than_truncated() -> None:
    """A sentence in the locations field must not become a search for half a sentence.

    ``SearchQuery.area`` accepts 100 characters, so an over-long location either
    crashes the planner inside a pydantic validator three layers from the cause,
    or gets cut mid-word into a term matching nothing. It is dropped instead,
    and the places the candidate really named still get searched.
    """
    sentence = "a" * (MAX_AREA_CHARS + 1)
    profile = wide_profile(locations=("Almaty", sentence))

    assert placements_for(profile) == (Placement(area="Almaty"),)
    plan = plan_queries(profile)
    assert all(query.area is None or len(query.area) <= MAX_AREA_CHARS for query in plan.queries)


def test_a_profile_that_said_nothing_about_where_is_still_searched_somewhere() -> None:
    """No location, no relocation, no preference is a resume, not an error.

    Planning nothing here would leave a perfectly usable profile with an empty
    dashboard. One unconstrained slot lets every source search the region it
    already serves, which is the best guess available and costs one query.
    """
    profile = wide_profile(locations=())

    assert placements_for(profile) == (Placement(),)
    plan = plan_queries(profile)
    assert plan.placements == 1
    assert all(query.area is None and query.remote is None for query in plan.queries)


# ── filters that are deliberately never set ───────────────────────────


def test_no_salary_country_or_language_filter_is_ever_set() -> None:
    """Each of these narrows the search on data this module cannot read correctly.

    ``SearchQuery`` carries no currency, so a salary floor of 4000 USD sent to a
    source working in tenge filters out everything the candidate could take. The
    country cannot be derived from free-text locations, and picking one of the
    profile's languages hides every posting written in the others. All three are
    present on this profile precisely so their absence in the plan is a decision
    rather than a coincidence.
    """
    profile = wide_profile(
        salary_min=Decimal("4000.00"),
        salary_currency="USD",
        languages=[{"name": "English", "level": "B2"}],
    )

    plan = plan_queries(profile)

    assert plan.queries
    for query in plan.queries:
        assert query.salary_min is None
        assert query.country is None
        assert query.language is None
        assert query.employment_type is None
        assert query.posted_within_days == min(
            settings.max_vacancy_age_days, MAX_POSTED_WITHIN_DAYS
        )


def test_an_age_limit_larger_than_the_query_field_allows_is_clamped_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configuration typo must not surface as a crash inside a pure planner.

    ``max_vacancy_age_days`` is unbounded above while the query field stops at a
    year, so an extra zero in the environment would otherwise take the whole run
    down with a validation error naming a field nobody set.
    """
    monkeypatch.setattr(settings, "max_vacancy_age_days", MAX_POSTED_WITHIN_DAYS * 10)

    plan = plan_queries(wide_profile())

    assert plan.queries
    assert all(query.posted_within_days == MAX_POSTED_WITHIN_DAYS for query in plan.queries)


# ── purity ────────────────────────────────────────────────────────────


def test_planning_is_synchronous_and_repeatable() -> None:
    """The planner is arithmetic; anything else here would be a hidden dependency.

    Two runs over one profile that disagreed would make the whole pipeline
    unreproducible — a run's coverage would depend on dictionary iteration
    order rather than on the resume. And a planner that awaited something would
    mean deciding a budget needs a session, a network or a clock, which is how a
    pure decision becomes a source of run failures.
    """
    assert not inspect.iscoroutinefunction(plan_queries)
    assert not inspect.iscoroutinefunction(keyword_groups_for)
    assert not inspect.iscoroutinefunction(placements_for)
    profile = wide_profile()
    before = [skill_read.canonical_name for skill_read in profile.skills]

    first = plan_queries(profile)
    second = plan_queries(profile)

    assert first == second
    assert first.queries == second.queries
    assert first.groups == second.groups
    # The profile handed in is not sorted in place on the way through.
    assert [skill_read.canonical_name for skill_read in profile.skills] == before


# ── profiles with nothing to search for ───────────────────────────────


def test_a_profile_with_no_skills_and_no_headline_plans_nothing_and_says_why() -> None:
    """A keyword-less search is a full download of every feed, so nothing is planned.

    Both halves are the point. Planning an empty query would pull every posting
    each source has and call it a match run; raising would turn a half-parsed
    resume into a failed pipeline. The warning is what tells the run report
    which of the two happened.
    """
    profile = candidate(locations=("Almaty",))

    with structlog.testing.capture_logs() as captured:
        plan = plan_queries(profile)

    assert plan.queries == ()
    assert plan.groups == ()
    assert plan.placements == 1
    # An empty plan still produces a line for the run report: "nothing was
    # searched for" is the one result a user most needs written down, and a
    # plan with no groups must not reach the dashboard as a blank string.
    assert plan.summary.endswith(".")
    assert "0" in plan.summary
    assert plan.dropped == 0
    assert plan.collapsed == 0
    assert plan.is_truncated is False
    assert plan.limit == settings.max_queries_per_run
    warnings = [entry for entry in captured if entry["log_level"] == "warning"]
    assert [entry["event"] for entry in warnings] == ["query_planner.no_keywords"]


def test_a_profile_with_only_a_headline_is_searched_by_job_title() -> None:
    """The headline is the last resort and the best single term there is.

    A resume whose skills all missed the dictionary still says what job the
    person wants, in the words vacancies are titled with. Falling back to it
    turns an empty dashboard into a usable one.
    """
    profile = candidate(headline="  Senior   Python Developer ", locations=("Almaty",))

    plan = plan_queries(profile)

    assert plan.groups == (FALLBACK_HEADLINE_LABEL,)
    # Collapsed whitespace: the stored headline is whatever a person typed.
    assert all_keywords(plan) == {"Senior Python Developer"}


def test_a_headline_that_is_really_a_summary_paragraph_is_not_a_search_term() -> None:
    """A paragraph as a query matches nothing and costs a slot to find that out.

    Plenty of resumes put a two-line pitch where the job title goes. It is the
    same non-answer as no headline at all, so it takes the same route: nothing
    planned, and the run says so.
    """
    profile = candidate(headline="I am a " + "very " * 40 + "senior engineer")

    plan = plan_queries(profile)

    assert plan.queries == ()
    assert plan.groups == ()


def test_every_query_carries_the_profiles_own_title() -> None:
    """The one field of a plan that is the same on every query of it.

    The groups say what this profile knows and differ from each other; the
    headline says what it is looking for and does not. A source with something
    to rank needs the difference, and measured on hh the cost of not having it
    was a run that opened catalogue pages for Go, C, JavaScript, Linux and C#
    off a resume headed "Python Developer" — every one of those languages
    genuinely on it, and none of them the one wanted.
    """
    profile = candidate(
        headline="Python Developer — Backend / AI-интеграции",
        skills=[skill("python"), skill("go"), skill("docker")],
        locations=["Алматы"],
    )

    plan = plan_queries(profile)

    assert plan.queries
    assert {query.headline for query in plan.queries} == {
        "Python Developer — Backend / AI-интеграции"
    }


def test_a_blank_headline_is_sent_as_none_rather_than_as_an_empty_string() -> None:
    """An empty headline is not a headline, and a source must not weigh one."""
    profile = candidate(
        headline="   ", skills=[skill("python"), skill("docker")], locations=["Алматы"]
    )

    plan = plan_queries(profile)

    assert plan.queries
    assert all(query.headline is None for query in plan.queries)
