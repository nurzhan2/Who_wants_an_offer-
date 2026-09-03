"""Experience arithmetic. Pure functions, no database, no LLM.

This module exists because of one failure mode. Ask a model for "total years of
experience" and it adds job durations together, so a student with a part-time
job, a freelance gig and a side project alongside their studies comes out with
twelve years. The model extracts *periods*; the union of those periods is
computed here.

Everything is counted in whole months, as an inclusive index
``year * 12 + (month - 1)``. Months rather than days because resumes state
months, and inclusive because a job that starts and ends in March 2021 is one
month of experience, not zero.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.schemas.llm import WorkPeriod

MONTHS_PER_YEAR = 12
#: The prompt normalises every date to YYYY-MM, but the model is not a parser.
YEAR_MONTH = re.compile(r"^\s*(\d{4})-(\d{1,2})\s*$")


@dataclass(frozen=True, slots=True, order=True)
class MonthInterval:
    """A closed range of months, both ends included."""

    start: int
    end: int

    @property
    def months(self) -> int:
        """How many months the interval covers."""
        return self.end - self.start + 1


@dataclass(frozen=True, slots=True)
class ExperienceSpan:
    """Total experience, plus anything odd noticed while computing it."""

    months: int
    #: Human-readable notes about unusable dates. Never contains resume text
    #: beyond a company name, because these reach the logs.
    warnings: tuple[str, ...] = ()

    @property
    def years(self) -> Decimal:
        """Months as years, one decimal place."""
        return (Decimal(self.months) / MONTHS_PER_YEAR).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )


def month_index(year: int, month: int) -> int:
    """Position of a month on a single monotonic scale."""
    return year * MONTHS_PER_YEAR + (month - 1)


def parse_month(value: str | None) -> int | None:
    """Parse ``YYYY-MM`` into a month index, or None when unusable."""
    if not value:
        return None
    match = YEAR_MONTH.match(value)
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= MONTHS_PER_YEAR:
        return None
    if not 1900 <= year <= 2200:
        return None
    return month_index(year, month)


def intervals_from(
    periods: Sequence[WorkPeriod], *, today: date
) -> tuple[list[MonthInterval], list[str]]:
    """Turn extracted work periods into month intervals.

    ``today`` is a parameter rather than a call to ``date.today()`` so the
    result is reproducible: a test that asserts "still employed means up to
    now" would otherwise change its answer every month.
    """
    now = month_index(today.year, today.month)
    intervals: list[MonthInterval] = []
    warnings: list[str] = []

    for period in periods:
        start = parse_month(period.start)
        if start is None:
            warnings.append(f"{period.company or 'unnamed job'}: unusable start date")
            continue

        end = now if period.is_current else parse_month(period.end)
        if end is None:
            # A job with no end and no "current" flag is a gap in the resume,
            # not a reason to guess. Count the one month we can prove.
            warnings.append(f"{period.company or 'unnamed job'}: no end date, counted as one month")
            end = start
        elif end < start:
            # A transposed range would otherwise subtract experience.
            warnings.append(
                f"{period.company or 'unnamed job'}: end precedes start, counted as one month"
            )
            end = start
        elif end > now:
            warnings.append(
                f"{period.company or 'unnamed job'}: end is in the future, clamped to today"
            )
            end = now

        intervals.append(MonthInterval(start=start, end=end))

    return intervals, warnings


def merge_intervals(intervals: Iterable[MonthInterval]) -> list[MonthInterval]:
    """Collapse overlapping and adjacent intervals into their union.

    Adjacent months merge too: February to March followed by April to May is four
    continuous months, and keeping them apart would only complicate the caller.
    A genuine career break stays a break, because its intervals are not
    adjacent.
    """
    ordered = sorted(intervals)
    merged: list[MonthInterval] = []
    for interval in ordered:
        if merged and interval.start <= merged[-1].end + 1:
            last = merged[-1]
            if interval.end > last.end:
                merged[-1] = MonthInterval(start=last.start, end=interval.end)
            continue
        merged.append(interval)
    return merged


def union_months(intervals: Iterable[MonthInterval]) -> int:
    """Total months covered, counting overlapping time once."""
    return sum(interval.months for interval in merge_intervals(intervals))


def total_experience(periods: Sequence[WorkPeriod], *, today: date) -> ExperienceSpan:
    """Career length as the union of every job, not their sum."""
    intervals, warnings = intervals_from(periods, today=today)
    return ExperienceSpan(months=union_months(intervals), warnings=tuple(warnings))


def experience_with(
    periods: Sequence[WorkPeriod], companies: Iterable[str], *, today: date
) -> ExperienceSpan:
    """How long a skill was actually used, given where it was used.

    The union of the jobs named in ``companies``. Two overlapping jobs that both
    used PostgreSQL do not make two separate stretches of PostgreSQL experience.
    """
    wanted = {name.strip().casefold() for name in companies if name.strip()}
    if not wanted:
        return ExperienceSpan(months=0)
    relevant = [p for p in periods if (p.company or "").strip().casefold() in wanted]
    intervals, warnings = intervals_from(relevant, today=today)
    return ExperienceSpan(months=union_months(intervals), warnings=tuple(warnings))


def last_used_year(
    periods: Sequence[WorkPeriod], companies: Iterable[str], *, today: date
) -> int | None:
    """Calendar year a skill was last used, for staleness."""
    span_end: int | None = None
    wanted = {name.strip().casefold() for name in companies if name.strip()}
    now = month_index(today.year, today.month)

    for period in periods:
        if (period.company or "").strip().casefold() not in wanted:
            continue
        end = now if period.is_current else parse_month(period.end)
        if end is None:
            end = parse_month(period.start)
        if end is not None and (span_end is None or end > span_end):
            span_end = end

    if span_end is None:
        return None
    return span_end // MONTHS_PER_YEAR
