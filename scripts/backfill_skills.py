"""Derive ``vacancy_skill`` and ``vacancy.min_years`` for vacancies already stored.

    uv run python scripts/backfill_skills.py
    uv run python scripts/backfill_skills.py --dry-run
    uv run python scripts/backfill_skills.py --dry-run --examples 10

The crawl now does this as it writes, so this is for the corpus collected before
it did. It is not a one-off: the derivation is a pure function of a payload and
a description that are already in the database, so whenever that function learns
something — another spelling that is really a language, a synonym the dictionary
picks up, a sentence the text reader now understands — running this again applies
it to every row without fetching a single page. At the rate hh tolerates,
re-crawling 1958 postings would take some two and a half hours; this takes about
a second.

Re-running is safe: skills are replaced, not merged, so a second pass over
unchanged payloads produces the same rows.

**Two sources, counted apart.** Since ``0014_requirement_source`` a row records
whether the employer named the skill in hh's structured field or whether it was
read out of their description, and the report prints the split. That is the
measurement the change is judged on, so it is printed even when it is boring:
"the descriptions added nothing" is a result about this corpus, not a bug.

**To see the effect on the buckets**, run the scoring pass either side of this
one — the numbers it prints are the distribution ``docs/MATCHING.md`` defines:

    uv run python scripts/run_matching.py --dry-run     # before
    uv run python scripts/backfill_skills.py
    uv run python scripts/run_matching.py               # after

``--examples N`` prints what was read out of N live descriptions and, beside it,
the requirement lines that produced nothing. The second half is the honest one:
it is where the dictionary's gaps are visible, and it is what an argument for
adding a spelling has to be built from.
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import configure_logging
from app.db.enums import RequirementSource
from app.db.models import Vacancy, VacancySkill
from app.db.session import session_factory
from app.normalize.description import TextSkills, skills_in_text
from app.normalize.sync import SyncOutcome, sync_requirements

RULE = "-" * 78  # ASCII: this report is printed to a cp1251 console

#: Words that make a sentence a requirement rather than a description of the
#: office. Used only to choose which unmatched lines are worth showing: a miss
#: in a requirements line is worth reading, one in a line about team-building
#: at the company's expense is not.
REQUIREMENT_WORDS: tuple[str, ...] = (
    "требован",
    "требуется",
    "знание",
    "опыт",
    "владение",
    "умение",
    "навыки",
    "стек",
    "будет плюсом",
)

#: How much of a line to print. The console is 78 columns and a hh list item
#: runs long.
LINE = 66

#: Unmatched lines shown per example. Enough to see what the dictionary is
#: missing, few enough that ten examples still fit on a screen.
MISSED_LINES = 3

#: Below this a line is a heading rather than a requirement — hh's descriptions
#: put «Требования:» on a line of its own, and printing it as something the
#: reader missed says nothing about the dictionary.
SHORTEST_LINE = 16


@dataclass(frozen=True, slots=True)
class Example:
    """One live description, what was read out of it, and what was not."""

    title: str
    found: TextSkills
    missed: tuple[str, ...]


def parse_args() -> argparse.Namespace:
    """Command line."""
    parser = argparse.ArgumentParser(description="Fill vacancy_skill from stored payloads.")
    parser.add_argument(
        "--dry-run", action="store_true", help="derive everything, write nothing, print the counts"
    )
    parser.add_argument(
        "--examples",
        type=int,
        default=0,
        metavar="N",
        help="print what was read out of N descriptions that carry no key_skills",
    )
    return parser.parse_args()


async def run(*, dry_run: bool, examples: int) -> tuple[SyncOutcome, int, int, list[Example]]:
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
        shown = await _examples(session, examples) if examples > 0 else []
    return outcome, vacancies or 0, years or 0, shown


async def _examples(session: AsyncSession, limit: int) -> list[Example]:
    """Live descriptions of postings whose employer left the skills field empty.

    Those are the vacancies this whole change is about, so they are the ones
    worth looking at: the ones with a filled field were already scoreable.
    """
    stated = select(VacancySkill.id).where(
        VacancySkill.vacancy_id == Vacancy.id,
        VacancySkill.source == RequirementSource.EMPLOYER_FIELD,
    )
    rows = (
        await session.execute(
            select(Vacancy.id, Vacancy.title, Vacancy.description_raw)
            .where(Vacancy.description_raw.is_not(None), ~stated.exists())
            .order_by(Vacancy.first_seen_at.desc(), Vacancy.id)
            .limit(limit)
        )
    ).all()

    shown: list[Example] = []
    for _, title, description in rows:
        text = "\n".join(part for part in (title, description) if part)
        found = skills_in_text(text)
        shown.append(Example(title=title, found=found, missed=_missed(text, found)))
    return shown


def _missed(text: str, found: TextSkills) -> tuple[str, ...]:
    """Requirement-shaped lines the reader took nothing from.

    Approximate on purpose: a line naming a skill that was found elsewhere in
    the same line is not a miss, and a line about the office is not a
    requirement. What survives both filters is the thing worth arguing about.
    """
    named = {mention.sentence for mention in found.mentions}
    missed: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        folded = stripped.casefold()
        if not stripped or stripped in named or len(stripped) < SHORTEST_LINE:
            continue
        if not any(word in folded for word in REQUIREMENT_WORDS):
            continue
        if any(stripped in sentence for sentence in named):
            continue
        missed.append(stripped[:LINE])
        if len(missed) >= MISSED_LINES:
            break
    return tuple(missed)


def show(
    outcome: SyncOutcome,
    vacancies: int,
    years: int,
    *,
    dry_run: bool,
    examples: list[Example] | None = None,
) -> None:
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
    print("  ОТКУДА ТРЕБОВАНИЯ")
    print(f"    названо работодателем   {outcome.from_field}")
    print(f"    найдено в описании      {outcome.from_text}")
    print(f"    из них необязательных   {outcome.optional_from_text}")
    # The two numbers the change is judged on: how many postings left the field
    # empty, and how many of those got requirements out of their own text.
    print(f"  поле навыков пустое       {outcome.without_field_skills}")
    print(f"  из них спасено описанием  {outcome.rescued_by_text}")
    # Counted, never written. Whether the negation rules earn their keep is a
    # question about this corpus, and this is the number that answers it.
    print(f"  упоминаний с отрицанием   {outcome.negated_in_text}")
    print()
    print(f"  в базе: вакансий с навыками {vacancies}, с указанным опытом {years}")
    if examples:
        _show_examples(examples)
    print(RULE)


def _show_examples(examples: list[Example]) -> None:
    """What ten live descriptions gave up, and what they did not."""
    print()
    print(RULE)
    print("ЧТО ПРОЧИТАНО В ОПИСАНИЯХ БЕЗ KEYSKILLS")
    print(RULE)
    for number, example in enumerate(examples, start=1):
        print(f"  {number}. {example.title[:LINE]}")
        if example.found.required:
            print(f"     требования: {', '.join(example.found.required)}")
        if example.found.optional:
            print(f"     плюсом:     {', '.join(example.found.optional)}")
        if example.found.negated:
            print(f"     отброшено:  {', '.join(example.found.negated)}")
        if not example.found.names:
            print("     ничего не найдено")
        for line in example.missed:
            print(f"     мимо:       {line}")


async def main() -> int:
    """Run it."""
    args = parse_args()
    configure_logging()
    outcome, vacancies, years, examples = await run(dry_run=args.dry_run, examples=args.examples)
    show(outcome, vacancies, years, dry_run=args.dry_run, examples=examples)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
