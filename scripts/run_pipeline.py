"""Run one crawl and print what it did.

    uv run python scripts/run_pipeline.py
    uv run python scripts/run_pipeline.py --dry-run
    uv run python scripts/run_pipeline.py --source arbeitnow --source remotive

The endpoint does the same thing; this exists because the interesting output of
a run is a table, and a person checking whether a source works wants it in a
terminal rather than as JSON. It also prints the two numbers that decide whether
the run was worth making — credits spent, and how many vectors were skipped
because nothing had changed.

No credential is ever printed. A source that is not configured is reported by
the NAMES of the keys it wants, which is what its own ``unavailable()`` carries.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.logging import configure_logging
from app.db.repositories.source_quota import SourceQuotaRepository
from app.db.session import session_factory
from app.pipeline.runner import RunReport, run_pipeline
from app.sources.registry import all_sources

RULE = "─" * 78


def parse_args() -> argparse.Namespace:
    """Command line."""
    parser = argparse.ArgumentParser(description="Crawl the enabled sources once.")
    parser.add_argument("--source", action="append", dest="sources", help="limit to this slug")
    parser.add_argument("--dry-run", action="store_true", help="decide everything, fetch nothing")
    parser.add_argument("--force", action="store_true", help="skip a short cooldown")
    return parser.parse_args()


async def report_quota() -> dict[str, tuple[int, int | None]]:
    """Credits spent today, per metered source."""
    async with session_factory() as session:
        quotas = SourceQuotaRepository(session)
        return {
            source.slug: (await quotas.used_today(source.slug), source.daily_quota)
            for source in all_sources()
            if source.daily_quota is not None
        }


def show(report: RunReport, quota: dict[str, tuple[int, int | None]]) -> None:
    """Print the run, in the order a person reads it."""
    print(RULE)
    print("ПЛАН")
    print(RULE)
    print(f"  {report.plan.summary}")
    if report.plan.groups:
        print(f"  группы: {', '.join(report.plan.groups)}")

    print()
    print(RULE)
    print("ИСТОЧНИКИ")
    print(RULE)
    print(
        f"  {'источник':<12} {'найдено':>8} {'новых':>7} {'обновл':>7} "
        f"{'дублей':>7} {'запросов':>9} {'сек':>7}"
    )
    for outcome in sorted(report.sources, key=lambda item: item.slug):
        if outcome.skipped is not None:
            print(f"  {outcome.slug:<12} пропущен: {outcome.skipped.code.value}")
            print(f"  {'':<12}   {outcome.skipped.detail}")
            continue
        print(
            f"  {outcome.slug:<12} {outcome.found:>8} {outcome.new:>7} {outcome.updated:>7} "
            f"{outcome.duplicates:>7} {outcome.requests:>9} {outcome.duration_seconds:>7.1f}"
        )
        for error in outcome.errors:
            print(f"  {'':<12}   ошибка: {error.get('error')}: {error.get('detail')}")

    if quota:
        print()
        print("  кредиты за сегодня:")
        for slug, (used, limit) in sorted(quota.items()):
            print(f"    {slug:<12} {used} из {limit}")

    print()
    print(RULE)
    print("ЭМБЕДДИНГИ")
    print(RULE)
    if report.embedding is None:
        print("  шаг не выполнялся (dry-run)")
    elif report.embedding.skipped_reason:
        print(f"  пропущено: {report.embedding.skipped_reason}")
    else:
        step = report.embedding
        print(
            f"  рассмотрено {step.considered}, "
            f"не изменилось {step.unchanged}, посчитано {step.embedded}"
        )

    print()
    print(RULE)
    print(
        f"ИТОГО  найдено {report.found}, новых {report.new}, "
        f"дублей схлопнуто {report.duplicates}, за {report.duration_seconds:.1f} с"
    )
    print(RULE)


async def main() -> int:
    """Run it."""
    args = parse_args()
    configure_logging()
    report = await run_pipeline(source_slugs=args.sources, dry_run=args.dry_run, force=args.force)
    show(report, await report_quota())
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
