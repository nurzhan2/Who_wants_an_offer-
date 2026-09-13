"""Compute the vectors the database is missing, without crawling anything.

    uv run python scripts/embed_backlog.py
    uv run python scripts/embed_backlog.py --max 500 --seconds 600
    uv run python scripts/embed_backlog.py --until-drained

This exists because until now there was no way to do it. The embedding step ran
only as the tail of ``scripts/run_pipeline.py``, so catching up on rows already
stored meant first sitting through a full crawl — twenty minutes of it for hh
alone — and any interruption anywhere in that hour threw the vectors away. The
step commits every batch now, so this script is safe to stop: whatever it has
written stays written and the next invocation resumes behind it.

The output is the one number the run report could not previously answer: how
much is left.

No box-drawing characters and no emoji anywhere in this file, output included.
The console this runs on encodes cp1251, where U+2500 and friends have no
mapping at all, so one of them raises UnicodeEncodeError at the first print —
after the work, which is the worst possible place to lose a report.
"""

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import settings
from app.core.logging import configure_logging
from app.db.session import session_factory
from app.pipeline.embedding import EmbeddingOutcome, embed_pending, embed_pending_titles

RULE = "-" * 72

#: Why the loop stopped, in the words a person reading a terminal wants.
STOPPED: dict[str, str] = {
    "drained": "очередь пуста, все векторы посчитаны",
    "budget": "исчерпан бюджет прохода (--max или --seconds)",
    "starved": (
        "окно выборки целиком занято строками, которым вектор не нужен; "
        "то, что за ними, отсюда не видно"
    ),
    "unavailable": "модель недоступна",
}


def parse_args() -> argparse.Namespace:
    """Command line."""
    parser = argparse.ArgumentParser(description="Embed the vacancies that have no vector yet.")
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help=(
            "vectors per pass (default: settings.embedding_max_per_run="
            f"{settings.embedding_max_per_run})"
        ),
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        help=(
            "wall-clock budget per pass (default: "
            f"settings.embedding_time_budget_seconds={settings.embedding_time_budget_seconds:.0f})"
        ),
    )
    parser.add_argument(
        "--until-drained",
        action="store_true",
        help="keep starting passes while a pass still has work and makes progress",
    )
    return parser.parse_args()


def show(outcome: EmbeddingOutcome, *, index: int) -> None:
    """Print one pass, in the order a person reads it.

    ``осталось`` is the step's counted remainder, so it is printed flat. It used
    to be prefixed with "не менее" whenever the step could not see past its own
    window, which on the default settings was every large backlog — and the
    number behind that hedge was zero.
    """
    print(RULE)
    print(f"ПРОХОД {index}")
    print(RULE)
    print(f"  рассмотрено      {outcome.considered}")
    print(f"  не изменилось    {outcome.unchanged}")
    print(f"  посчитано        {outcome.embedded}")
    print(f"  коммитов         {outcome.batches}")
    print(f"  осталось         {outcome.backlog}")
    print(f"  остановлено      {STOPPED.get(outcome.stopped, outcome.stopped)}")
    if outcome.skipped_reason:
        print(f"  причина          {outcome.skipped_reason}")


async def one_pass(args: argparse.Namespace) -> EmbeddingOutcome:
    """One call to the step, in a session of its own."""
    async with session_factory() as session:
        return await embed_pending(session, limit=args.max, time_budget=args.seconds)


#: How :func:`drain` runs a pass. Injected so the loop can be tested without a
#: database, a model, or an hour.
type PassRunner = Callable[[argparse.Namespace], Awaitable[EmbeddingOutcome]]


async def drain(args: argparse.Namespace, *, run_pass: PassRunner = one_pass) -> int:
    """Run passes until there is nothing left to do, and return the exit code.

    Three ways to stop, and the difference between them is the whole point of
    the script: the work is done, the model is missing, or the budget ran out
    and a human should start it again.
    """
    index = 0
    while True:
        index += 1
        outcome = await run_pass(args)
        show(outcome, index=index)
        if outcome.stopped == "unavailable":
            print()
            print("Векторы не посчитаны: модель недоступна. Установите её командой")
            print("  uv sync --extra embeddings")
            print('или переключитесь на EMBEDDING_PROVIDER="fake" для работы без семантики.')
            return 1
        if not args.until_drained or outcome.stopped != "budget" or outcome.backlog == 0:
            break
        if outcome.embedded == 0:
            # A pass that spent its budget without writing anything would repeat
            # itself forever. Stop and say so rather than spin.
            print()
            print("Проход не посчитал ни одного вектора. Останавливаюсь.")
            break

    print()
    # The counted remainder, not the stop reason. A pass that drains the backlog
    # and lands on its row cap in the same breath stops for "budget" with
    # nothing left, and telling that operator to run it again is how a finished
    # job looks unfinished forever.
    if outcome.backlog == 0:
        print("Готово: непосчитанных векторов не осталось.")
        return 0
    if outcome.stopped == "starved":
        print(f"Осталось непосчитанных векторов: {outcome.backlog}, но выборка их не отдаёт.")
        print("Повторный запуск ничего не изменит: нужен другой порядок в needs_embedding().")
        return 0
    print(f"Осталось непосчитанных векторов: {outcome.backlog}. Запустите скрипт ещё раз.")
    return 0


async def titles(args: argparse.Namespace) -> EmbeddingOutcome:
    """One pass over title vectors, in a session of its own.

    Titles are embedded apart from descriptions since ``0015_title_embedding``
    and are matching's main signal. They run after the descriptions, and cost a
    fraction of them: a title is a few words, a description thousands.
    """
    async with session_factory() as session:
        return await embed_pending_titles(session, limit=args.max, time_budget=args.seconds)


async def main() -> int:
    """Drain, or try to, then do the same for titles."""
    args = parse_args()
    configure_logging()
    code = await drain(args)
    if code != 0:
        return code
    outcome = await titles(args)
    print()
    print(RULE)
    print("НАЗВАНИЯ")
    print(RULE)
    print(f"  посчитано        {outcome.embedded}")
    print(f"  осталось         {outcome.backlog}")
    print(f"  остановлено      {STOPPED.get(outcome.stopped, outcome.stopped)}")
    if outcome.skipped_reason:
        print(f"  причина          {outcome.skipped_reason}")
    return 1 if outcome.stopped == "unavailable" else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
