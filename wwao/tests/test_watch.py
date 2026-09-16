"""The watcher: claims what the dashboard asked for and runs the agent for it.

What must hold:

* it runs the agent as a child process and only the two known commands — the
  read-only walk, and the send restricted to what the dashboard confirmed;
* it reports the start, the end and the child's last lines, and a failing child
  is reported as a failure;
* a backend that does not answer is a line in the window, not a crash;
* it refuses to start without a terminal, like ``apply``.
"""

import io
import sys
from collections.abc import Callable, Sequence
from typing import Any

import httpx
import pytest
import respx

from wwao import cli, watch
from wwao.watch import WatchError

BASE = "http://localhost:8000"
FP = "a" * 64
#: A server running this checkout's code, for every loop test.
SAME: dict[str, Any] = {
    "health": lambda url: {"code_fingerprint": FP},
    "fingerprint": lambda: FP,
}


class Backend:
    """A backend that hands out a scripted list of claims and records reports."""

    def __init__(self, claims: list[dict[str, Any] | None]) -> None:
        self.claims = claims
        self.reports: list[tuple[str, dict[str, Any] | None]] = []

    def __call__(self, url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        if url.endswith("/claim"):
            if not self.claims:
                return {"operation": None}
            claim = self.claims.pop(0)
            if claim is None:
                raise WatchError("Бэкенд не ответил: connection refused")
            return {"operation": claim}
        self.reports.append((url, payload))
        return {}


def _child(code: int, lines: Sequence[str] = ()) -> Any:
    calls: list[list[str]] = []

    def run(command: Sequence[str], echo: Callable[[str], None]) -> int:
        calls.append(list(command))
        for line in lines:
            echo(line)
        return code

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_a_send_request_starts_the_agent_on_the_dashboard_confirmations_only() -> None:
    backend = Backend([{"id": "op-1", "kind": "send"}])
    child = _child(0, ["Отправлено: 2", ""])
    out = io.StringIO()

    assert watch.watch(BASE, post=backend, run_child=child, out=out, rounds=1, **SAME) == 0

    [command] = child.calls
    assert command[:4] == [sys.executable, "-X", "utf8", "-m"]
    assert command[4:] == ["agent.run", "--send", "--dashboard", "--from", BASE]
    statuses = [payload["status"] for _, payload in backend.reports if payload]
    assert statuses == ["running", "success"]
    assert backend.reports[-1][0] == f"{BASE}/api/v1/applications/operations/op-1"
    assert backend.reports[-1][1] == {
        "status": "success",
        "message": "агент закончил",
        "report": ["Отправлено: 2"],
    }
    assert "Отправлено: 2" in out.getvalue()


def test_an_outcomes_request_starts_the_read_only_walk() -> None:
    backend = Backend([{"id": "op-2", "kind": "outcomes"}])
    child = _child(0)

    watch.watch(BASE, post=backend, run_child=child, out=io.StringIO(), rounds=1, **SAME)

    assert child.calls[0][4:] == ["agent.outcomes", "--to", BASE]


def test_a_failing_child_is_reported_as_a_failure_with_its_last_lines() -> None:
    backend = Backend([{"id": "op-3", "kind": "send"}])
    lines = [f"строка {index}" for index in range(20)]

    watch.watch(BASE, post=backend, run_child=_child(2, lines), out=io.StringIO(), rounds=1, **SAME)

    final = backend.reports[-1][1]
    assert final is not None
    assert final["status"] == "failed"
    assert "кодом 2" in final["message"]
    assert final["report"] == lines[-watch.REPORT_LINES :]


def test_an_unknown_request_runs_nothing() -> None:
    backend = Backend([{"id": "op-4", "kind": "delete-account"}])
    child = _child(0)

    watch.watch(BASE, post=backend, run_child=child, out=io.StringIO(), rounds=1, **SAME)

    assert child.calls == []
    final = backend.reports[-1][1]
    assert final is not None
    assert final["status"] == "failed"


def test_a_child_that_cannot_start_is_reported() -> None:
    backend = Backend([{"id": "op-5", "kind": "outcomes"}])

    def broken(command: Sequence[str], echo: Callable[[str], None]) -> int:
        raise FileNotFoundError("python")

    watch.watch(BASE, post=backend, run_child=broken, out=io.StringIO(), rounds=1, **SAME)

    final = backend.reports[-1][1]
    assert final is not None
    assert "не запустился" in final["message"]


def test_a_silent_backend_is_a_line_and_the_loop_goes_on() -> None:
    backend = Backend([None, None])
    slept: list[float] = []
    out = io.StringIO()

    watch.watch(
        BASE,
        post=backend,
        run_child=_child(0),
        out=out,
        rounds=3,
        interval=7.0,
        sleep=slept.append,
        **SAME,
    )

    assert slept == [7.0, 7.0, 7.0]
    assert out.getvalue().count("Бэкенд не ответил") == 2


def test_a_report_that_cannot_be_delivered_does_not_stop_the_job() -> None:
    reports: list[str] = []

    def flaky(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        if url.endswith("/claim"):
            return {"operation": {"id": "op-6", "kind": "outcomes"}}
        reports.append(url)
        raise WatchError("gone")

    child = _child(0)
    out = io.StringIO()
    watch.watch(BASE, post=flaky, run_child=child, out=out, rounds=1, **SAME)

    assert len(child.calls) == 1
    assert "Не удалось сообщить дашборду" in out.getvalue()


@respx.mock
def test_posting_carries_the_token_and_names_where_it_came_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_API_TOKEN", "s3cret")
    route = respx.post(f"{BASE}{watch.CLAIM_PATH}").mock(
        return_value=httpx.Response(200, json={"operation": None})
    )
    assert watch.post_over_http(f"{BASE}{watch.CLAIM_PATH}", None) == {"operation": None}
    assert route.calls[0].request.headers["authorization"] == "Bearer s3cret"

    route.mock(return_value=httpx.Response(401))
    with pytest.raises(WatchError, match=r"ответил 401.*окружение") as raised:
        watch.post_over_http(f"{BASE}{watch.CLAIM_PATH}", None)
    assert "s3cret" not in str(raised.value)

    route.mock(return_value=httpx.Response(503))
    with pytest.raises(WatchError, match="не задан"):
        watch.post_over_http(f"{BASE}{watch.CLAIM_PATH}", None)

    route.mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(WatchError, match="500"):
        watch.post_over_http(f"{BASE}{watch.CLAIM_PATH}", None)

    route.mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(WatchError, match=r"не ответил.*refused") as silent:
        watch.post_over_http(f"{BASE}{watch.CLAIM_PATH}", None)
    assert not isinstance(silent.value, watch.RouteMissingError)

    route.mock(return_value=httpx.Response(404))
    with pytest.raises(watch.RouteMissingError) as missing:
        watch.post_over_http(f"{BASE}{watch.CLAIM_PATH}", None)
    assert "ответил 404" in str(missing.value)
    assert watch.CLAIM_PATH in str(missing.value)
    assert "не ответил" not in str(missing.value)
    assert "другой экземпляр" in str(missing.value)


def test_the_real_child_runner_passes_output_through() -> None:
    """Russian survives the pipe because the child runs in UTF-8 mode.

    Without ``-X utf8`` a child on a Russian Windows writes cp1251 into the pipe
    and this reads it back as replacement characters — measured, it is how this
    test first failed.
    """
    seen: list[str] = []
    code = watch.run_with_echo(
        [sys.executable, "-X", "utf8", "-c", "print('строка'); raise SystemExit(3)"],
        seen.append,
    )
    assert code == 3
    assert seen == ["строка"]


class _Stream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_watch_refuses_to_start_without_a_terminal() -> None:
    err = _Stream(False)
    code = cli.main(["watch"], stdin=_Stream(False), stdout=_Stream(False), stderr=err)

    assert code == cli.EXIT_NO_HUMAN
    assert "без окна терминала" in err.getvalue()


def test_watch_takes_a_closed_set_of_flags() -> None:
    with pytest.raises(SystemExit):
        cli.main(["watch", "--send-everything"], stdin=_Stream(True), stdout=_Stream(True))


def test_watch_in_a_terminal_starts_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    started: dict[str, Any] = {}

    def fake_watch(base: str, **kwargs: Any) -> int:
        started.update(base=base, **kwargs)
        return 0

    monkeypatch.setattr(watch, "watch", fake_watch)
    code = cli.main(
        ["watch", "--from", "http://127.0.0.1:9000", "--interval", "0"],
        stdin=_Stream(True),
        stdout=_Stream(True),
        stderr=_Stream(True),
    )

    assert code == 0
    assert started["base"] == "http://127.0.0.1:9000"
    assert started["interval"] == 1.0


def test_a_missing_route_stops_the_loop_and_says_why() -> None:
    """Polling a server without the route again changes nothing."""
    polls: list[str] = []

    def old_server(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        polls.append(url)
        raise watch.RouteMissingError("Бэкенд ответил 404: маршрута нет.")

    out = io.StringIO()
    code = watch.watch(BASE, post=old_server, run_child=_child(0), out=out, rounds=5, **SAME)

    assert code == watch.EXIT_OTHER_SERVER
    assert len(polls) == 1
    assert "ответил 404" in out.getvalue()


@pytest.mark.parametrize(
    ("body", "why"),
    [({"status": "ok"}, "старой версии"), ({"code_fingerprint": "0" * 64}, "другого кода")],
)
def test_a_server_from_other_code_is_refused_before_polling(
    body: dict[str, object], why: str
) -> None:
    polls: list[str] = []

    def never(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        polls.append(url)
        return {}

    out = io.StringIO()
    code = watch.watch(
        BASE,
        post=never,
        run_child=_child(0),
        out=out,
        rounds=1,
        health=lambda url: body,
        fingerprint=lambda: FP,
    )

    assert code == watch.EXIT_OTHER_SERVER
    assert polls == []
    assert why in out.getvalue()
    assert "start.cmd" in out.getvalue()


def test_a_server_not_up_yet_is_waited_for() -> None:
    backend = Backend([])
    code = watch.watch(
        BASE,
        post=backend,
        run_child=_child(0),
        out=io.StringIO(),
        rounds=1,
        sleep=lambda seconds: None,
        health=lambda url: None,
        fingerprint=lambda: FP,
    )
    assert code == 0
