"""Derive ``vacancy_skill`` and ``vacancy.min_years`` for vacancies already stored.

    uv run python scripts/backfill_skills.py
    uv run python scripts/backfill_skills.py --dry-run

The crawl now does this as it writes, so this is for the corpus collected before
it did. It is not a one-off: the derivation is a pure function of a payload that
is already in the database, so whenever that function learns something — another
spelling that is really a language, a synonym the dictionary picks up — running
this again applies it to every row without fetching a single page. At the rate hh
tolerates, re-crawling 643 postings would take some fifty minutes; this takes
about a second.

Re-running is safe: skills are replaced, not merged, so a second pass over
unchanged payloads produces the same rows.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import func, select

from app.core.logging import configure_logging
from app.db.models import Vacancy, VacancySkill
from app.db.session import session_factory
from app.normalize.sync import SyncOutcome, sync_requirements

RULE = "-" * 78  # ASCII: this report is printed to a cp1251 console


def parse_args() -> argparse.Namespace:
    """Command line."""
    parser = argparse.ArgumentParser(description="Fill vacancy_skill from stored payloads.")
    parser.add_argument(
        "--dry-run", action="store_true", help="derive everything, write nothing, print the counts"
    )
    return parser.parse_args()


async def run(*, dry_run: bool) -> tuple[SyncOutcome, int, int]:
    """Derive over the whole corpus and count what is there afterwards."""
    async with session_factory() as session:
        outcome = await sync_requirements(session)
        if dry_run:
            await session.rollback()
        else:
            await session.commit()
        vacancies = await session.scalar(select(func.count(func.distinct(VacancySkill.vacancy_id))))
        years = await session.scalar(
            select(func.count()).select_from(Vacancy).where(Vacancy.min_years.is_not(None))
        )
    return outcome, vacancies or 0, years or 0


def show(outcome: SyncOutcome, vacancies: int, years: int, *, dry_run: bool) -> None:
    """Print it, in the order a person reads it."""
    print(RULE)
    print("БЭКФИЛЛ НАВЫКОВ" + ("  (ничего не записано, --dry-run)" if dry_run else ""))
    print(RULE)
    print(f"  вакансий просмотрено      {outcome.considered}")
    print(f"  строк навыков записано    {outcome.skills_written}")
    print(f"  опыт проставлен           {outcome.years_written}")
    # Not a failure: on hh the key-skills field is optional and most employers
    # leave it empty. Printed so that a run which derived nothing is visibly
    # different from a run that found nothing to derive from.
    print(f"  без навыков в данных      {outcome.without_skills}")
    print()
    print(f"  в базе: вакансий с навыками {vacancies}, с указанным опытом {years}")
    print(RULE)


async def main() -> int:
    """Run it."""
    args = parse_args()
    configure_logging()
    outcome, vacancies, years = await run(dry_run=args.dry_run)
    show(outcome, vacancies, years, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
