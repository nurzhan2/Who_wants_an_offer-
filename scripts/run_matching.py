"""Score every stored vacancy against the active profile and print what happened.

    uv run python scripts/run_matching.py
    uv run python scripts/run_matching.py --dry-run
    uv run python scripts/run_matching.py --limit 50
    uv run python scripts/run_matching.py --dry-run --unstated fixed
    uv run python scripts/run_matching.py --dry-run --formula components

``wwao match`` wraps this. The formula is ``docs/MATCHING.md``; the deviations
the data forced are documented in ``app/matching/rules.py`` and named in this
report, because a score whose method is invisible is a number nobody can argue
with.

The profile is embedded first if it has no vector, and its headline likewise.
Both similarities are evidence under the default title formula, and a profile
missing either vector makes that signal unmeasurable for every vacancy at once
— which the scorer would report honestly, and which would still be the wrong
answer when one call fixes it. Title vectors for vacancies come from
``scripts/embed_backlog.py``, not from here: they are thousands of model calls.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.logging import configure_logging
from app.db.session import session_factory
from app.matching import profile_vectors
from app.matching.rules import DEFAULT_FORMULA, DEFAULT_UNSTATED, Formula, UnstatedRequirement
from app.matching.scorer import ProfileNotReadyError, ScoringOutcome, score_corpus

RULE = "-" * 78  # ASCII: this report is printed to a cp1251 console

#: The order buckets are printed in, best first. Matches docs/MATCHING.md.
BUCKET_ORDER: tuple[tuple[str, str], ...] = (
    ("apply_now", "откликаться"),
    ("strong", "сильное совпадение"),
    ("stretch", "на вырост"),
    ("skip", "мимо"),
    ("filtered", "отфильтровано"),
)


def parse_args() -> argparse.Namespace:
    """Command line."""
    parser = argparse.ArgumentParser(description="Score the corpus against the active profile.")
    parser.add_argument("--dry-run", action="store_true", help="score everything, write nothing")
    parser.add_argument("--limit", type=int, default=None, help="score only the newest N vacancies")
    parser.add_argument(
        "--no-embed-profile",
        action="store_true",
        help="do not embed the profile even if it has no vector",
    )
    parser.add_argument(
        "--unstated",
        choices=[rule.value for rule in UnstatedRequirement],
        default=DEFAULT_UNSTATED.value,
        help=(
            "what the one requirement the employer did not write down weighs: "
            "mean-weight (the mean of this vacancy's own weights) or fixed (1.0)"
        ),
    )
    parser.add_argument(
        "--formula",
        choices=[formula.value for formula in Formula],
        default=DEFAULT_FORMULA.value,
        help=(
            "title (title against headline 0.7, description 0.3) or components "
            "(the six weighted components, kept for comparison)"
        ),
    )
    return parser.parse_args()


async def ensure_profile_embedding(*, allowed: bool) -> str | None:
    """Embed the active profile if it has no vector. Returns a line for the report.

    The work lives in :mod:`app.matching.profile_vectors`, where the dashboard's
    rescoring button reaches it too.
    """
    async with session_factory() as session:
        return await profile_vectors.ensure_profile_embedding(session, allowed=allowed)


async def run(
    *,
    dry_run: bool,
    limit: int | None,
    unstated: UnstatedRequirement = DEFAULT_UNSTATED,
    formula: Formula = DEFAULT_FORMULA,
) -> ScoringOutcome:
    """Score, and keep it only if this is not a rehearsal."""
    async with session_factory() as session:
        outcome = await score_corpus(session, limit=limit, unstated=unstated, formula=formula)
        if dry_run:
            await session.rollback()
        else:
            await session.commit()
    return outcome


def show(outcome: ScoringOutcome, note: str | None, *, dry_run: bool) -> None:
    """Print it, in the order a person reads it."""
    print(RULE)
    print("СКОРИНГ" + ("  (ничего не записано, --dry-run)" if dry_run else ""))
    print(RULE)
    if note:
        print(f"  {note}")
        print()
    # Named every time: bucket counts from two passes are only comparable when
    # the reader can see which rule weighed the unwritten requirement.
    print(f"  формула                  {outcome.formula.value}")
    print(f"  ненаписанное требование  {outcome.unstated.value}")
    print(f"  вакансий рассмотрено   {outcome.considered}")
    print(f"  записано match-строк   {outcome.written}")
    print(f"  сидовых пропущено      {outcome.skipped_seeds}")
    print()
    print("  БАКЕТЫ")
    for key, label in BUCKET_ORDER:
        print(f"    {label:<20} {outcome.buckets.get(key, 0)}")
    print()
    # The two numbers that say how much of the formula actually ran. Printed
    # every time rather than only when they are non-zero: a reader comparing
    # two runs needs to see the coverage change, not guess at it.
    print("  ЧЕГО НЕ ХВАТИЛО ДАННЫХ")
    print(f"    без эмбеддинга       {outcome.without_embedding}")
    print(f"    без вектора названия {outcome.without_title_embedding}")
    print(f"    без указанных навыков {outcome.without_skills}")
    print(RULE)


async def main() -> int:
    """Run it."""
    args = parse_args()
    configure_logging()
    try:
        note = await ensure_profile_embedding(allowed=not args.no_embed_profile)
        outcome = await run(
            dry_run=args.dry_run,
            limit=args.limit,
            unstated=UnstatedRequirement(args.unstated),
            formula=Formula(args.formula),
        )
    except ProfileNotReadyError as error:
        # The CLI already sets this example: when a link is missing, say which
        # one rather than returning an empty result as a success.
        print(RULE)
        print("СКОРИНГ НЕ ВЫПОЛНЕН")
        print(RULE)
        print(f"  {error}")
        print(RULE)
        return 1
    show(outcome, note, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
