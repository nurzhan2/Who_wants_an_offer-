"""Experience arithmetic.

The bug this file exists to prevent: adding job durations together. People hold
overlapping jobs — a full-time role plus freelance, a job plus a side project —
and summing them gives a third-year student twelve years of experience. Every
number downstream is derived from this one, so a quiet error here poisons every
match score in the product.

No database, no model, no network: these are pure functions.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.resume import dates
from app.schemas.llm import WorkPeriod

pytestmark = pytest.mark.unit

#: Fixed so "still employed" has a reproducible answer.
TODAY = date(2026, 9, 1)


def job(
    company: str = "Acme",
    start: str | None = "2020-01",
    end: str | None = "2020-12",
    *,
    is_current: bool = False,
    title: str = "Engineer",
) -> WorkPeriod:
    """One work period, with only the fields these tests care about."""
    return WorkPeriod(company=company, title=title, start=start, end=end, is_current=is_current)


# ── parsing ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2021-03", 2021 * 12 + 2),
        ("2021-3", 2021 * 12 + 2),
        ("  2021-03  ", 2021 * 12 + 2),
        ("2021-01", 2021 * 12),
        ("2021-12", 2021 * 12 + 11),
    ],
)
def test_recognised_dates_map_to_a_month_index(value: str, expected: int) -> None:
    """The prompt normalises to YYYY-MM, but a model is not a parser and the
    calculation must not trust that it always complied."""
    assert dates.parse_month(value) == expected


@pytest.mark.parametrize(
    "value",
    [None, "", "2021", "March 2021", "2021-13", "2021-00", "1200-05", "2400-05", "not a date"],
)
def test_unusable_dates_are_rejected_rather_than_guessed(value: str | None) -> None:
    """A date we cannot read must become None, not a plausible number: a wrong
    guess here silently changes someone's seniority."""
    assert dates.parse_month(value) is None


# ── the headline case ─────────────────────────────────────────────────


def test_overlapping_jobs_are_counted_once() -> None:
    """THE test of this module. A full-time job and a freelance contract running
    at the same time are one stretch of experience, not two.

    Summing durations would give 36 months here. The union is 24."""
    periods = [
        job("Acme", "2022-01", "2023-12"),
        job("Freelance", "2023-01", "2023-12"),
    ]

    span = dates.total_experience(periods, today=TODAY)

    assert span.months == 24
    assert span.years == Decimal("2.0")


def test_fully_contained_job_adds_nothing() -> None:
    """A side project entirely inside a longer job is already counted."""
    periods = [
        job("Acme", "2020-01", "2024-12"),
        job("Side", "2021-06", "2021-08"),
    ]

    assert dates.total_experience(periods, today=TODAY).months == 60


def test_three_way_overlap_collapses_to_one_span() -> None:
    """Overlap is not only pairwise; the merge has to be transitive."""
    periods = [
        job("A", "2020-01", "2020-12"),
        job("B", "2020-06", "2021-06"),
        job("C", "2021-01", "2021-12"),
    ]

    assert dates.total_experience(periods, today=TODAY).months == 24


def test_a_career_break_is_not_swallowed() -> None:
    """The mirror image: a genuine gap must stay a gap, or the union would be
    just as wrong in the other direction."""
    periods = [
        job("A", "2018-01", "2018-12"),
        job("B", "2022-01", "2022-12"),
    ]

    assert dates.total_experience(periods, today=TODAY).months == 24


def test_back_to_back_jobs_join_without_a_gap() -> None:
    """Leaving in March and starting in April is continuous employment, and the
    inclusive month counting must not drop the boundary month."""
    periods = [
        job("A", "2021-01", "2021-03"),
        job("B", "2021-04", "2021-06"),
    ]

    assert dates.total_experience(periods, today=TODAY).months == 6


# ── boundaries ────────────────────────────────────────────────────────


def test_a_one_month_job_counts_as_one_month() -> None:
    """Start and end in the same month is a month of work, not zero. Exclusive
    arithmetic is the easy way to get this wrong."""
    assert dates.total_experience([job("A", "2021-03", "2021-03")], today=TODAY).months == 1


def test_a_current_job_is_counted_up_to_today() -> None:
    """ "по настоящее время" has to mean something, and it has to mean the same
    thing every time the tests run — hence an injected date."""
    span = dates.total_experience(
        [job("A", "2025-09", None, is_current=True)], today=date(2026, 9, 1)
    )

    assert span.months == 13


def test_a_current_job_ignores_any_end_date_it_was_given() -> None:
    """A resume that says both "2024" and "present" means present."""
    span = dates.total_experience(
        [job("A", "2024-01", "2024-06", is_current=True)], today=date(2026, 9, 1)
    )

    # January 2024 through September 2026 inclusive.
    assert span.months == 33


def test_transposed_dates_do_not_subtract_experience() -> None:
    """A typo that puts the end before the start must not produce a negative
    span, which would quietly reduce the total."""
    span = dates.total_experience([job("A", "2021-06", "2020-06")], today=TODAY)

    assert span.months == 1
    assert any("end precedes start" in warning for warning in span.warnings)


def test_a_future_end_date_is_clamped_to_today() -> None:
    """Nobody has experience they have not lived yet."""
    span = dates.total_experience([job("A", "2026-01", "2030-01")], today=date(2026, 9, 1))

    assert span.months == 9
    assert any("future" in warning for warning in span.warnings)


def test_a_job_with_no_usable_start_is_skipped_with_a_warning() -> None:
    """Dropping it is right; guessing a start would invent experience."""
    span = dates.total_experience([job("A", None, "2021-12")], today=TODAY)

    assert span.months == 0
    assert any("unusable start" in warning for warning in span.warnings)


def test_a_job_with_no_end_and_no_current_flag_counts_one_month() -> None:
    """An open-ended period that does not claim to be current is a hole in the
    resume. Counting the one provable month beats extending it to today."""
    span = dates.total_experience([job("A", "2021-01", None)], today=TODAY)

    assert span.months == 1
    assert any("no end date" in warning for warning in span.warnings)


def test_no_periods_is_zero_not_an_error() -> None:
    """A resume with no dated jobs is a real resume, not a failure."""
    assert dates.total_experience([], today=TODAY).months == 0


@pytest.mark.parametrize(
    ("months", "years"),
    [(0, "0.0"), (1, "0.1"), (6, "0.5"), (12, "1.0"), (18, "1.5"), (30, "2.5"), (77, "6.4")],
)
def test_months_convert_to_years_at_one_decimal(months: int, years: str) -> None:
    """The column is Numeric(4, 1); anything finer would be rounded on write
    anyway, and rounding here keeps the stored and computed values equal."""
    assert dates.ExperienceSpan(months=months).years == Decimal(years)


# ── per-skill experience ──────────────────────────────────────────────


def test_skill_years_use_only_the_jobs_where_the_skill_was_used() -> None:
    """Per-skill experience is what makes coverage scoring meaningful. Counting
    the whole career for every listed skill would flatten the signal."""
    periods = [
        job("Acme", "2020-01", "2021-12"),
        job("Globex", "2022-01", "2023-12"),
    ]

    span = dates.experience_with(periods, ["Acme"], today=TODAY)

    assert span.months == 24


def test_skill_years_also_deduplicate_overlapping_jobs() -> None:
    """The same trap as the total, one level down: PostgreSQL used at two
    concurrent jobs is not two separate stretches of PostgreSQL."""
    periods = [
        job("Acme", "2022-01", "2023-12"),
        job("Freelance", "2023-01", "2023-12"),
    ]

    span = dates.experience_with(periods, ["Acme", "Freelance"], today=TODAY)

    assert span.months == 24


def test_skill_company_matching_ignores_case_and_padding() -> None:
    """The model echoes company names back as prose, not as identifiers."""
    periods = [job("Acme Corp", "2020-01", "2020-12")]

    assert dates.experience_with(periods, ["  acme corp "], today=TODAY).months == 12


def test_a_skill_tied_to_no_job_has_no_years() -> None:
    """A skill listed only in a sidebar has no employer behind it, and inventing
    years for it would promote it to a level it has not earned."""
    periods = [job("Acme", "2020-01", "2020-12")]

    assert dates.experience_with(periods, [], today=TODAY).months == 0
    assert dates.experience_with(periods, ["Unknown Ltd"], today=TODAY).months == 0


# ── last used ─────────────────────────────────────────────────────────


def test_last_used_year_is_the_latest_of_the_matching_jobs() -> None:
    """Staleness is judged on this, so it has to be the most recent use rather
    than the first one found."""
    periods = [
        job("Old", "2015-01", "2016-12"),
        job("Recent", "2022-01", "2023-06"),
    ]

    assert dates.last_used_year(periods, ["Old", "Recent"], today=TODAY) == 2023


def test_last_used_year_of_a_current_job_is_this_year() -> None:
    """A skill in use right now is not stale, whatever the resume's dates say."""
    periods = [job("Now", "2019-01", None, is_current=True)]

    assert dates.last_used_year(periods, ["Now"], today=date(2026, 9, 1)) == 2026


def test_last_used_year_is_none_when_nothing_matches() -> None:
    """No evidence is not the same as old evidence."""
    assert dates.last_used_year([job("Acme")], ["Other"], today=TODAY) is None


# ── merging, directly ─────────────────────────────────────────────────


def test_merge_is_order_independent() -> None:
    """Work periods arrive in whatever order the resume listed them."""
    a = dates.MonthInterval(start=10, end=20)
    b = dates.MonthInterval(start=15, end=30)

    assert dates.merge_intervals([a, b]) == dates.merge_intervals([b, a])


def test_merge_keeps_the_widest_end() -> None:
    """A short job inside a long one must not truncate the long one."""
    long_job = dates.MonthInterval(start=0, end=100)
    short_job = dates.MonthInterval(start=10, end=20)

    assert dates.merge_intervals([long_job, short_job]) == [long_job]
