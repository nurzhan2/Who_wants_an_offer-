"""``python -m wwao up``: start what is missing, reuse what runs, say what failed.

The machine is a fake: no Docker, no server, no browser, no network.
"""

import io
from collections.abc import Sequence
from pathlib import Path

import pytest

from wwao import cli, queue_view, up, watch

FP = "f" * 64
NETSTAT = (
    "Активные подключения\n\n"
    "  Имя    Локальный адрес        Внешний адрес          Состояние       PID\n"
    "  TCP    127.0.0.1:8000         0.0.0.0:0              ПРОСЛУШИВАНИЕ   32344\n"
    "  TCP    127.0.0.1:8000         127.0.0.1:51148        TIME_WAIT       0\n"
)


class Child(up.Stoppable):
    def __init__(self, command: Sequence[str]) -> None:
        self.command = list(command)
        self.stopped = False

    def terminate(self) -> None:
        self.stopped = True


class FakeMachine(up.Machine):
    """Records every command; the answers are set per test."""

    def __init__(
        self,
        *,
        docker: bool = True,
        docker_running: bool = True,
        compose_ok: bool = True,
        alembic_ok: bool = True,
        npm: bool = True,
        up_urls: set[str] | None = None,
        start_answers: bool = True,
        other_api: dict[str, object] | None = None,
        netstat: str | None = NETSTAT,
    ) -> None:
        self.commands: list[list[str]] = []
        self.children: list[Child] = []
        self.opened: list[str] = []
        self._docker = docker
        self._docker_running = docker_running
        self._compose_ok = compose_ok
        self._alembic_ok = alembic_ok
        self._npm = npm
        self._up = set(up_urls or ())
        self._start_answers = start_answers
        self._now = 0.0
        self._other_api = other_api
        self._netstat = netstat
        super().__init__(
            run=self._run,
            spawn=self._spawn,
            answers=lambda url: url in self._up,
            health=self._health,
            fingerprint=lambda: FP,
            which=self._which,
            sleep=self._sleep,
            clock=lambda: self._now,
            open_browser=self.opened.append,
        )

    def _which(self, name: str) -> str | None:
        if name == "docker":
            return "docker" if self._docker else None
        if name == "npm":
            return "npm" if self._npm else None
        return None

    def _health(self, url: str) -> dict[str, object] | None:
        if self._other_api is not None:
            return self._other_api
        return {"status": "ok", "code_fingerprint": FP} if url in self._up else None

    def _run(self, command: Sequence[str], cwd: Path) -> tuple[int, str]:
        if command[0] == "netstat":
            return (0, self._netstat) if self._netstat is not None else (1, "")
        self.commands.append(list(command))
        if command[:2] == ["docker", "info"]:
            return (0 if self._docker_running else 1), ""
        if command[:2] == ["docker", "compose"]:
            return (0, "") if self._compose_ok else (1, "pull access denied\nimage missing")
        if "alembic" in command:
            return (0, "") if self._alembic_ok else (1, "FAILED: Can't locate revision")
        return 0, ""

    def _spawn(self, command: Sequence[str], cwd: Path) -> up.Stoppable:
        child = Child(command)
        self.children.append(child)
        if self._start_answers:
            if "uvicorn" in command:
                self._up.add(f"{up.API_URL}/health")
            if "dev" in command:
                self._up.add(up.DASHBOARD_URL)
        return child

    def _sleep(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture
def dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text("DATABASE_URL=x\nAGENT_API_TOKEN=already\n", encoding="utf-8")
    monkeypatch.setattr(queue_view, "DOTENV", path)
    return path


def test_everything_missing_is_started_and_the_dashboard_opened(dotenv: Path) -> None:
    machine = FakeMachine()
    out = io.StringIO()

    assert up.up(machine, out) == up.EXIT_OK

    assert ["docker", "compose", "up", "-d", "--wait", "db"] in machine.commands
    assert any("alembic" in command for command in machine.commands)
    assert [child.command[-1] for child in machine.children] == ["8000", "dev"]
    assert machine.opened == [up.DASHBOARD_URL]
    assert "Готово" in out.getvalue()


def test_running_pieces_are_reused_not_started_twice(dotenv: Path) -> None:
    machine = FakeMachine(up_urls={f"{up.API_URL}/health", up.DASHBOARD_URL})
    out = io.StringIO()

    assert up.up(machine, out, open_browser=False) == up.EXIT_OK

    assert machine.children == []
    assert machine.opened == []
    assert "уже работает" in out.getvalue()


@pytest.mark.parametrize(
    ("machine", "code", "said"),
    [
        (FakeMachine(docker=False), up.EXIT_NO_DOCKER, "Docker не найден"),
        (FakeMachine(docker_running=False), up.EXIT_NO_DOCKER, "не запущен"),
        (FakeMachine(compose_ok=False), up.EXIT_NO_DATABASE, "pull access denied"),
        (FakeMachine(alembic_ok=False), up.EXIT_MIGRATIONS, "Can't locate revision"),
        (FakeMachine(npm=False), up.EXIT_NO_NODE, "Node.js не найден"),
        (FakeMachine(start_answers=False), up.EXIT_NO_API, "не ответил"),
    ],
)
def test_each_failure_names_its_cause_and_stops(
    dotenv: Path, machine: FakeMachine, code: int, said: str
) -> None:
    out = io.StringIO()

    assert up.up(machine, out) == code
    assert said in out.getvalue()
    assert machine.opened == []


def test_a_dashboard_that_never_answers_is_reported(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "frontend" / "node_modules").mkdir(parents=True)
    monkeypatch.setattr(up, "REPO_ROOT", tmp_path)
    machine = FakeMachine(up_urls={f"{up.API_URL}/health"}, start_answers=False)
    out = io.StringIO()

    assert up.up(machine, out) == up.EXIT_NO_WEB
    assert "5173" in out.getvalue()
    assert not any(command[-1] == "install" for command in machine.commands)


def test_dependencies_are_installed_once_before_the_first_start(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "frontend").mkdir()
    monkeypatch.setattr(up, "REPO_ROOT", tmp_path)
    machine = FakeMachine(up_urls={f"{up.API_URL}/health"})

    assert up.up(machine, io.StringIO(), open_browser=False) == up.EXIT_OK
    assert ["npm", "install"] in machine.commands


def test_an_empty_token_is_filled_and_said(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# local\nAGENT_API_TOKEN=\nOTHER=1\n", encoding="utf-8")
    monkeypatch.setattr(queue_view, "DOTENV", path)
    out = io.StringIO()

    assert up._env_file(FakeMachine(), out) == up.EXIT_OK

    values = queue_view.read_dotenv(path)
    assert len(values["AGENT_API_TOKEN"]) >= 32
    assert values["OTHER"] == "1"
    assert "записал случайный" in out.getvalue()
    assert values["AGENT_API_TOKEN"] not in out.getvalue()


def test_a_missing_env_is_created_from_the_example(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env.example").write_text("DATABASE_URL=x\n", encoding="utf-8")
    monkeypatch.setattr(queue_view, "DOTENV", tmp_path / ".env")
    out = io.StringIO()

    assert up._env_file(FakeMachine(), out) == up.EXIT_OK

    values = queue_view.read_dotenv(tmp_path / ".env")
    assert values["DATABASE_URL"] == "x"
    assert values["AGENT_API_TOKEN"]
    assert "создал его из .env.example" in out.getvalue()


def test_without_env_or_example_nothing_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(queue_view, "DOTENV", tmp_path / ".env")
    machine = FakeMachine()

    assert up.up(machine, io.StringIO()) != up.EXIT_OK
    assert machine.commands == []


def test_stop_ends_only_what_this_window_started(dotenv: Path) -> None:
    machine = FakeMachine()
    up.up(machine, io.StringIO(), open_browser=False)
    out = io.StringIO()

    up.stop(machine, out)

    assert all(child.stopped for child in machine.children)
    assert "База данных продолжает работать" in out.getvalue()


class _Stream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_the_cli_brings_the_stack_up_then_watches_then_stops(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = FakeMachine()
    watched: list[str] = []

    def fake_watch(base: str, **_: object) -> int:
        watched.append(base)
        return 0

    monkeypatch.setattr(watch, "watch", fake_watch)
    args = cli.build_parser().parse_args(["up", "--no-browser"])
    out = _Stream(True)

    code = cli._up(args, src=_Stream(True), out=out, err=_Stream(True), machine=machine)

    assert code == 0
    assert watched == [up.API_URL]
    assert machine.opened == []
    assert all(child.stopped for child in machine.children)


def test_the_cli_stops_early_and_says_so(dotenv: Path) -> None:
    machine = FakeMachine(docker=False)
    args = cli.build_parser().parse_args(["up"])
    out = _Stream(True)

    code = cli._up(args, src=_Stream(True), out=out, err=_Stream(True), machine=machine)

    assert code == up.EXIT_NO_DOCKER
    assert "Не всё запустилось" in out.getvalue()


def test_up_needs_a_terminal() -> None:
    err = _Stream(False)
    assert cli.main(["up"], stdin=_Stream(False), stdout=_Stream(False), stderr=err) == (
        cli.EXIT_NO_HUMAN
    )


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ({"status": "ok", "version": "0.1.0"}, "более старой версии"),
        ({"status": "ok", "code_fingerprint": "0" * 64}, "из другого кода"),
    ],
)
def test_an_api_from_other_code_is_not_reused_and_is_named(
    dotenv: Path, body: dict[str, object], why: str
) -> None:
    """Measured 2026-09-17: the old API on 8000 was reused and the watcher 404-ed."""
    machine = FakeMachine(other_api=body)
    out = io.StringIO()

    assert up.up(machine, out) == up.EXIT_OTHER_API

    said = out.getvalue()
    assert "другой экземпляр" in said
    assert why in said
    assert "процесс 32344" in said
    assert "taskkill /PID 32344" in said
    assert "уже работает" not in said
    assert machine.children == []
    assert machine.opened == []


def test_without_netstat_the_message_still_says_what_to_stop(dotenv: Path) -> None:
    machine = FakeMachine(other_api={"status": "ok"}, netstat=None)
    out = io.StringIO()

    assert up.up(machine, out) == up.EXIT_OTHER_API
    assert "слушает порт 8000" in out.getvalue()


def test_the_listener_is_found_by_address_not_by_the_state_word() -> None:
    english = NETSTAT.replace("ПРОСЛУШИВАНИЕ", "LISTENING")
    for listing in (NETSTAT, english):
        assert up.listener(FakeMachine(netstat=listing), 8000) == "32344"
    assert up.listener(FakeMachine(netstat=NETSTAT), 5173) is None
