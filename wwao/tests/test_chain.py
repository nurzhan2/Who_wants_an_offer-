"""The chain from the terminal: what it starts, what it prints, what it cannot do.

Nothing here reaches the network. :func:`wwao.chain.run` takes its transport and
its sleep as arguments precisely so that the loop — twenty minutes of crawling,
five steps, one failure — can be walked in milliseconds.

Three claims, and the third is the one the phase rests on:

* it follows *its own* chain, by id, so a second one started from the dashboard
  while this runs neither reports for it nor ends it;
* a failed step ends the command with a code a scheduler can read, and says
  that a rerun continues rather than starting over;
* **it cannot send.** Not "does not by default": there is no argument, and the
  only thing it posts is ``kind: chain``. A scheduled command runs where nobody
  is watching, so that has to be a property of the file rather than of how it
  is called.
"""

import ast
import io
from pathlib import Path
from typing import Any, Final, final

import pytest

from wwao import chain, cli

pytestmark = pytest.mark.unit

OPERATION_ID: Final[str] = "0199c3b0-1111-7000-8000-000000000001"

STEPS: Final[tuple[tuple[str, str], ...]] = (
    ("crawl", "Сбор вакансий"),
    ("embed", "Эмбеддинги"),
    ("match", "Подбор"),
    ("letters", "Письма"),
    ("queue", "Очередь"),
)


def _steps(**status: str) -> list[dict[str, Any]]:
    """The five steps, each pending unless the caller named it."""
    return [
        {
            "key": key,
            "title": title,
            "status": status.get(key, "pending"),
            "note": None,
            "report": [],
        }
        for key, title in STEPS
    ]


def _state(
    steps: list[dict[str, Any]],
    *,
    status: str = "running",
    operation_id: str = OPERATION_ID,
    report: list[str] | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """What ``GET /api/v1/operations`` answers, with this chain inside it."""
    return {
        "operations": [
            {"id": "other-operation", "kind": "match", "status": "success", "steps": []},
            {
                "id": operation_id,
                "kind": "chain",
                "status": status,
                "message": message or "Цепочка: идёт.",
                "steps": steps,
                "report": report or [],
            },
        ]
    }


@final
class Server:
    """A backend that answers a scripted sequence of polls."""

    def __init__(self, *answers: dict[str, Any]) -> None:
        self.answers = list(answers)
        self.posted: list[tuple[str, dict[str, Any] | None]] = []

    def __call__(self, url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        self.posted.append((url, payload))
        if payload is not None:
            return {"id": OPERATION_ID}
        return self.answers.pop(0) if self.answers else self.answers[-1]


def _run(server: Server, *, rounds: int | None = None) -> tuple[int, str]:
    out = io.StringIO()
    code = chain.run(
        "http://localhost:8000",
        call=server,
        out=out,
        sleep=lambda _: None,
        rounds=rounds,
    )
    return code, out.getvalue()


def test_it_starts_the_chain_and_follows_it_to_the_end() -> None:
    server = Server(
        _state(_steps(crawl="running")),
        _state(_steps(crawl="done", embed="running")),
        _state(
            _steps(crawl="done", embed="done", match="done", letters="done", queue="done"),
            status="success",
            report=["Готово к отправке: 4. В «посмотреть руками»: 11."],
        ),
    )

    code, printed = _run(server)

    assert code == chain.EXIT_OK
    assert server.posted[0] == (
        "http://localhost:8000/api/v1/operations",
        {"kind": "chain"},
    )
    assert "Сбор вакансий" in printed
    assert "Готово к отправке: 4" in printed
    # The line that says what the command did NOT do, printed every run.
    assert "Ничего не отправляется" in printed


def test_a_failed_step_is_named_and_the_command_says_a_rerun_continues() -> None:
    """A scheduled run leaves this text behind as its whole record."""
    server = Server(
        _state(
            _steps(crawl="done", embed="failed"),
            status="failed",
            message="Цепочка остановилась на шаге «Эмбеддинги»: модель недоступна.",
        )
    )

    code, printed = _run(server)

    assert code == chain.EXIT_FAILED
    assert "Эмбеддинги" in printed
    assert "продолжит с того шага" in printed


def test_a_chain_somebody_else_started_is_not_mistaken_for_this_one() -> None:
    """Its ending would otherwise end this command, and its steps would be printed
    here as if they were ours."""
    server = Server(_state(_steps(crawl="done"), status="success", operation_id="someone-else"))

    code, printed = _run(server, rounds=1)

    assert code == chain.EXIT_FAILED
    assert "больше не помнит" in printed


def test_a_server_that_is_not_running_says_what_to_start() -> None:
    def refuse(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        raise chain.ChainError("Сервер приложения не ответил. Он запускается через start.cmd.")

    out = io.StringIO()
    code = chain.run("http://localhost:8000", call=refuse, out=out, sleep=lambda _: None)

    assert code == chain.EXIT_FAILED
    assert "start.cmd" in out.getvalue()


def test_every_line_it_prints_survives_the_console_it_prints_on() -> None:
    """This console encodes cp1251, and a character outside it becomes «?».

    So no ticks, no arrows, no box drawing — the step marks are words. Asserted
    rather than trusted, because the first version of this file used «✓» and the
    owner would have read «? Сбор вакансий» at three in the morning.
    """
    server = Server(
        _state(_steps(crawl="running")),
        _state(
            _steps(crawl="done", embed="done", match="done", letters="done", queue="done"),
            status="success",
            report=["Перечитано страниц: 12, из них закрыто или в архиве: 3."],
        ),
    )

    _, printed = _run(server)

    assert printed.encode("cp1251", "strict").decode("cp1251") == printed
    assert "[готово]" in printed


def test_the_cli_routes_chain_without_asking_for_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No card to read and no word to type, so no terminal check: this is the
    command a scheduler runs at three in the morning."""
    asked: list[str] = []

    def fake_run(base_url: str, **kwargs: Any) -> int:
        asked.append(base_url)
        return chain.EXIT_OK

    monkeypatch.setattr(cli.chain_command, "run", fake_run)
    code = cli.main(
        ["chain"],
        stdin=io.StringIO(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert (code, asked) == (chain.EXIT_OK, ["http://localhost:8000"])


def test_the_chain_command_has_no_way_to_send_anything() -> None:
    """The vocabulary, read out of the source, in front of whoever adds the flag.

    The chain ends with a queue. Sending is the agent, on the owner's own
    machine, after a person has read a batch and confirmed it — so no string in
    this module may name an operation that sends, and no argument may exist that
    would turn the command into one.
    """
    tree = ast.parse(Path(chain.__file__).read_text(encoding="utf-8"))
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    }
    written = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    ]
    written += [node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)]
    for forbidden in ("send", "--yes", "unattended", "auto_send", "apply"):
        assert not any(forbidden in text for text in written), forbidden

    chain_parser = _subparser("chain")
    assert {action.dest for action in chain_parser._actions} == {"help", "backend", "interval"}


def _subparser(name: str) -> Any:
    """One subcommand's own parser, out of the CLI's parser."""
    import argparse

    for action in cli.build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices[name]
    raise AssertionError(f"нет подкоманды {name}")
