"""One command for the whole thing, and one process per world.

    python -m wwao crawl              обойти источники
    python -m wwao match              посчитать соответствие профилю
    python -m wwao letters --limit 5  написать письма
    python -m wwao queue              что готово к отклику и почему остальное нет
    python -m wwao apply --send       отклики, по одному, с подтверждением
    python -m wwao outcomes           что hh отвечает на уже отправленное
    python -m wwao watch              выполнять запросы дашборда, которым нужен агент
    python -m wwao up                 поднять базу, сервер и дашборд, затем ждать запросов

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
``--from``, ``--no-backend``, ``--queue`` and ``--requeue`` are all it accepts,
and an unrecognised flag is an error rather than something passed along. The two
naming the queue's source were added deliberately and neither touches consent:
they say which list the agent is offered, not whether a person answers for it.
If a way to skip the confirmation is ever added to the agent, it does not become
reachable from here by default — somebody has to add it to this file, in front
of the test that forbids it.

**``apply`` refuses to start without a terminal.** The confirmation is a word
typed in full after reading a card, and a scheduler can neither read the card
nor type the word. ``agent.run`` already treats a closed stdin as a refusal;
this refuses earlier and says why, so that a nightly job fails visibly at the
top instead of opening a browser first. There is no flag, and no environment
variable, that lifts it — this module reads no environment at all — and the
task is explicit that if such a switch starts to look necessary, the task has
been misunderstood.

**``queue`` asks the database, and reads the hand-written file after it.** The
queue is the backend's answer — vacancies scored above ``agent_queue_min_score``
with a letter written and no application sent — and until 9 September 2026 this
command read ``agent/queue.json`` instead: a file a person edits. So a night of
crawling, scoring and five written letters arrived here as one row typed in
weeks earlier, with no score, and the header named the file rather than saying
what was missing. ``--from`` now defaults to the backend, the file is merged in
behind it and marked as hand-added, and a backend that does not answer is a
warning plus a non-zero exit rather than a silent fall back to the file. Naming
a path in ``--from`` still reads that path alone: it is the transport that needs
no server, no database and no token.

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

**``watch`` is how the dashboard reaches the agent without becoming it.**
Added 2026-09-16, when the browser got buttons for reading outcomes and for
sending what the owner confirmed there. The API records those requests and
never acts on them; this loop, on the owner's machine, claims them over the
token-guarded seam and starts ``agent.outcomes`` or ``agent.run --send
--dashboard`` as children, exactly as ``outcomes`` and ``apply`` do. It takes
``apply``'s terminal check, because a send can meet a captcha or an expired
session that only a person at this window can deal with, and it has a closed
flag set. What it sends is decided by the agent from the confirmations the
owner gave card by card; the watcher only says "go".

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

from wwao import up as stack
from wwao import watch as watch_loop
from wwao.console import encoding_of, harden, printable
from wwao.queue_view import (
    QueueEntry,
    QueueNotBuiltError,
    QueueUnavailableError,
    QueueView,
    ResultsPayload,
    classify,
    fetch_over_http,
    merge,
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

#: Where the queue comes from: the backend, which is the only thing that knows
#: which vacancies are scored, have a letter and have not been applied to.
#: ``--from`` takes a path instead for the day there is no server to ask.
DEFAULT_BACKEND: Final[str] = "http://localhost:8000"

#: The hand-written addition to that queue. It was the whole queue until
#: 2026-09-09, which is why the pipeline's output was invisible here: this file
#: is edited by a person and knows nothing about last night's run.
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
        missing="Скоринг живёт в scripts/run_matching.py.",
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
        default=DEFAULT_BACKEND,
        help=(
            "базовый адрес бэкенда, откуда берётся очередь, или файл очереди "
            "вместо него. По умолчанию: %(default)s"
        ),
    )
    queue.add_argument(
        "--manual",
        default=str(DEFAULT_QUEUE),
        help="файл ручных добавлений к очереди из базы. По умолчанию: %(default)s",
    )
    queue.add_argument(
        "--no-manual",
        action="store_true",
        help="показать только то, что отдал бэкенд",
    )
    queue.add_argument(
        "--results",
        default=None,
        help="файл результатов прошлого прогона; по умолчанию рядом с ручной очередью",
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
    apply_.add_argument(
        "--from",
        dest="backend",
        default=None,
        help="базовый адрес бэкенда, откуда агент берёт очередь",
    )
    apply_.add_argument(
        "--no-backend",
        action="store_true",
        help="агент работает по одному файлу очереди, без базы",
    )
    apply_.add_argument("--queue", default=None, help="файл ручных добавлений к очереди агента")
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
    watch = subparsers.add_parser(
        "watch",
        help="выполнять запросы дашборда, которым нужен агент (исходы, отправка подтверждённого)",
        description=(
            "Ждёт, пока на дашборде нажмут «Обновить исходы» или «Отправить "
            "подтверждённые», и запускает агента на этом компьютере. Отправляется "
            "только то, что человек подтвердил на дашборде карточка за карточкой. "
            "Работает только в окне терминала: если hh покажет проверку на робота, "
            "её проходит человек."
        ),
    )
    watch.add_argument(
        "--from",
        dest="backend",
        default=DEFAULT_BACKEND,
        help="базовый адрес бэкенда. По умолчанию: %(default)s",
    )
    watch.add_argument(
        "--interval",
        type=float,
        default=watch_loop.DEFAULT_INTERVAL,
        help="как часто спрашивать бэкенд, секунд. По умолчанию: %(default)s",
    )
    up = subparsers.add_parser(
        "up",
        help="поднять базу, сервер и дашборд одной командой (то же делает start.cmd)",
        description=(
            "Проверяет Docker, базу, схему, сервер приложения и дашборд, запускает "
            "недостающее и открывает дашборд в браузере. Потом остаётся в этом окне и "
            "выполняет запросы дашборда, которым нужен агент. Закрыть окно — "
            "остановить то, что оно запустило."
        ),
    )
    up.add_argument(
        "--no-browser",
        action="store_true",
        help="не открывать дашборд в браузере",
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
    if args.command == "watch":
        if not _a_person_is_here(src, out):
            print(NO_HUMAN_TO_WATCH, file=err)
            return EXIT_NO_HUMAN
        return watch_loop.watch(
            str(args.backend),
            post=watch_loop.post_over_http,
            run_child=watch_loop.run_with_echo,
            out=out,
            interval=max(1.0, float(args.interval)),
        )
    if args.command == "up":
        return _up(args, src=src, out=out, err=err)
    return _apply(args, run=run, src=src, out=out, err=err)


def _up(
    args: argparse.Namespace,
    *,
    src: TextIO,
    out: TextIO,
    err: TextIO,
    machine: "stack.Machine | None" = None,
) -> int:
    """Bring the stack up, then stay as the watcher until the window closes.

    The terminal check comes first for the watcher's reason: the agent it runs
    may need a person at this window.
    """
    if not _a_person_is_here(src, out):
        print(NO_HUMAN_TO_WATCH, file=err)
        return EXIT_NO_HUMAN
    box = machine if machine is not None else stack.real_machine()
    try:
        code = stack.up(box, out, open_browser=not args.no_browser)
        if code != stack.EXIT_OK:
            print("\nНе всё запустилось — сообщение выше говорит, что сделать.", file=out)
            return code
        return watch_loop.watch(
            stack.API_URL,
            post=watch_loop.post_over_http,
            run_child=watch_loop.run_with_echo,
            out=out,
        )
    finally:
        stack.stop(box, out)


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
    # Where the agent takes its queue from. Forwarded rather than decided here:
    # the agent holds the same default and the same escape hatch, and one
    # address kept in two places is how the two come to disagree.
    if args.backend:
        command += ["--from", str(args.backend)]
    if args.no_backend:
        command.append("--no-backend")
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


#: Printed when ``watch`` is started where no person can look at it.
NO_HUMAN_TO_WATCH: Final[str] = (
    "watch не запускается без окна терминала.\n"
    "\n"
    "Он запускает агента под вашим аккаунтом hh, и если hh покажет проверку на\n"
    "робота или сессия истечёт, разбираться будет человек у этого окна. Запустите\n"
    "его двойным щелчком по start.cmd или командой python -m wwao watch в терминале."
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
    """Read the queue from the backend, add what a person wrote by hand, print it.

    No account, no browser, no agent: one GET and one file. This is the
    unattended half of ``apply`` — the same question, answered without being
    able to act on the answer.

    **A backend that does not answer is a failure, not a fallback.** The file is
    still shown, because rows a person typed are rows somebody wants to see, but
    the report says the database half is missing and the command exits non-zero.
    Quietly showing the file alone is exactly how a month-old hand-written row
    came to be read as this morning's queue.
    """
    source: str = args.source
    if not source.startswith(("http://", "https://")):
        return _show_queue_file(args, Path(source), out=out, err=err)

    warnings: list[str] = []
    failure = EXIT_OK
    primary: list[QueueEntry] = []
    try:
        primary = list(parse_queue(fetch(source, args.limit)).items)
    except QueueNotBuiltError as error:
        # Nobody has written this yet, or the two sides are on different
        # versions of the contract. Different answer, different exit code.
        print(f"queue: {error}", file=err)
        warnings.append("эндпоинта очереди нет — список ниже собран без базы")
        failure = EXIT_MISSING_PIECE
    except QueueUnavailableError as error:
        print(f"queue: {error}", file=err)
        warnings.append("база не ответила — список ниже собран без неё")
        failure = EXIT_FAILED
    except OSError as error:
        print(f"queue: не удалось прочитать очередь: {error}", file=err)
        warnings.append("база не ответила — список ниже собран без неё")
        failure = EXIT_FAILED

    manual_path = None if args.no_manual else Path(args.manual)
    extra: list[QueueEntry] = []
    if manual_path is not None and manual_path.is_file():
        try:
            extra = list(parse_queue(manual_path.read_text(encoding="utf-8")).items)
        except (QueueUnavailableError, OSError) as error:
            # The hand-written half being unreadable must not cost the half that
            # came from the database, so it is a line in the report rather than
            # an exit code.
            warnings.append(f"{manual_path.name} не прочитан: {error}")

    entries, manual = merge(primary, extra)
    results_path = Path(args.results) if args.results else _results_for(manual_path)
    results = parse_results(results_path) if results_path is not None else ResultsPayload()
    if results_path is not None and manual and not results_path.is_file():
        # Only worth saying when there are hand-added rows to explain. Everything
        # the backend serves is already filtered by what the tracker knows — a
        # vacancy applied to is not in the queue at all — so for those rows the
        # missing file explains nothing and the line would be noise on every run.
        warnings.append(
            f"результатов прошлых прогонов нет ({results_path.name}) — "
            "«не готово» по ручным строкам показано только по тому, что знает краулер"
        )

    view = QueueView(
        source=_describe(source, manual_path, len(manual)),
        rows=classify(list(entries[: args.limit]), results, manual=manual),
        results_seen=len(results.results),
        warnings=tuple(warnings),
    )
    print(render(view, encoding=encoding_of(out)), file=out)
    return failure


def _show_queue_file(args: argparse.Namespace, path: Path, *, out: TextIO, err: TextIO) -> int:
    """One file and nothing else, because that is what was asked for.

    ``--from`` with a path is the transport that needs no server, no database
    and no token. Nothing is merged into it: a person who named a file is
    reading that file, and a second source appearing underneath it would be the
    surprise this command was fixed to stop.
    """
    if not path.is_file():
        print(
            f"queue: нет файла очереди {path}.\n"
            "  Очередь собирается из базы: python -m wwao queue --from http://localhost:8000\n"
            "  Формат файла ручных добавлений — в agent/README.md",
            file=err,
        )
        return EXIT_FAILED
    try:
        payload = parse_queue(path.read_text(encoding="utf-8"))
    except QueueNotBuiltError as error:
        print(f"queue: {error}", file=err)
        return EXIT_MISSING_PIECE
    except QueueUnavailableError as error:
        print(f"queue: {error}", file=err)
        return EXIT_FAILED
    except OSError as error:
        print(f"queue: не удалось прочитать очередь: {error}", file=err)
        return EXIT_FAILED

    warnings: list[str] = []
    # The results of a run are written beside the queue they came from, so a
    # queue read from a file has a known place to look.
    results_path = Path(args.results) if args.results else _results_beside(path)
    results = parse_results(results_path)
    if not results_path.is_file():
        warnings.append(
            f"результатов прошлых прогонов нет ({results_path.name}) — "
            "«не готово» показано только по тому, что знает краулер"
        )
    view = QueueView(
        source=str(path),
        rows=classify(payload.items[: args.limit], results),
        results_seen=len(results.results),
        warnings=tuple(warnings),
    )
    print(render(view, encoding=encoding_of(out)), file=out)
    return EXIT_OK


def _results_for(manual: Path | None) -> Path | None:
    """Where the last run wrote its outcomes, when there is a file to look beside."""
    return None if manual is None else _results_beside(manual)


def _describe(source: str, manual: Path | None, added: int) -> str:
    """The header line: both sources, and how much came from the second one.

    Named in the report because the whole defect this replaced was invisible:
    the header said one path and a reader had no way to know that path was the
    only thing being read.
    """
    if manual is None:
        return f"{source} (база)"
    return f"{source} (база) + {manual} (вручную: {added})"


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
