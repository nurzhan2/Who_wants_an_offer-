"""Turning an extraction into storable facts.

The load-bearing behaviour here is skill merging. A resume that lists "Python"
in its sidebar and "Python 3" in a job description yields two extracted skills
that canonicalise to the same name. Writing both violates
``uq_profile_skill_profile_id_canonical_name`` — the same constraint that
already caught a delete-before-insert ordering bug in phase 1. This is the other
way to trip it, and it happens on close to every real resume.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.db.enums import Seniority, SkillEvidence, SkillLevel
from app.resume import enricher
from app.resume.skills import SkillCanonicalizer
from app.schemas.llm import ExtractedSkill, ProfileExtraction, SkillMention, StatedLevel, WorkPeriod

pytestmark = pytest.mark.unit

TODAY = date(2026, 9, 1)


class StubCanonicalizer:
    """A canonicaliser with a known, tiny vocabulary.

    Using the real dictionary here would make these tests fail whenever someone
    adds an alias to skills_min.yaml, which is not what they are checking.
    """

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._mapping = mapping or {
            "python": "python",
            "python 3": "python",
            "python3": "python",
            "postgres": "postgresql",
            "postgresql": "postgresql",
            "fastapi": "fastapi",
        }

    def canonicalize(self, raw: str) -> str | None:
        """Look the name up, case-insensitively."""
        return self._mapping.get(raw.strip().casefold())

    def group_of(self, canonical: str) -> str | None:
        """Not exercised by these tests."""
        return None


def skill(
    name: str,
    *,
    level: StatedLevel | None = None,
    mentioned_in: SkillMention = "skills_block",
    companies: tuple[str, ...] = (),
) -> ExtractedSkill:
    """One extracted skill."""
    return ExtractedSkill(
        name=name, level=level, mentioned_in=mentioned_in, companies=list(companies)
    )


def extraction(
    *,
    skills: tuple[ExtractedSkill, ...] = (),
    periods: tuple[WorkPeriod, ...] = (),
    stated_total_years: float | None = None,
) -> ProfileExtraction:
    """An extraction carrying only what a test needs."""
    return ProfileExtraction(
        skills=list(skills),
        work_periods=list(periods),
        stated_total_years=stated_total_years,
    )


def job(
    company: str, start: str, end: str | None, *, title: str = "Engineer", current: bool = False
) -> WorkPeriod:
    """One work period."""
    return WorkPeriod(company=company, title=title, start=start, end=end, is_current=current)


def canonicalizer() -> SkillCanonicalizer:
    """The stub, typed as the protocol the enricher accepts."""
    return StubCanonicalizer()


# ── merging: the reason this module exists ────────────────────────────


def test_spellings_of_one_skill_collapse_to_a_single_record() -> None:
    """Two spellings, one canonical name, one row. Without this the write hits
    the (profile_id, canonical_name) unique constraint."""
    result = enricher.enrich_skills(
        extraction(skills=(skill("Python"), skill("Python 3"))),
        today=TODAY,
        canonicalizer=canonicalizer(),
    )
    skills, _ = result

    assert [s.canonical_name for s in skills] == ["python"]


def test_canonical_names_are_unique_after_enrichment() -> None:
    """The property the repository depends on, asserted directly."""
    skills, _ = enricher.enrich_skills(
        extraction(
            skills=(
                skill("Python"),
                skill("python3"),
                skill("Postgres"),
                skill("PostgreSQL"),
                skill("FastAPI"),
            )
        ),
        today=TODAY,
        canonicalizer=canonicalizer(),
    )

    names = [s.canonical_name for s in skills]

    assert len(names) == len(set(names))
    assert set(names) == {"python", "postgresql", "fastapi"}


def test_merging_keeps_every_original_spelling() -> None:
    """The raw names are the only record of what the resume said, and the input
    for extending the dictionary later. Overwriting them loses that."""
    skills, _ = enricher.enrich_skills(
        extraction(skills=(skill("Python"), skill("Python 3"))),
        today=TODAY,
        canonicalizer=canonicalizer(),
    )

    assert skills[0].raw_names == ("Python", "Python 3")


def test_merging_takes_the_stronger_level() -> None:
    """One mention says expert, another says basic. Downgrading the candidate
    because of extraction order would be arbitrary."""
    skills, _ = enricher.enrich_skills(
        extraction(skills=(skill("Python", level="basic"), skill("Python 3", level="expert"))),
        today=TODAY,
        canonicalizer=canonicalizer(),
    )

    assert skills[0].level is SkillLevel.EXPERT


def test_merging_takes_the_longer_experience() -> None:
    """Evidence accumulates: a skill used at two jobs has the longer history of
    the two, not whichever was merged last."""
    periods = (job("Short", "2024-01", "2024-06"), job("Long", "2020-01", "2023-12"))
    skills, _ = enricher.enrich_skills(
        extraction(
            skills=(
                skill("Python", companies=("Short",)),
                skill("Python 3", companies=("Long",)),
            ),
            periods=periods,
        ),
        today=TODAY,
        canonicalizer=canonicalizer(),
    )

    assert skills[0].years == Decimal("4.0")


def test_a_recognised_spelling_makes_the_whole_skill_known() -> None:
    """One unknown alias alongside a known one must not mark the skill unknown:
    that would understate how well the dictionary is doing."""
    skills, _ = enricher.enrich_skills(
        extraction(skills=(skill("Питон-подобный"), skill("Python"))),
        today=TODAY,
        canonicalizer=canonicalizer(),
    )
    merged = {s.canonical_name: s for s in skills}

    assert merged["python"].is_unknown is False


def test_the_merge_is_reported_so_it_is_visible() -> None:
    """Silent collapsing would hide a canonicaliser that is too aggressive."""
    _, warnings = enricher.enrich_skills(
        extraction(skills=(skill("Python"), skill("Python 3"), skill("python3"))),
        today=TODAY,
        canonicalizer=canonicalizer(),
    )

    assert any("collapsed" in warning for warning in warnings)


# ── evidence recovered from the per-job stacks ────────────────────────


def test_a_skill_is_dated_from_the_job_stacks_when_it_names_no_companies() -> None:
    """The model fills skills[].companies unreliably but fills work_periods[].stack
    consistently, and the two carry the same fact from opposite directions.

    Without this fallback every skill on a real extraction gets zero years and
    lands at ``basic``, which flattens the level multiplier in the coverage
    score for a reason that has nothing to do with the candidate.
    """
    periods = (
        WorkPeriod(
            company="Acme",
            title="Engineer",
            start="2020-01",
            end="2023-12",
            stack=["Python 3", "PostgreSQL"],
        ),
    )
    skills, _ = enricher.enrich_skills(
        extraction(skills=(skill("Python"),), periods=periods),
        today=TODAY,
        canonicalizer=canonicalizer(),
    )

    assert skills[0].years == Decimal("4.0")
    assert skills[0].last_used_year == 2023


def test_the_stack_fallback_matches_through_the_canonicaliser() -> None:
    """A job listing "Python 3" is evidence for the skill "Python"; matching the
    raw strings would miss it exactly when the spellings differ, which is the
    only case where the fallback is needed."""
    periods = (
        WorkPeriod(
            company="Acme", title="Engineer", start="2021-01", end="2021-12", stack=["Postgres"]
        ),
    )
    skills, _ = enricher.enrich_skills(
        extraction(skills=(skill("PostgreSQL"),), periods=periods),
        today=TODAY,
        canonicalizer=canonicalizer(),
    )

    assert skills[0].years == Decimal("1.0")


def test_an_explicit_company_list_wins_over_the_stacks() -> None:
    """When the model did the cross-referencing, believe it: the fallback is a
    recovery path, not a second opinion."""
    periods = (
        WorkPeriod(
            company="Short", title="Engineer", start="2024-01", end="2024-06", stack=["Python"]
        ),
        WorkPeriod(
            company="Long", title="Engineer", start="2020-01", end="2023-12", stack=["Python"]
        ),
    )
    skills, _ = enricher.enrich_skills(
        extraction(skills=(skill("Python", companies=("Short",)),), periods=periods),
        today=TODAY,
        canonicalizer=canonicalizer(),
    )

    assert skills[0].years == Decimal("0.5")


def test_a_sidebar_only_skill_still_has_no_evidence() -> None:
    """The fallback must not invent experience. A skill in no job stack and no
    company list has nothing behind it, and saying otherwise would promote every
    listed technology to whatever the career length happens to be."""
    periods = (
        WorkPeriod(
            company="Acme", title="Engineer", start="2015-01", end="2025-12", stack=["Python"]
        ),
    )
    skills, _ = enricher.enrich_skills(
        extraction(skills=(skill("FastAPI"),), periods=periods),
        today=TODAY,
        canonicalizer=canonicalizer(),
    )

    assert skills[0].years is None
    assert skills[0].level is SkillLevel.WORKING
    assert skills[0].evidence is SkillEvidence.STATED


def test_a_dated_skill_is_corroborated() -> None:
    """The two fields answer different questions, and this is the one that says
    the years behind the level are computed rather than assumed."""
    periods = (
        WorkPeriod(
            company="Acme", title="Engineer", start="2020-01", end="2023-12", stack=["Python"]
        ),
    )
    skills, _ = enricher.enrich_skills(
        extraction(skills=(skill("Python"),), periods=periods),
        today=TODAY,
        canonicalizer=canonicalizer(),
    )

    assert skills[0].evidence is SkillEvidence.CORROBORATED


def test_one_dated_mention_corroborates_the_merged_skill() -> None:
    """Merging must not let an undated spelling erase the evidence a dated one
    brought with it."""
    periods = (
        WorkPeriod(
            company="Acme", title="Engineer", start="2020-01", end="2023-12", stack=["Python"]
        ),
    )
    skills, _ = enricher.enrich_skills(
        extraction(
            skills=(skill("Python 3"), skill("Python", companies=("Acme",))), periods=periods
        ),
        today=TODAY,
        canonicalizer=canonicalizer(),
    )

    assert skills[0].evidence is SkillEvidence.CORROBORATED


# ── unknown skills ────────────────────────────────────────────────────


def test_an_unknown_skill_is_kept_under_a_slug() -> None:
    """Dropping it would lose a real signal, and the dictionary is deliberately
    incomplete until phase 4."""
    skills, _ = enricher.enrich_skills(
        extraction(skills=(skill("Elixir/OTP"),)), today=TODAY, canonicalizer=canonicalizer()
    )

    assert skills[0].canonical_name == "elixir-otp"
    assert skills[0].is_unknown is True
    assert skills[0].raw_names == ("Elixir/OTP",)


def test_warnings_count_unknown_skills_without_naming_them() -> None:
    """Skill names are resume content. The count is what the logs need; the
    names stay in the database where the dictionary work can read them."""
    _, warnings = enricher.enrich_skills(
        extraction(skills=(skill("Elixir"), skill("Erlang"))),
        today=TODAY,
        canonicalizer=canonicalizer(),
    )
    joined = " ".join(warnings)

    assert "2 skills" in joined
    assert "Elixir" not in joined
    assert "Erlang" not in joined


@pytest.mark.parametrize("raw", ["  ", "", "\t"])
def test_blank_skill_names_are_ignored(raw: str) -> None:
    """A stray empty string must not become a skill called "unknown"."""
    skills, _ = enricher.enrich_skills(
        extraction(skills=(skill(raw),)), today=TODAY, canonicalizer=canonicalizer()
    )

    assert skills == ()


# ── level inference ───────────────────────────────────────────────────


def test_a_stated_level_beats_anything_inferred() -> None:
    """When the resume says it, believe it: the heuristic exists for the rest."""
    assert enricher.infer_level(stated="basic", years=Decimal("10")) is SkillLevel.BASIC


@pytest.mark.parametrize(
    ("years", "expected"),
    [
        (Decimal("0.5"), SkillLevel.BASIC),
        (Decimal("2"), SkillLevel.WORKING),
        (Decimal("4"), SkillLevel.STRONG),
        (Decimal("8"), SkillLevel.EXPERT),
    ],
)
def test_level_follows_years_when_the_skill_is_dated(years: Decimal, expected: SkillLevel) -> None:
    """Years at a named employer are the strongest evidence available."""
    assert enricher.infer_level(stated=None, years=years) is expected


def test_an_undated_skill_is_neutral_rather_than_weak() -> None:
    """A skill with nothing dating it used to be scored ``basic``, which the
    coverage score multiplies by 0.7.

    Plenty of strong candidates never write a technology list under each job,
    so that was a 30% penalty for a formatting habit rather than a fact about
    the person. The uncertainty is recorded as ``evidence`` instead; the level
    is the neutral rung.
    """
    assert enricher.infer_level(stated=None, years=None) is SkillLevel.WORKING


# ── seniority ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("years", "expected"),
    [
        (Decimal("1"), Seniority.JUNIOR),
        (Decimal("4"), Seniority.MIDDLE),
        (Decimal("7"), Seniority.SENIOR),
    ],
)
def test_seniority_baseline_comes_from_computed_years(years: Decimal, expected: Seniority) -> None:
    """Experience, not the title, is the baseline — titles inflate."""
    assert enricher.infer_seniority(total_years=years, titles=["Engineer"]) is expected


@pytest.mark.parametrize(
    "title",
    [
        "Team Lead",
        "Head of Engineering",
        "Тимлид",
        "Руководитель разработки",
        "Начальник отдела",
        "Principal Engineer",
    ],
)
def test_a_leadership_title_is_taken_at_face_value(title: str) -> None:
    """Leading people is a fact about the role, not a self-assessment, and it
    is the one title that overrides the years-based baseline."""
    assert enricher.infer_seniority(total_years=Decimal("3"), titles=[title]) is Seniority.LEAD


def test_a_senior_title_promotes_by_at_most_one_step() -> None:
    """ "Senior" after three years is ordinary in this market, so it may nudge
    the grade but must not skip one."""
    assert (
        enricher.infer_seniority(total_years=Decimal("1"), titles=["Senior Developer"])
        is Seniority.MIDDLE
    )


def test_a_junior_title_pulls_the_grade_down() -> None:
    """An intern with four years of overlapping side projects is still an
    intern."""
    assert (
        enricher.infer_seniority(total_years=Decimal("4"), titles=["Junior Developer"])
        is Seniority.JUNIOR
    )


def test_no_titles_and_no_experience_gives_no_grade() -> None:
    """None is honest; junior would be an invented fact."""
    assert enricher.infer_seniority(total_years=Decimal("0"), titles=[]) is None


# ── the whole pass ────────────────────────────────────────────────────


def test_total_years_is_the_union_and_not_the_resume_claim() -> None:
    """The headline guarantee of the phase: overlapping jobs are counted once,
    and whatever the resume asserts about its own total is not used."""
    result = enricher.enrich(
        extraction(
            periods=(job("Acme", "2022-01", "2023-12"), job("Freelance", "2023-01", "2023-12")),
            stated_total_years=3.0,
        ),
        today=TODAY,
        canonicalizer=canonicalizer(),
    )

    assert result.total_years == Decimal("2.0")


def test_a_resume_that_overstates_its_experience_is_flagged() -> None:
    """Worth seeing in the logs: it is usually the resume's arithmetic that is
    wrong, and a large gap can also mean the dates were misread."""
    result = enricher.enrich(
        extraction(periods=(job("Acme", "2024-01", "2024-12"),), stated_total_years=8.0),
        today=TODAY,
        canonicalizer=canonicalizer(),
    )

    assert result.stated_years_delta == Decimal("7.0")
    assert any("claims 8.0 years" in warning for warning in result.warnings)


def test_a_small_discrepancy_is_not_worth_a_warning() -> None:
    """Rounding and part-months differ; only a real disagreement is a signal."""
    result = enricher.enrich(
        extraction(periods=(job("Acme", "2024-01", "2024-12"),), stated_total_years=1.0),
        today=TODAY,
        canonicalizer=canonicalizer(),
    )

    assert not any("claims" in warning for warning in result.warnings)


def test_domains_are_deduplicated_and_normalised() -> None:
    """They feed the domain-fit score, where a duplicate would double-count."""
    periods = (
        WorkPeriod(
            company="A", title="Engineer", start="2020-01", end="2021-01", domains=["Fintech"]
        ),
        WorkPeriod(
            company="B",
            title="Engineer",
            start="2021-02",
            end="2022-01",
            domains=["fintech", "edtech"],
        ),
    )
    result = enricher.enrich(
        extraction(periods=periods), today=TODAY, canonicalizer=canonicalizer()
    )

    assert result.domains == ("fintech", "edtech")


def test_stale_skills_are_identifiable() -> None:
    """ "Angular, last used in 2019" is a weaker claim than "Angular"."""
    fresh = enricher.EnrichedSkill(
        canonical_name="python",
        raw_names=("Python",),
        years=None,
        level=SkillLevel.WORKING,
        evidence=SkillEvidence.STATED,
        last_used_year=2025,
    )
    cold = enricher.EnrichedSkill(
        canonical_name="angular",
        raw_names=("Angular",),
        years=None,
        level=SkillLevel.WORKING,
        evidence=SkillEvidence.STATED,
        last_used_year=2019,
    )

    assert fresh.is_stale(today=TODAY) is False
    assert cold.is_stale(today=TODAY) is True


def test_a_skill_never_used_at_a_job_is_not_stale() -> None:
    """No evidence of use is not evidence of disuse."""
    unknown = enricher.EnrichedSkill(
        canonical_name="rust",
        raw_names=("Rust",),
        years=None,
        level=SkillLevel.WORKING,
        evidence=SkillEvidence.STATED,
        last_used_year=None,
    )

    assert unknown.is_stale(today=TODAY) is False
