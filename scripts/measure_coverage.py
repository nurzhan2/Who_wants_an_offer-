"""How many requirements the corpus states, and what coverage that produces.

    uv run python scripts/measure_coverage.py
    uv run python scripts/measure_coverage.py --top 20

Read-only, and written to answer one question that was asked with a number
attached: five postings sat at 87.5-88.6 at the top of the queue for a Python
backend profile — an IBM engineer, a communications engineer, a network
engineer, a structured-cabling designer — and every one of them explained
itself with «совпадает: linux». One requirement, met, read as a perfect match.

So the report is three questions, in the order they have to be answered:

* **how many requirements does a vacancy state at all** — because a coverage
  ratio over a list of one is a ratio nobody should trust;
* **how is coverage distributed** — because "some vacancies score 1.0" and
  "a sixth of the corpus scores 1.0 off a single line" are different problems;
* **what would the assumption change** — the same corpus recomputed for several
  sizes of :data:`app.matching.rules.UNSTATED_REQUIREMENT`, so the number in the
  formula is chosen against the distribution rather than against an intuition.

Nothing here writes, and nothing here scores: it recomputes the coverage
component with the project's own function and leaves ``match`` alone. To see
what a value does to the buckets, score with it and throw the result away:

    uv run python scripts/run_matching.py --dry-run --unstated 0    # before
    uv run python scripts/run_matching.py --dry-run --unstated 1    # after
"""

import argparse
import asyncio
import sys
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import configure_logging
from app.db.enums import RequirementSource
from app.db.models import CandidateProfile, Match, ProfileSkill, Vacancy, VacancySkill
from app.db.session import session_factory
from app.matching.rules import UNSTATED_REQUIREMENT, skill_coverage

RULE = "-" * 78  # ASCII: this report is printed to a cp1251 console

#: Sizes of the assumption to compare, in weight units. Zero is the corpus as it
#: was scored before the assumption existed, and it has to be in the table:
#: a comparison without the state you are leaving is a table of one column.
CANDIDATES: tuple[Decimal, ...] = (
    Decimal("0"),
    Decimal("0.5"),
    Decimal("1.0"),
    Decimal("2.0"),
)

#: Buckets for the requirement-count histogram. A vacancy stating one thing and
#: a vacancy stating ten are the two ends of the question, so they get their own
#: rows rather than being averaged into "a few".
SIZES: tuple[tuple[int, int, str], ...] = (
    (1, 1, "1 требование"),
    (2, 2, "2"),
    (3, 5, "3-5"),
    (6, 10, "6-10"),
    (11, 10_000, "11 и больше"),
)

#: Bands for the coverage histogram, top down: the interesting end is the top.
BANDS: tuple[tuple[Decimal, str], ...] = (
    (Decimal("1"), "= 1.00  полное"),
    (Decimal("0.85"), "0.85-0.99"),
    (Decimal("0.60"), "0.60-0.84"),
    (Decimal("0.25"), "0.25-0.59"),
    (Decimal("0"), "0.01-0.24"),
)


@dataclass(frozen=True, slots=True)
class Requirement:
    """One row of ``vacancy_skill``, as the formula reads it."""

    canonical_name: str
    weight: Decimal
    source: RequirementSource


@dataclass(frozen=True, slots=True)
class Row:
    """One vacancy: what it asks for, and how the profile covers it."""

    vacancy_id: UUID
    title: str
    requirements: tuple[Requirement, ...]
    #: Raw ratio — coverage with nothing assumed — so the report can show what
    #: the old formula said and what each candidate value would say.
    raw: Decimal
    matched: tuple[str, ...]
    missing: tuple[str, ...]
    score: Decimal | None
    bucket: str | None

    @property
    def size(self) -> int:
        """How many requirements the posting states."""
        return len(self.requirements)

    @property
    def from_text(self) -> int:
        """How many of them nobody wrote in a field: read out of the description."""
        return sum(
            1 for item in self.requirements if item.source is RequirementSource.DESCRIPTION_TEXT
        )

    def coverage(self, unstated: Decimal) -> Decimal:
        """The same coverage under a different assumption, without rescoring."""
        weight = sum((item.weight for item in self.requirements), Decimal("0"))
        return self.raw * weight / (weight + unstated) if weight else Decimal("0")


def parse_args() -> argparse.Namespace:
    """Command line."""
    parser = argparse.ArgumentParser(description="Requirement counts and skill coverage.")
    parser.add_argument("--top", type=int, default=10, help="сколько верхних вакансий показать")
    return parser.parse_args()


async def collect(session: AsyncSession) -> tuple[list[Row], str]:
    """Every scored vacancy with requirements, plus the profile they are read against."""
    profile = (
        await session.execute(select(CandidateProfile).where(CandidateProfile.is_active.is_(True)))
    ).scalar_one_or_none()
    if profile is None:
        raise SystemExit("Нет активного профиля: сначала загрузите резюме или seed.")

    held = {
        name: str(level)
        for name, level in (
            await session.execute(
                select(ProfileSkill.canonical_name, ProfileSkill.level).where(
                    ProfileSkill.profile_id == profile.id
                )
            )
        ).all()
    }

    requirements: dict[UUID, list[Requirement]] = {}
    for vacancy_id, name, weight, source in (
        await session.execute(
            select(
                VacancySkill.vacancy_id,
                VacancySkill.canonical_name,
                VacancySkill.weight,
                VacancySkill.source,
            ).where(VacancySkill.is_required.is_(True))
        )
    ).all():
        requirements.setdefault(vacancy_id, []).append(
            Requirement(canonical_name=name, weight=weight, source=source)
        )

    scored = {
        vacancy_id: (score, str(bucket))
        for vacancy_id, score, bucket in (
            await session.execute(
                select(Match.vacancy_id, Match.score, Match.bucket).where(
                    Match.profile_id == profile.id
                )
            )
        ).all()
    }
    titles = {
        vacancy_id: title
        for vacancy_id, title in (await session.execute(select(Vacancy.id, Vacancy.title))).all()
    }

    rows: list[Row] = []
    for vacancy_id, items in requirements.items():
        # ``unstated=0`` is the raw ratio: what the old formula computed, and the
        # number every candidate value below is derived from.
        raw, matched, missing = skill_coverage(
            {item.canonical_name: item.weight for item in items},
            held,
            unstated=Decimal("0"),
        )
        if raw is None:
            continue
        score, bucket = scored.get(vacancy_id, (None, None))
        rows.append(
            Row(
                vacancy_id=vacancy_id,
                title=titles.get(vacancy_id, "?"),
                requirements=tuple(items),
                raw=raw,
                matched=tuple(item.canonical_name for item in matched),
                missing=tuple(item.canonical_name for item in missing),
                score=score,
                bucket=bucket,
            )
        )
    rows.sort(key=lambda row: (row.score is None, -(row.score or Decimal("0"))))
    # ``top`` orders the report rather than truncating it: every number above the
    # head of the queue is about the whole corpus, and a distribution measured
    # over ten rows would answer a different question than the one asked.
    return rows, profile.headline or profile.name or "без имени"


def sizes(rows: list[Row]) -> None:
    """How many requirements a vacancy states, and how many nobody stated."""
    print("  ТРЕБОВАНИЙ НА ВАКАНСИЮ")
    counts = Counter[str]()
    text_only = Counter[str]()
    for row in rows:
        for low, high, label in SIZES:
            if low <= row.size <= high:
                counts[label] += 1
                if row.from_text == row.size:
                    text_only[label] += 1
                break
    for _, _, label in SIZES:
        share = counts[label] * 100 // len(rows) if rows else 0
        print(
            f"    {label:<14} {counts[label]:>5}  ({share:>2}%)  из них целиком из текста: "
            f"{text_only[label]}"
        )
    print(f"    {'всего':<14} {len(rows):>5}")


def coverage(rows: list[Row]) -> None:
    """The distribution the question was really about."""
    print("  ПОКРЫТИЕ НАВЫКОВ, как считалось до этой правки")
    counts = Counter[str]()
    perfect_size = Counter[int]()
    for row in rows:
        if row.raw <= 0:
            counts["= 0.00  ничего"] += 1
            continue
        for floor, label in BANDS:
            if row.raw >= floor:
                counts[label] += 1
                break
        if row.raw >= 1:
            perfect_size[row.size] += 1
    for _, label in BANDS:
        print(f"    {label:<18} {counts[label]:>5}")
    print(f"    {'= 0.00  ничего':<18} {counts['= 0.00  ничего']:>5}")
    if perfect_size:
        print()
        print("    из полного покрытия — по числу требований:")
        for size in sorted(perfect_size):
            print(f"      {size:>3} требование(й)  {perfect_size[size]:>5}")


def assumption(rows: list[Row]) -> None:
    """What each size of the assumption does to the same corpus."""
    print("  ЧТО ДЕЛАЕТ ДОПУЩЕНИЕ О НЕНАЗВАННОМ ТРЕБОВАНИИ")
    print(f"    {'unstated':>8}  {'покрытие 1.0':>12}  {'>= 0.85':>8}  {'среднее':>8}")
    for value in CANDIDATES:
        covers = [row.coverage(value) for row in rows]
        full = sum(1 for item in covers if item >= 1)
        high = sum(1 for item in covers if item >= Decimal("0.85"))
        mean = sum(covers, Decimal("0")) / len(covers) if covers else Decimal("0")
        mark = "  <- сейчас" if value == UNSTATED_REQUIREMENT else ""
        print(f"    {value!s:>8}  {full:>12}  {high:>8}  {mean:>8.2f}{mark}")


def top(rows: list[Row], limit: int) -> None:
    """The head of the queue, with the requirement list behind each score."""
    print(f"  ВЕРХ ОЧЕРЕДИ ПО СОХРАНЁННОМУ СКОРУ: {limit}")
    # Both coverages, because the whole question is the difference between them:
    # the first is the ratio of what was stated, the second is that ratio with
    # one requirement nobody stated in the denominator.
    print(f"    {'скор':>6}  {'треб':>4}  {'было':>5}  {'стало':>5}  вакансия")
    for row in rows[:limit]:
        score = f"{row.score:>6.1f}" if row.score is not None else f"{'--':>6}"
        now = row.coverage(UNSTATED_REQUIREMENT)
        indent = " " * 28
        print(f"    {score}  {row.size:>4}  {row.raw:>5.2f}  {now:>5.2f}  {row.title[:38]}")
        print(f"{indent}совпадает: {', '.join(row.matched) or 'ничего'}")
        if row.missing:
            print(f"{indent}не хватает: {', '.join(row.missing)}")


async def main() -> int:
    """Run it."""
    args = parse_args()
    configure_logging()
    async with session_factory() as session:
        rows, profile = await collect(session)

    print(RULE)
    print(f"ТРЕБОВАНИЯ И ПОКРЫТИЕ   профиль: {profile}")
    print(RULE)
    if not rows:
        print("  Ни одна вакансия не имеет требований. Запустите backfill_skills.py.")
        print(RULE)
        return 0
    sizes(rows)
    print()
    coverage(rows)
    print()
    assumption(rows)
    print()
    top(rows, args.top)
    print(RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
