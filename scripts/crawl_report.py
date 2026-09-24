"""What the last crawl did, on one screen, for the morning after.

    uv run python scripts/crawl_report.py
    python -m wwao report

A night run is eight hours long and nobody watches it. By breakfast the
terminal has been closed, the log lines are in a file nobody greps, and the
question the owner actually has is short: how much did it get, in which cities,
where did it stop, and did hh ask for a human.

``scripts/run_pipeline.py`` prints the run it just made. This prints the run
that already happened, out of the database, and it is deliberately the same
facts in the same order so that the two read alike.

Nothing here decides anything. Every number comes from a connector describing
its own stored state — ``describe_last_run`` and ``describe_position``, both of
which live in ``sources/`` for CLAUDE.md rule 5 — so a source added tomorrow
appears on this screen without this file being edited, and a source that keeps
no position is simply absent from it.
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.logging import configure_logging
from app.db.repositories.source_state import SourceStateRepository
from app.db.session import session_factory
from app.schemas.crawl import CrawlPosition, CrawlRunSummary, CrawlStop
from app.sources.base import BaseSource
from app.sources.registry import all_sources

RULE = "-" * 78  # ASCII: this report is printed to a cp1251 console

#: Why the run ended, as the owner reads it. The stored value is a token, so
#: the wording can change here without two spellings of one fact in the table.
STOPPED: dict[CrawlStop, str] = {
    CrawlStop.CORPUS: "обошли всё, что было не пройдено",
    CrawlStop.TIME: "закончилось отведённое время",
    CrawlStop.PAGES: "закончился бюджет страниц",
    CrawlStop.CHALLENGE: "проверка на робота",
    CrawlStop.INTERRUPTED: "прогон оборвался — сеть, сон машины или сбой",
}

#: The comparison the night work was authorised on: what the old budget bought.
#: Kept as the measured pair rather than as a rate, because the rate is not the
#: interesting number — the pair is what the owner decided against.
BASELINE_MINUTES = 90.0
BASELINE_PAGES = 961


def parse_args() -> argparse.Namespace:
    """Command line."""
    parser = argparse.ArgumentParser(description="Что сделал последний обход.")
    parser.add_argument(
        "--files",
        action="store_true",
        help="показать позицию по каждому файлу карты сайта, а не только по городам",
    )
    return parser.parse_args()


async def collect() -> list[tuple[BaseSource, CrawlRunSummary | None, list[CrawlPosition]]]:
    """Every source's last run and current position, as the source describes them."""
    async with session_factory() as session:
        states = SourceStateRepository(session)
        gathered = []
        for source in all_sources():
            stored = await states.all_for(source.slug)
            if not stored:
                continue
            gathered.append(
                (source, source.describe_last_run(stored), source.describe_position(stored))
            )
        return gathered


def _hm(seconds: float) -> str:
    """Seconds as ``7 ч 02 мин``. Hours, because a night is measured in them."""
    minutes = round(seconds / 60.0)
    return f"{minutes // 60} ч {minutes % 60:02d} мин"


def show_run(summary: CrawlRunSummary) -> None:
    """The run itself: how long it had, what it spent, where it stopped."""
    elapsed = (summary.finished_at - summary.started_at).total_seconds()
    crawling = max(0.0, elapsed - summary.paused_seconds)
    print(
        f"  {summary.started_at.astimezone():%d.%m %H:%M} -> "
        f"{summary.finished_at.astimezone():%d.%m %H:%M}   {_hm(elapsed)}"
    )
    budget = f"{summary.pages} страниц"
    if summary.minutes is not None:
        budget += f" / {_hm(summary.minutes * 60.0)}"
    print(f"  бюджет: {budget}")
    print(f"  остановились: {STOPPED.get(summary.stopped_by, summary.stopped_by.value)}")
    print(f"  куплено {summary.fetched} страниц, сложено {summary.stored}")
    if summary.paused_seconds:
        print(f"  из них в паузе после проверки на робота: {_hm(summary.paused_seconds)}")

    print()
    print(f"  {'город':<16} {'куплено':>8} {'сложено':>8} {'по ролям':>9} {'осталось':>9}  дошли")
    for city in summary.cities:
        print(
            f"  {(city.title or city.scope):<16} {city.fetched:>8} {city.stored:>8} "
            f"{city.role_hits:>9} {city.outstanding:>9}  {'да' if city.finished else 'нет'}"
        )

    if summary.challenges:
        print()
        print("  ПРОВЕРКА НА РОБОТА")
        for check in summary.challenges:
            went = "переждали и вернулись" if check.resumed else "прогон остановлен"
            print(
                f"    {check.at.astimezone():%d.%m %H:%M}  {check.scope}  "
                f"после {check.after_pages} страниц  -> {went}"
            )

    # The projection the night budget was argued on, recomputed from what this
    # run actually did rather than from the pace in the config: a page costs a
    # request plus whatever hh took to answer, and the only honest source for
    # that is a run that happened.
    if crawling > 60 and summary.fetched:
        per_hour = summary.fetched / (crawling / 3600.0)
        print()
        print("  ЧТО ДАЁТ ДОЛГИЙ ПРОГОН")
        print(f"    темп этого прогона: {per_hour:.0f} страниц в час")
        print(
            f"    90 минут ~ {per_hour * 1.5:.0f} страниц "
            f"(замерено на живом hh: {BASELINE_PAGES} за {BASELINE_MINUTES:.0f} минут)"
        )
        print(f"    8 часов  ~ {per_hour * 8:.0f} страниц")
        if summary.outstanding:
            nights = summary.outstanding / max(1.0, per_hour * 8)
            print(f"    осталось {summary.outstanding} — это ещё {nights:.1f} таких ночей")


def show_files(positions: list[CrawlPosition]) -> None:
    """Position per sitemap file. Long, so it is behind a flag."""
    print()
    print(f"  {'город':<16} {'файл':<12} {'всего':>8} {'осталось':>9} {'отрезков':>9}  обновлено")
    for place in positions:
        title = place.title or place.scope
        total = "—" if place.total is None else str(place.total)
        left = "—" if place.outstanding is None else str(place.outstanding)
        when = "—" if place.updated_at is None else f"{place.updated_at.astimezone():%d.%m %H:%M}"
        print(f"  {title:<16} {place.label:<12} {total:>8} {left:>9} {place.stretches:>9}  {when}")


def show(
    gathered: list[tuple[BaseSource, CrawlRunSummary | None, list[CrawlPosition]]],
    *,
    files: bool,
) -> None:
    """Print every source that has something to say about its last walk."""
    printed = False
    for source, summary, positions in gathered:
        if summary is None and not positions:
            continue
        printed = True
        print(RULE)
        print(f"ОБХОД: {source.name}")
        print(RULE)
        if summary is None:
            # A source with a position but no run summary either predates this
            # record or has never finished a walk. Saying so is the answer; a
            # blank screen is not.
            print("  прогон не записан — есть только позиция по файлам")
        else:
            show_run(summary)
        if files and positions:
            show_files(positions)
        elif positions:
            covered = sum(1 for place in positions if place.stretches)
            print()
            print(
                f"  файлов карты сайта под наблюдением: {len(positions)}, "
                f"начатых: {covered}   (--files покажет поимённо)"
            )
        print()
    if not printed:
        print("Ни один источник ещё не записал ни позиции, ни прогона.")
        print("Запустите обход: python -m wwao crawl")


async def main() -> int:
    """Read it and print it."""
    args = parse_args()
    configure_logging()
    gathered = await collect()
    print(f"{datetime.now(UTC).astimezone():%d.%m.%Y %H:%M} — последний обход")
    print()
    show(gathered, files=args.files)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
