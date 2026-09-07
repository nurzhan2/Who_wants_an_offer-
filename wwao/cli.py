"""One command for the whole thing, and one process per world.

    python -m wwao crawl              обойти источники
    python -m wwao match              посчитать соответствие профилю
    python -m wwao letters --limit 5  написать письма
    python -m wwao queue              что готово к отклику и почему остальное нет
    python -m wwao apply --send       отклики, по одному, с подтверждением
    python -m wwao outcomes           что hh отвечает на уже отправленное

**Every subcommand is a child process, and that is the design rather than an
implementation detail.** ``backend/`` and ``agent/`` must not meet: the crawler
is anonymous, read-only and runs on a server, the agent acts under a person's
own hh account and sends things, and ``agent/tests/test_isolation.py`` enforces
the separation by parsing the import graph. A single command that can do both
is exactly the thing that could fuse them, so it is built so that it cannot:
this module imports neither world, at import time or later. ``crawl``, ``match``
and ``letters`` start the script that owns the work; ``apply`` starts ``python
-m agent.run`` and ``outcomes`` starts ``python -m agent.outcomes``. The CLI
process itself never loads ``app``, never loads ``agent``, and never loads a
browser driver, whichever subcommand is running.

Lazy imports would have been enough to satisfy the letter of that rule and were
rejected, because the guarantee they give is "nobody wrote the wrong import
yet". A child process makes it a property of how the program runs: there is no
statement anywhere in this package that could load the other side by accident,
and a transitive import three libraries deep cannot do it either. It also means
the machine that crawls overnight never needs the agent's dependencies
installed, which is the same boundary seen from the other end.

**Flags are not re-declared for the wrapped tools.** Everything after ``crawl``,
``match`` or ``letters`` is handed to the script that owns it, so
``wwao crawl --source hh --dry-run`` is ``scripts/run_pipeline.py``'s own
command line and cannot drift from it. ``wwao crawl --help`` prints that
script's help.

**``apply`` is the exception, and its flags are a closed set.** It is the only
subcommand that sends anything, so nothing is forwarded blindly: ``--send``,
``--queue`` and ``--requeue`` are all it accepts, and an unrecognised flag is an
error rather than something passed along. If a way to skip the confirmation is
ever added to the agent, it does not become reachable from here by default —
somebody has to add it to this file, in front of the test that forbids it.

**``apply`` refuses to start without a terminal.** The confirmation is a word
typed in full after reading a card, and a scheduler can neither read the card
nor type the word. ``agent.run`` already treats a closed stdin as a refusal;
this refuses earlier and says why, so that a nightly job fails visibly at the
top instead of opening a browser first. There is no flag, and no environment
variable, that lifts it — this module reads no environment at all — and the
task is explicit that if such a switch starts to look necessary, the task has
been misunderstood.

**``outcomes`` is the third category, and it needs saying because there were
only two.** ``crawl``, ``match``, ``letters`` and ``queue`` need neither a
person nor an account; ``apply`` needs both. ``outcomes`` needs the account and
not the person: it opens the owner's browser and reads one page per application
already sent, so nobody has to answer anything, but it cannot run on a machine
that is not signed in. So it gets ``apply``'s closed flag set — nothing is
forwarded blindly to a subcommand that runs under somebody's login — and not
``apply``'s terminal check, because there is no card to read and no word to
type. It sends nothing, and it cannot: ``agent/outcomes.py`` mints no mandate,
so ``agent/gate.py`` refuses every application-shaped request the browser
makes.

Everything else runs with no human and no account, which is what makes it
runnable overnight.
"""

import argparse
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO, final

from wwao.console import encoding_of, harden, printable
from wwao.queue_view import (
    QueueNotBuiltError,
    QueueUnavailableError,
    QueueView,
    ResultsPayload,
    classify,
    fetch_over_http,
    parse_queue,
    parse_results,
    render,
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
SCRIPTS: Final[Path] = REPO_ROOT / "scripts"

#: The agent, started as a module rather than imported. The one string in this
#: package that names the other world, and it is data, not an import.
AGENT_MODULE: Final[str] = "agent.run"

#: The read-only half of the agent, started the same way and for the same
#: reason. A separate module rather than a flag on the one above, because
#: «прочитать, что ответил hh» and «отправить отклик» must not be two moods of
#: one program that has already been started.
OUTCOMES_MODULE: Final[str] = "agent.outcomes"

#: The queue as it exists today: a JSON file the backend writes and the agent
#: reads. ``--from`` takes an http(s) base URL instead, for the endpoint
#: described in ``agent/queue.py``, once it exists.
DEFAULT_QUEUE: Final[Path] = REPO_ROOT / "agent" / "queue.json"

#: Everything went as asked.
EXIT_OK: Final[int] = 0
#: The step ran and failed, or the data it needed was not there.
EXIT_FAILED: Final[int] = 1
#: This part of the pipeline has not been written yet. Distinct from a failure
#: because the answer is "write it", not "look at the logs".
EXIT_MISSING_PIECE: Final[int] = 3
#: ``apply`` was started where no person could answer it.
EXIT_NO_HUMAN: Final[int] = 4

#: Runs one child process to completion. Injected so tests can watch the exact
#: command line without starting anything.
Runner = Callable[[Sequence[str]], int]
#: Fetches a queue payload as raw text. Injected for the same reason, and it is
#: what keeps the tests off the network.
Fetcher = Callable[[str, int], str]


@final
@dataclass(frozen=True, slots=True)
class Wrapped:
    """A subcommand whose work belongs to a script that already exists.

    The CLI's job for these is to know which file owns the step and to hand over
    the rest of the command line. It deliberately knows nothing about their
    flags: a router that copies the flags of the thing it routes to is a router
    that goes stale.
    """

    name: str
    script: Path
    summary: str
    #: What to say when the script is not in the tree yet. Named per subcommand
    #: because "run this instead" differs, and a generic "file not found" for a
    #: step nobody has written is a puzzle rather than an answer.
    missing: str


WRAPPED: Final[tuple[Wrapped, ...]] = (
    Wrapped(
        name="crawl",
        script=SCRIPTS / "run_pipeline.py",
        summary="обойти источники и сложить вакансии в базу",
        missing="Обход источников живёт в scripts/run_pipeline.py.",
    ),
    Wrapped(
        name="match",
        script=SCRIPTS / "run_matching.py",
        summary="посчитать соответствие вакансий профилю",
        missing=(
            "Скоринга ещё нет: CLI ждёт scripts/run_matching.py — такой же скрипт, как\n"
            "  scripts/run_pipeline.py и scripts/generate_letters.py, запускаемый\n"
            "  «python scripts/run_matching.py». В backend есть таблица match, схемы\n"
            "  app/schemas/match.py и MatchRepository.bulk_upsert; кода, который\n"
            "  считает score, в репозитории нет ни строки.\n"
            "  Пока его нет: letters и queue работают на том, что уже посчитано,\n"
            "  а очередь без score это честно показывает."
        ),
    ),
    Wrapped(
        name="letters",
        script=SCRIPTS / "generate_letters.py",
        summary="написать сопроводительные для верхних по score",
        missing="Генерация писем живёт в scripts/generate_letters.py.",
    ),
)


def _spawn(command: Sequence[str]) -> int:
    """Run one child to completion with the streams it was given.

    Inherited streams rather than pipes, on purpose: ``apply`` needs the person
    to type into the agent's own prompt, and the wrapped scripts print reports
    a person reads as they happen.
    """
    return subprocess.call(list(command), cwd=str(REPO_ROOT))


def build_parser() -> argparse.ArgumentParser:
    """The command line, exposed so a test can walk it.

    The wrapped subcommands are declared with ``add_help=False`` and take no
    options of their own, so ``-h`` and everything else reaches the script that
    owns them.
    """
    parser = argparse.ArgumentParser(
        prog="python -m wwao",
        description="Резюме -> источники -> соответствие -> письма -> отклик.",
        epilog=(
            "Ночью запускаются crawl, match, letters и queue: им не нужен ни человек, "
            "ни аккаунт. apply отправляет отклики и работает только в терминале. "
            "outcomes посередине: аккаунт нужен, человек — нет, отправить он ничего "
            "не может. "
            f"Коды возврата: {EXIT_OK} успех, {EXIT_FAILED} шаг не удался, "
            f"{EXIT_MISSING_PIECE} этой части пайплайна ещё нет, "
            f"{EXIT_NO_HUMAN} apply запущен без человека."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="подкоманда")

    for wrapped in WRAPPED:
        subparsers.add_parser(
            wrapped.name,
            add_help=False,
            help=f"{wrapped.summary} ({wrapped.script.name}; флаги — его собственные)",
        )

    queue = subparsers.add_parser("queue", help="что готово к отклику и почему остальное нет")
    queue.add_argument(
        "--from",
        dest="source",
        default=str(DEFAULT_QUEUE),
        help=(
            "файл очереди или базовый адрес бэкенда "
            "(http://localhost:8000). По умолчанию: %(default)s"
        ),
    )
    queue.add_argument(
        "--results",
        default=None,
        help="файл результатов прошлого прогона; по умолчанию рядом с очередью",
    )
    queue.add_argument("--limit", type=int, default=20, help="сколько вакансий показать")

    apply_ = subparsers.add_parser(
        "apply",
        help="отклики: карточка, подтверждение, отправка. Только из терминала",
        description=(
            "Единственная подкоманда, которая что-то отправляет. Без --send это "
            "сухой прогон: карточки показываются, наружу не уходит ничего. "
            "Подтверждение — слово, набранное руками; флага, который его "
            "заменяет, нет."
        ),
    )
    apply_.add_argument(
        "--send",
        action="store_true",
        help="дойти до подтверждения и отправить (по умолчанию — только показать)",
    )
    apply_.add_argument("--queue", default=None, help="файл очереди для агента")
    apply_.add_argument(
        "--requeue",
        nargs="+",
        metavar="ID",
        default=(),
        help="вернуть вакансии из needs_manual или failed в очередь; ничего не отправляет",
    )

    outcomes = subparsers.add_parser(
        "outcomes",
        help="пройти по отправленным откликам и прочитать, что ответил hh",
        description=(
            "Открывает по одной странице на каждый уже отправленный отклик и "
            "записывает, что hh о нём говорит. Нужен аккаунт, не нужен человек. "
            "Отправить ничего не может: мандата не выдаётся, и шлюз отклоняет "
            "любой запрос, похожий на отклик."
        ),
    )
    outcomes.add_argument("--limit", type=int, default=None, help="сколько откликов обойти за раз")
    outcomes.add_argument(
        "--to",
        default=None,
        metavar="URL",
        help=(
            "дополнительно отправить прочитанное в трекер: базовый адрес бэкенда "
            "(http://localhost:8000). Локальный файл agent/probe/outcomes.json пишется всегда"
        ),
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    run: Runner = _spawn,
    fetch: Fetcher = fetch_over_http,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Route one subcommand and return its exit code.

    The four seams are arguments so that the whole of this can be tested without
    starting a process, without a network and without a terminal — which matters
    most for the one behaviour that must never regress, that ``apply`` will not
    run where nobody can answer it.
    """
    src = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    harden(out)
    harden(err)

    parser = build_parser()
    args, extra = parser.parse_known_args(argv)

    wrapped = {tool.name: tool for tool in WRAPPED}
    if args.command in wrapped:
        return _run_wrapped(wrapped[args.command], extra, run=run, err=err)

    if extra:
        # A closed set of flags, and this is what makes it closed. Forwarding an
        # unknown flag to the agent is how «--yes» would arrive one day.
        parser.error(
            f"подкоманда {args.command} не принимает {' '.join(extra)}. "
            "Её флаги перечислены в --help и это весь список."
        )

    if args.command == "queue":
        return _show_queue(args, fetch=fetch, out=out, err=err)
    if args.command == "outcomes":
        return _outcomes(args, run=run, err=err)
    return _apply(args, run=run, src=src, out=out, err=err)


def _run_wrapped(tool: Wrapped, extra: Sequence[str], *, run: Runner, err: TextIO) -> int:
    """Hand the rest of the command line to the script that owns this step."""
    if not tool.script.is_file():
        print(f"{tool.name}: {tool.missing}", file=err)
        return EXIT_MISSING_PIECE
    return run([sys.executable, str(tool.script), *extra])


def _apply(args: argparse.Namespace, *, run: Runner, src: TextIO, out: TextIO, err: TextIO) -> int:
    """Start the agent, in its own process, with a person watching.

    The guard is first and is not conditional on ``--send``. A dry run prints
    every letter in full and moves nothing, so it is harmless; but "apply needs
    a terminal" is a rule somebody has to be able to hold in their head, and
    "apply needs a terminal unless you left off the flag that sends" is not that
    rule. What runs unattended is ``queue``, which answers the same question
    from files and needs neither the agent's dependencies nor a browser.
    """
    if not _a_person_is_here(src, out):
        print(NO_HUMAN, file=err)
        return EXIT_NO_HUMAN

    agent = REPO_ROOT / "agent" / "run.py"
    if not agent.is_file():
        print(f"apply: агента нет на месте — {agent} не найден.", file=err)
        return EXIT_MISSING_PIECE

    command = [sys.executable, "-m", AGENT_MODULE]
    if args.send:
        command.append("--send")
    if args.queue:
        command += ["--queue", str(args.queue)]
    if args.requeue:
        command += ["--requeue", *args.requeue]
    return run(command)


def _outcomes(args: argparse.Namespace, *, run: Runner, err: TextIO) -> int:
    """Start the read-only walk, in its own process, with nobody watching.

    No terminal check, and the difference from :func:`_apply` is the whole point
    of having two subcommands: there is no card here, nothing is confirmed and
    nothing leaves. What this needs is the account, which is why it is still a
    child process of its own with a closed flag set rather than something the
    unattended half of the pipeline can wander into.
    """
    walker = REPO_ROOT / "agent" / "outcomes.py"
    if not walker.is_file():
        print(f"outcomes: обхода нет на месте — {walker} не найден.", file=err)
        return EXIT_MISSING_PIECE

    command = [sys.executable, "-m", OUTCOMES_MODULE]
    if args.limit is not None:
        command += ["--limit", str(args.limit)]
    if args.to:
        command += ["--to", str(args.to)]
    return run(command)


#: Printed when ``apply`` is started where no person can answer it. Spelled out
#: rather than one line, because the honest answer to "how do I run this from
#: cron" is "you do not", and that needs a reason attached.
NO_HUMAN: Final[str] = (
    "apply не запускается без человека за клавиатурой.\n"
    "\n"
    "Это единственная подкоманда, которая отправляет отклики, и каждый из них\n"
    "подтверждается словом, набранным руками, после чтения карточки: id вакансии,\n"
    "ссылка, компания, соответствие с объяснением, ПОЛНЫЙ текст письма и то, что\n"
    "hh уже сказал про этот отклик. Сейчас stdin или stdout — не терминал (cron,\n"
    "пайп, CI), то есть карточку некому прочитать и подтверждение некому дать.\n"
    "\n"
    "Флага, который это отключает, нет, и переменной окружения тоже нет.\n"
    "Ночью запускаются crawl, match, letters и queue — им не нужны ни человек,\n"
    "ни аккаунт. «python -m wwao queue» показывает, что накопилось к утру."
)


def _a_person_is_here(src: TextIO, out: TextIO) -> bool:
    """Whether somebody can read the card and answer it.

    Both streams, not just stdin. A redirected stdout means the card is written
    to a file nobody is looking at, and a confirmation given without reading the
    letter is the thing the confirmation exists to prevent.

    A stream that refuses to answer at all — closed, detached — counts as
    nobody, which is the same direction every other refusal in this project
    leans.
    """
    try:
        return bool(src.isatty() and out.isatty())
    except (ValueError, AttributeError):
        return False


def _show_queue(args: argparse.Namespace, *, fetch: Fetcher, out: TextIO, err: TextIO) -> int:
    """Read the queue from wherever it is and print what it means.

    No account, no browser, no agent: a file and, one day, one GET. This is the
    unattended half of ``apply`` — the same question, answered without being
    able to act on the answer.
    """
    source: str = args.source
    results_path: Path | None = Path(args.results) if args.results else None
    try:
        if source.startswith(("http://", "https://")):
            raw = fetch(source, args.limit)
        else:
            path = Path(source)
            if not path.is_file():
                print(
                    f"queue: нет файла очереди {path}.\n"
                    "  Пока бэкенд не отдаёт очередь, её формат описан в agent/README.md,\n"
                    "  а адрес эндпоинта — в --from http://localhost:8000",
                    file=err,
                )
                return EXIT_FAILED
            raw = path.read_text(encoding="utf-8")
            # The results of a run are written beside the queue they came from,
            # so a queue read from a file has a known place to look.
            results_path = results_path or _results_beside(path)
        payload = parse_queue(raw)
    except QueueNotBuiltError as error:
        # Nobody has written this yet, or the two sides are on different
        # versions of the contract. Different answer, different exit code.
        print(f"queue: {error}", file=err)
        return EXIT_MISSING_PIECE
    except QueueUnavailableError as error:
        print(f"queue: {error}", file=err)
        return EXIT_FAILED
    except OSError as error:
        print(f"queue: не удалось прочитать очередь: {error}", file=err)
        return EXIT_FAILED

    warnings: list[str] = []
    results = parse_results(results_path) if results_path is not None else ResultsPayload()
    if results_path is not None and not results_path.is_file():
        warnings.append(
            f"результатов прошлых прогонов нет ({results_path.name}) — "
            "«не готово» показано только по тому, что знает краулер"
        )
    view = QueueView(
        source=source,
        rows=classify(payload.items[: args.limit], results),
        results_seen=len(results.results),
        warnings=tuple(warnings),
    )
    print(render(view, encoding=encoding_of(out)), file=out)
    return EXIT_OK


def _results_beside(queue: Path) -> Path:
    """Where a run writes its outcomes: the same name plus ``-results``.

    Kept identical to ``agent.queue.FileQueue``'s own rule, which cannot be
    imported from here — the two ends of this contract do not share code, and
    that is the point of the contract.
    """
    return queue.with_name(f"{queue.stem}-results.json")


def cli() -> int:  # pragma: no cover - the console entry point
    """What ``python -m wwao`` runs."""
    try:
        return main()
    except KeyboardInterrupt:
        print(printable("\nПрервано.", encoding_of(sys.stderr)), file=sys.stderr)
        return EXIT_FAILED
