"""The whole day's collecting and preparing, started from one command.

``python -m wwao chain`` is the terminal's half of the «Цепочка» button: it asks
the running server to start the chain and then prints what each step is doing
until the chain ends. The work itself happens in the server
(``app.services.autopilot``) rather than here, and that is deliberate — the
dashboard's button and this command must not be two implementations of one
routine that can disagree about what "собрать очередь" means.

**It sends nothing, and it is the command a scheduler runs.** The chain ends
with a ready queue; applications leave only after somebody reads the batch and
confirms it, which needs a person and is therefore ``wwao apply`` or the
dashboard. So this command has no terminal check — there is no card to read —
and no flag that could turn it into a send, in this file or in the server.

Like the rest of this package it imports neither ``app`` nor ``agent``: it talks
HTTP, the same two endpoints the browser uses, and it needs no token because
those endpoints are the dashboard's own and are not behind one.
"""

import json
import time
from collections.abc import Callable
from typing import Any, Final, TextIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from wwao.console import encoding_of, printable

#: The two endpoints this command uses, both the dashboard's own.
OPERATIONS_PATH: Final[str] = "/api/v1/operations"

#: How often to ask how the chain is doing. The steps are minutes long — a
#: polite hh crawl is about twenty of them — so this is about keeping the window
#: alive rather than about resolution.
DEFAULT_INTERVAL: Final[float] = 5.0

#: How long a single HTTP call may take. The chain itself is long; every one of
#: these requests is short, and one that is not means the server is wedged.
TIMEOUT: Final[float] = 30.0

#: Everything ran.
EXIT_OK: Final[int] = 0
#: A step failed, or the server could not be reached.
EXIT_FAILED: Final[int] = 1

#: Posts or reads JSON and returns the decoded answer. Injected so the loop can
#: be tested without a server.
Caller = Callable[[str, dict[str, Any] | None], dict[str, Any]]


class ChainError(Exception):
    """The server refused or could not be reached, in a sentence for a person."""


def call_over_http(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    """One request to the dashboard's own API: POST with a body, GET without.

    ``urllib`` rather than httpx, because this package is the one part of the
    project that is supposed to run with nothing installed — it is what the
    person double-clicking ``start.cmd`` ends up in — and one JSON GET does not
    justify a dependency the CLI does not otherwise need.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", "replace")[:300]
        raise ChainError(f"Сервер ответил {error.code} на {url}: {body}") from error
    except (URLError, TimeoutError, ValueError) as error:
        raise ChainError(
            f"Сервер приложения не ответил ({url}): {error}. "
            "Он запускается вместе с приложением — start.cmd."
        ) from error
    return decoded if isinstance(decoded, dict) else {}


def run(
    base_url: str,
    *,
    call: Caller = call_over_http,
    out: TextIO,
    interval: float = DEFAULT_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
    rounds: int | None = None,
) -> int:
    """Start the chain and follow it to its end, printing each step as it moves.

    One line per transition rather than a redrawn block: this output is read in
    a console window that may be scrolled back hours later, and it is what a
    scheduled run leaves behind as its record.
    """
    encoding = encoding_of(out)

    def say(line: str) -> None:
        print(printable(line, encoding), file=out)

    try:
        started = call(f"{base_url.rstrip('/')}{OPERATIONS_PATH}", {"kind": "chain"})
    except ChainError as error:
        say(str(error))
        return EXIT_FAILED

    operation_id = str(started.get("id", ""))
    say("Цепочка запущена: сбор, эмбеддинги, подбор, письма, очередь.")
    say("Ничего не отправляется: цепочка заканчивается готовой очередью.")

    seen: dict[str, str] = {}
    polls = 0
    while rounds is None or polls < rounds:
        polls += 1
        try:
            state = call(f"{base_url.rstrip('/')}{OPERATIONS_PATH}", None)
        except ChainError as error:
            say(str(error))
            return EXIT_FAILED
        operation = _chain_operation(state, operation_id)
        if operation is None:
            say("Сервер больше не помнит эту цепочку — возможно, его перезапустили.")
            return EXIT_FAILED
        for line in _transitions(operation, seen):
            say(line)
        if str(operation.get("status")) in _TERMINAL:
            return _finish(operation, say)
        sleep(interval)
    return EXIT_OK


def _chain_operation(state: dict[str, Any], operation_id: str) -> dict[str, Any] | None:
    """This chain out of the operations list, by id.

    By id rather than by kind: a second chain started from the dashboard while
    this one runs would otherwise be reported here as if it were ours, and its
    ending would end this command.
    """
    operations = state.get("operations")
    if not isinstance(operations, list):
        return None
    for entry in operations:
        if isinstance(entry, dict) and str(entry.get("id")) == operation_id:
            return entry
    return None


def _transitions(operation: dict[str, Any], seen: dict[str, str]) -> list[str]:
    """The lines for whatever changed since the last poll.

    A step is printed when it starts, when it finishes and whenever the note it
    shows changes — which is what makes twenty minutes of crawling visible as
    something other than silence.
    """
    lines: list[str] = []
    steps = operation.get("steps")
    if not isinstance(steps, list):
        return lines
    for step in steps:
        if not isinstance(step, dict):
            continue
        key = str(step.get("key"))
        status = str(step.get("status"))
        note = str(step.get("note") or "")
        mark = f"{status}:{note}"
        if seen.get(key) == mark:
            continue
        seen[key] = mark
        title = str(step.get("title"))
        # Words rather than arrows and ticks. This console encodes cp1251 and
        # ``printable`` turns anything outside it into a question mark, so a
        # tidy «✓ Сбор вакансий» would reach the owner as «? Сбор вакансий».
        if status == "running":
            lines.append(f"  [идёт] {title}: {note or 'начинаю'}")
        elif status == "done":
            lines.append(f"  [готово] {title}")
            lines.extend(f"      {line}" for line in _report(step))
        elif status == "skipped":
            lines.append(f"  [пропущен] {title}: {note}")
        elif status == "failed":
            lines.append(f"  [ошибка] {title}: {note}")
    return lines


def _report(step: dict[str, Any]) -> list[str]:
    """One step's result lines, as the server wrote them."""
    report = step.get("report")
    return [str(line) for line in report] if isinstance(report, list) else []


def _finish(operation: dict[str, Any], say: Callable[[str], None]) -> int:
    """Print how the chain ended and answer with the exit code for it."""
    status = str(operation.get("status"))
    if status == "success":
        say("\nЦепочка прошла целиком.")
        for line in _report(operation):
            say(f"  {line}")
        say("\nЧто дальше: откройте «Обзор» и нажмите «Отправить все» — ")
        say("там письма целиком, галочки и одно подтверждение на всю пачку.")
        return EXIT_OK
    say(f"\n{operation.get('message') or 'Цепочка не прошла.'}")
    error = operation.get("error")
    if error:
        say(str(error))
    say("Повторный запуск продолжит с того шага, на котором она остановилась.")
    return EXIT_FAILED


_TERMINAL: Final[frozenset[str]] = frozenset({"success", "failed", "cancelled"})
