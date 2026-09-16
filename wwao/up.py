"""Everything the dashboard needs, started by one command or one double click.

    python -m wwao up        (или двойной щелчок по start.cmd в корне проекта)

Until 2026-09-16 the application needed three things started by hand — the
database container, the API and the frontend dev server — and somebody without a
terminal could start none of them. This command checks each, starts what is
missing, says in Russian what it could not do and why, opens the dashboard in the
browser, and then stays in this window as the local agent watcher: the one
process allowed to act under the owner's hh login (see :mod:`wwao.watch`).

**Checked before started, never started twice.** A database that is already
up, an API already answering ``/health``, a dev server already on its port —
each is reused. Closing this window stops only what this window started.

**Nothing here is a shortcut past a rule.** The API is the same ``uvicorn``
``make dev`` runs; migrations are ``alembic upgrade head``; the watcher keeps
``apply``'s terminal requirement. The only file this command may write is
``.env``, and only to fill an empty ``AGENT_API_TOKEN=`` with a fresh random
token — the dashboard's agent buttons cannot work without one, and a token
nobody chose is no worse than an empty one. It says so when it does.

Like the rest of this package it imports neither ``app`` nor ``agent``: every
piece is a child process.
"""

import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, TextIO

from wwao import queue_view
from wwao.queue_view import TOKEN_VARIABLE, read_dotenv

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
API_URL: Final[str] = "http://localhost:8000"
DASHBOARD_URL: Final[str] = "http://localhost:5173"

#: How long each piece may take to come up before the command gives up on it.
API_WAIT_SECONDS: Final[float] = 120.0
WEB_WAIT_SECONDS: Final[float] = 90.0

#: Exit codes. Distinct so a shortcut's window can say which step failed.
EXIT_OK: Final[int] = 0
EXIT_NO_DOCKER: Final[int] = 10
EXIT_NO_DATABASE: Final[int] = 11
EXIT_MIGRATIONS: Final[int] = 12
EXIT_NO_API: Final[int] = 13
EXIT_NO_NODE: Final[int] = 14
EXIT_NO_WEB: Final[int] = 15


@dataclass
class Machine:
    """Everything this command does to the outside world, injectable for tests."""

    #: Runs a command to completion and returns (exit code, combined output).
    run: Callable[[Sequence[str], Path], tuple[int, str]]
    #: Starts a long-running child and returns a handle with ``terminate()``.
    spawn: Callable[[Sequence[str], Path], "Stoppable"]
    #: True when the URL answers with a status below 500.
    answers: Callable[[str], bool]
    which: Callable[[str], str | None] = shutil.which
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    open_browser: Callable[[str], object] = webbrowser.open
    started: list["Stoppable"] = field(default_factory=list)


class Stoppable:
    """The one thing ``up`` needs from a child it started."""

    def __init__(self, process: "subprocess.Popen[bytes]") -> None:
        self._process = process

    def terminate(self) -> None:
        """Ask the child to stop and give it a few seconds."""
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()


def up(machine: Machine, out: TextIO, *, open_browser: bool = True) -> int:
    """Bring the stack up and return an exit code; the caller then runs the watcher."""
    steps: list[Callable[[Machine, TextIO], int]] = [
        _env_file,
        _database,
        _migrations,
        _api,
        _web,
    ]
    for step in steps:
        code = step(machine, out)
        if code != EXIT_OK:
            return code
    say(out, f"Готово. Дашборд: {DASHBOARD_URL}")
    if open_browser:
        machine.open_browser(DASHBOARD_URL)
    return EXIT_OK


def say(out: TextIO, line: str) -> None:
    """One line of progress, flushed so a double-clicked window shows it now."""
    print(line, file=out)
    out.flush()


# ── the steps ─────────────────────────────────────────────────────────


def _env_file(machine: Machine, out: TextIO) -> int:
    """Make sure ``.env`` exists and carries a local agent token."""
    dotenv = queue_view.DOTENV
    example = dotenv.with_name(".env.example")
    if not dotenv.is_file():
        if not example.is_file():
            say(out, "Нет ни .env, ни .env.example — не знаю, с какими настройками запускать.")
            return EXIT_NO_API
        shutil.copyfile(example, dotenv)
        say(out, "Файла .env не было — создал его из .env.example. Проверьте ключи в нём.")
    if read_dotenv(dotenv).get(TOKEN_VARIABLE):
        return EXIT_OK
    token = secrets.token_urlsafe(32)
    text = dotenv.read_text(encoding="utf-8")
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().removeprefix("export ").split("=", 1)[0].strip() == TOKEN_VARIABLE:
            lines[index] = f"{TOKEN_VARIABLE}={token}"
            break
    else:
        lines.append(f"{TOKEN_VARIABLE}={token}")
    dotenv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    say(
        out,
        f"В .env не было {TOKEN_VARIABLE} — записал случайный. Он нужен, чтобы дашборд "
        "мог передавать агенту запросы; менять его не обязательно.",
    )
    return EXIT_OK


def _database(machine: Machine, out: TextIO) -> int:
    """Start the PostgreSQL container, or say why it cannot be started."""
    if machine.which("docker") is None:
        say(
            out,
            "Docker не найден. Установите Docker Desktop (https://www.docker.com/products/"
            "docker-desktop/), запустите его и повторите.",
        )
        return EXIT_NO_DOCKER
    code, _ = machine.run(["docker", "info"], REPO_ROOT)
    if code != 0:
        say(
            out,
            "Docker установлен, но не запущен. Откройте Docker Desktop, дождитесь запуска "
            "и повторите.",
        )
        return EXIT_NO_DOCKER
    say(out, "База данных: запускаю контейнер…")
    code, output = machine.run(["docker", "compose", "up", "-d", "--wait", "db"], REPO_ROOT)
    if code != 0:
        say(out, "Контейнер базы не поднялся. Что ответил Docker:")
        say(out, _tail(output))
        return EXIT_NO_DATABASE
    say(out, "База данных: работает.")
    return EXIT_OK


def _migrations(machine: Machine, out: TextIO) -> int:
    """Bring the schema to head. Safe to repeat: alembic does nothing when current."""
    code, output = machine.run([sys.executable, "-m", "alembic", "upgrade", "head"], REPO_ROOT)
    if code != 0:
        say(out, "Схема базы не обновилась (alembic upgrade head). Последние строки:")
        say(out, _tail(output))
        return EXIT_MIGRATIONS
    say(out, "Схема базы: актуальна.")
    return EXIT_OK


def _api(machine: Machine, out: TextIO) -> int:
    """Reuse a running API, or start one and wait for ``/health``."""
    health = f"{API_URL}/health"
    if machine.answers(health):
        say(out, "Сервер приложения: уже работает.")
        return EXIT_OK
    say(out, "Сервер приложения: запускаю…")
    machine.started.append(
        machine.spawn(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
            ],
            REPO_ROOT,
        )
    )
    if not _wait(machine, health, API_WAIT_SECONDS):
        say(
            out,
            f"Сервер приложения не ответил за {API_WAIT_SECONDS:.0f} с. Проверьте сообщения "
            "выше: чаще всего это неверная строка в .env или занятый порт 8000.",
        )
        return EXIT_NO_API
    say(out, "Сервер приложения: отвечает.")
    return EXIT_OK


def _web(machine: Machine, out: TextIO) -> int:
    """Reuse a running dashboard, or install and start the dev server."""
    if machine.answers(DASHBOARD_URL):
        say(out, "Дашборд: уже работает.")
        return EXIT_OK
    npm = machine.which("npm")
    if npm is None:
        say(
            out,
            "Node.js не найден, а без него дашборд не запустить. Установите Node.js 20 или новее "
            "(https://nodejs.org/) и повторите.",
        )
        return EXIT_NO_NODE
    frontend = REPO_ROOT / "frontend"
    if not (frontend / "node_modules").is_dir():
        say(out, "Дашборд: ставлю зависимости (один раз, пару минут)…")
        code, output = machine.run([npm, "install"], frontend)
        if code != 0:
            say(out, "npm install не прошёл. Последние строки:")
            say(out, _tail(output))
            return EXIT_NO_WEB
    say(out, "Дашборд: запускаю…")
    machine.started.append(machine.spawn([npm, "run", "dev"], frontend))
    if not _wait(machine, DASHBOARD_URL, WEB_WAIT_SECONDS):
        say(
            out,
            f"Дашборд не поднялся за {WEB_WAIT_SECONDS:.0f} с. Проверьте, не занят ли порт 5173.",
        )
        return EXIT_NO_WEB
    say(out, "Дашборд: отвечает.")
    return EXIT_OK


def _wait(machine: Machine, url: str, seconds: float) -> bool:
    """Poll ``url`` until it answers or the time is up."""
    deadline = machine.clock() + seconds
    while machine.clock() < deadline:
        if machine.answers(url):
            return True
        machine.sleep(1.0)
    return False


def _tail(output: str, lines: int = 12) -> str:
    """The last lines of a child's output, indented."""
    return "\n".join(f"  {line}" for line in output.strip().splitlines()[-lines:])


def stop(machine: Machine, out: TextIO) -> None:
    """Stop what this window started, newest first."""
    for child in reversed(machine.started):
        child.terminate()
    if machine.started:
        say(out, "Остановил то, что запускал это окно. База данных продолжает работать.")


# ── the real machine ──────────────────────────────────────────────────


def _run(command: Sequence[str], cwd: Path) -> tuple[int, str]:
    try:
        done = subprocess.run(
            list(command),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        return 127, str(error)
    return done.returncode, f"{done.stdout}\n{done.stderr}"


def _spawn(command: Sequence[str], cwd: Path) -> Stoppable:
    return Stoppable(subprocess.Popen(list(command), cwd=str(cwd)))


def _answers(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return int(response.status) < 500
    except urllib.error.HTTPError as error:
        return error.code < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


def real_machine() -> Machine:
    """The machine ``python -m wwao up`` runs on."""
    return Machine(run=_run, spawn=_spawn, answers=_answers)
