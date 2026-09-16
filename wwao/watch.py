"""The local half of the dashboard's two agent buttons.

The dashboard can ask for two things the API must never do itself: read what hh
answered to the owner's applications, and send the applications the owner has
already confirmed card by card in the browser. Both need the owner's browser and
hh session, which live on this machine and nowhere else. So the API only
records the request, and this loop — started on the owner's machine, in a
console window — claims it, runs the agent **in its own process**, and reports
back.

Nothing here decides anything about an application. ``send`` starts
``python -m agent.run --send --dashboard``, and the agent sends only what the
dashboard marked as confirmed *and* whose letter and card still match what the
person confirmed; everything else it leaves alone. ``outcomes`` starts the
read-only walk, which mints no mandate and therefore cannot send at all.

Like the rest of this package, this module imports neither ``app`` nor
``agent``: the backend is reached over HTTP with the local token, and the agent
is a child process. The child's output is shown in this window as it happens and
its last lines are handed back to the dashboard as the report.
"""

import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TextIO

from wwao.fingerprint import code_fingerprint, fetch_health
from wwao.queue_view import local_token

#: The seam the watcher talks to. Token-guarded, like the queue.
CLAIM_PATH: Final[str] = "/api/v1/applications/operations/claim"
REPORT_PATH: Final[str] = "/api/v1/applications/operations/{id}"

#: How often an idle watcher asks for work. The dashboard calls a watcher dead
#: after ninety seconds of silence, so this has to stay well under that.
DEFAULT_INTERVAL: Final[float] = 10.0

#: How many of the child's last lines become the dashboard's report.
REPORT_LINES: Final[int] = 12

#: Posts JSON and returns the decoded answer. Injected so the loop can be
#: tested without a network.
Poster = Callable[[str, dict[str, Any] | None], dict[str, Any]]
#: Runs one child and returns its exit code, calling back with each line.
ChildRunner = Callable[[Sequence[str], Callable[[str], None]], int]


#: Exit code when the server on the port is not this checkout's: polling it
#: again changes nothing, so the loop stops and says so.
EXIT_OTHER_SERVER: Final[int] = 2


class WatchError(Exception):
    """The backend refused or could not be reached, in a sentence for a person."""


class RouteMissingError(WatchError):
    """The server answered, and it has no such route.

    Not "the backend did not answer": it did, and the answer is that the process
    on the port was started from other code. Retrying cannot fix that, so the
    loop stops on it. Measured 2026-09-17: an API started days earlier kept
    port 8000 and the watcher logged a 404 on every poll.
    """


@dataclass(frozen=True, slots=True)
class Job:
    """One claimed request."""

    id: str
    kind: str


def command_for(kind: str, base_url: str) -> list[str] | None:
    """The child process for one kind of request, or None for an unknown kind.

    ``-X utf8`` makes the child write UTF-8 into the pipe whatever the console's
    codepage is; this window then prints it through the hardened stream.
    """
    python = [sys.executable, "-X", "utf8", "-m"]
    if kind == "outcomes":
        return [*python, "agent.outcomes", "--to", base_url]
    if kind == "send":
        return [*python, "agent.run", "--send", "--dashboard", "--from", base_url]
    return None


def watch(
    base_url: str,
    *,
    post: Poster,
    run_child: ChildRunner,
    out: TextIO,
    interval: float = DEFAULT_INTERVAL,
    rounds: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    health: Callable[[str], dict[str, object] | None] = fetch_health,
    fingerprint: Callable[[], str] = code_fingerprint,
) -> int:
    """Claim and run requests until interrupted, or for ``rounds`` polls in tests.

    The server is checked once before the loop: an API started from other code
    answers ``/health`` like a current one and then 404s every claim, so the
    watcher says which it is up front. A server that does not answer yet is not
    an error — the loop waits for it.
    """
    base = base_url.rstrip("/")
    body = health(f"{base}/health")
    if body is not None and body.get("code_fingerprint") != fingerprint():
        print(other_server(base, "code_fingerprint" in body), file=out)
        return EXIT_OTHER_SERVER
    print(f"Жду запросов дашборда от {base}. Остановить: Ctrl+C.", file=out)
    done = 0
    while rounds is None or done < rounds:
        done += 1
        try:
            claimed = post(f"{base}{CLAIM_PATH}", None).get("operation")
        except RouteMissingError as error:
            print(str(error), file=out)
            return EXIT_OTHER_SERVER
        except WatchError as error:
            print(str(error), file=out)
            sleep(interval)
            continue
        if not isinstance(claimed, dict):
            sleep(interval)
            continue
        job = Job(id=str(claimed.get("id")), kind=str(claimed.get("kind")))
        _run(job, base, post=post, run_child=run_child, out=out)
    return 0


def _run(
    job: Job,
    base: str,
    *,
    post: Poster,
    run_child: ChildRunner,
    out: TextIO,
) -> None:
    """Run one claimed request and tell the dashboard how it ended."""
    report_url = f"{base}{REPORT_PATH.format(id=job.id)}"
    command = command_for(job.kind, base)
    if command is None:
        _report(post, report_url, "failed", f"Этот агент не умеет «{job.kind}».", [], out)
        return

    print(f"\n== {_TITLES.get(job.kind, job.kind)} ==", file=out)
    _report(post, report_url, "running", "агент запущен на компьютере владельца", [], out)
    tail: deque[str] = deque(maxlen=REPORT_LINES)

    def echo(line: str) -> None:
        print(line, file=out)
        if line.strip():
            tail.append(line.strip())

    try:
        code = run_child(command, echo)
    except OSError as error:
        _report(post, report_url, "failed", f"Агент не запустился: {error}", [], out)
        return
    if code == 0:
        _report(post, report_url, "success", "агент закончил", list(tail), out)
    else:
        _report(
            post,
            report_url,
            "failed",
            f"Агент завершился с кодом {code}. Последние строки — в отчёте.",
            list(tail),
            out,
        )


def _report(
    post: Poster,
    url: str,
    status: str,
    message: str,
    lines: list[str],
    out: TextIO,
) -> None:
    """Send one progress report; a failure to report is printed, not raised."""
    try:
        post(url, {"status": status, "message": message, "report": lines})
    except WatchError as error:
        print(f"Не удалось сообщить дашборду ({status}): {error}", file=out)


_TITLES: Final[dict[str, str]] = {
    "outcomes": "Читаю, что hh ответил на отправленные отклики",
    "send": "Отправляю отклики, подтверждённые на дашборде",
}


def other_server(base: str, reports_code: bool) -> str:
    """The sentence for a server on ``base`` that is not this checkout's code."""
    why = (
        "запущен из другого кода — из другой папки или до последних изменений"
        if reports_code
        else "запущен из старой версии, которая ещё не сообщает, из какого кода она"
    )
    return (
        f"На {base} отвечает другой экземпляр сервера приложения: он {why}. "
        "Маршрутов, через которые дашборд передаёт запросы агенту, в нём может не быть. "
        "Остановите его (закройте окно, где он запущен, или завершите процесс на этом порту) "
        "и запустите start.cmd снова."
    )


def post_over_http(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    """POST JSON with the local token and return the decoded answer.

    Every failure is named for what it is: no answer at all, an answer refusing
    the token, a 404 because the route does not exist on that server, or some
    other status. They need different actions and used to share one sentence.
    """
    import httpx

    token, found_in = local_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        response = httpx.post(url, json=payload, headers=headers, timeout=30.0)
    except httpx.HTTPError as error:
        raise WatchError(f"Бэкенд не ответил ({url}): {error}. Жду и пробую снова.") from error
    if response.status_code in (401, 403):
        raise WatchError(
            f"Бэкенд ответил {response.status_code}: не принял локальный токен AGENT_API_TOKEN "
            + (f"(взят из: {found_in})." if token else "(не задан ни в окружении, ни в .env).")
        )
    if response.status_code == 404:
        raise RouteMissingError(
            f"Бэкенд ответил 404: маршрута {httpx.URL(url).path} на этом сервере нет. "
            + other_server(f"{httpx.URL(url).scheme}://{httpx.URL(url).netloc.decode()}", True)
        )
    if response.status_code == 503:
        raise WatchError(
            "Бэкенд ответил 503: у него не задан AGENT_API_TOKEN — пропишите его в .env "
            "и перезапустите сервер."
        )
    if response.status_code >= 400:
        raise WatchError(f"Бэкенд ответил {response.status_code} на {url}: {response.text[:300]}")
    decoded = response.json()
    return decoded if isinstance(decoded, dict) else {}


def run_with_echo(command: Sequence[str], echo: Callable[[str], None]) -> int:
    """Run a child with its output passed through line by line.

    ``stdin`` is inherited: a captcha or an expired session is handed to the
    person at this window, never solved here.
    """
    with subprocess.Popen(
        list(command),
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    ) as child:
        if child.stdout is not None:
            for line in child.stdout:
                echo(line.rstrip("\n"))
        return child.wait()
