"""One command spans both worlds; this is the test that it does not fuse them.

``backend/`` is anonymous, read-only and runs on a server. ``agent/`` acts under
a person's own hh account on their laptop and sends things.
``agent/tests/test_isolation.py`` keeps them apart by parsing the import graph
in both directions, and it is right to: the import that would cross is the one
that looks helpful, and the failure it causes — a scheduler sending an
application — is not one a code review reliably catches.

A single CLI with a ``crawl`` subcommand and an ``apply`` subcommand is exactly
the thing that could put both in one process. It does not, and this file says so
three ways, because each catches something the others do not.

*Statically*, by parsing every file in this package for an import of ``app``,
``agent`` or a browser driver. This is the same technique
``agent/tests/test_isolation.py`` uses and for the same reason: several modules
here discuss the boundary in prose, and a text search would read the explanation
as the offence. The scanner is copied rather than imported — importing the
agent's test module to prove this package does not import the agent would be the
crossing itself.

*Dynamically*, by starting a real interpreter, running each unattended
subcommand in it with the child process replaced by a recorder, and looking at
what ``sys.modules`` ended up holding. This is what the static scan cannot do: a
transitive import, three libraries deep, produces no statement to find.

*By what it starts*, because that is where the guarantee actually comes from.
Every subcommand hands its work to a child process — the backend's own scripts
on one side, ``python -m agent.run`` on the other — so neither world is ever
loaded here at all, and the agent's own process is covered by the agent's own
isolation test.
"""

import ast
import io
import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest

from wwao import cli

pytestmark = pytest.mark.unit

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
PACKAGE: Final[Path] = REPO_ROOT / "wwao"

#: The three top-level names this package must never load. ``app`` is the
#: backend, ``agent`` is the sender, ``playwright`` is the browser that drives
#: somebody's logged-in session.
FOREIGN: Final[frozenset[str]] = frozenset({"app", "agent", "playwright"})

#: Fetching a module by a string rather than by syntax. Copied from
#: ``agent/tests/test_isolation.py``: an import statement is not the only way to
#: get a module, and ``importlib.import_module("app.core.config")`` would cross
#: this boundary without producing one.
DYNAMIC_IMPORTERS: Final[frozenset[str]] = frozenset(
    {"import_module", "__import__", "load_module", "find_spec"}
)


def _imported_modules(path: Path) -> set[str]:
    """Every module this file names, by statement or by string.

    The honest limit, written down rather than implied: a module name assembled
    at runtime is beyond a static scan. This stops the import somebody adds
    because it was convenient, not a determined one.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Call):
            called = node.func
            name = called.attr if isinstance(called, ast.Attribute) else getattr(called, "id", "")
            if name in DYNAMIC_IMPORTERS:
                names.update(
                    argument.value
                    for argument in node.args
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                )
    return names


def test_the_cli_imports_neither_world_anywhere_in_its_source() -> None:
    """Not the backend, not the agent, not a browser. Including in the tests."""
    crossings = {
        path.relative_to(REPO_ROOT).as_posix(): sorted(
            name for name in _imported_modules(path) if name.split(".")[0] in FOREIGN
        )
        for path in PACKAGE.rglob("*.py")
    }
    offenders = {name: imports for name, imports in crossings.items() if imports}

    assert not offenders, (
        f"CLI импортирует то, что должно жить в отдельном процессе: {offenders}. "
        "Подкоманды запускают чужой код дочерним процессом именно для этого."
    )


def test_the_scan_would_see_a_module_fetched_by_name(tmp_path: Path) -> None:
    """Driven over a file written here, so it keeps testing the scanner.

    Without this the previous test passes on a package that reaches the backend
    through ``importlib.import_module("app.core.config")``, which produces no
    ``ast.Import`` node at all.
    """
    sneaky = tmp_path / "sneaky.py"
    sneaky.write_text(
        'import importlib\nsettings = importlib.import_module("app.core.config").settings\n',
        encoding="utf-8",
    )

    assert "app.core.config" in _imported_modules(sneaky)


def test_a_live_process_running_the_unattended_subcommands_loads_neither_world(
    tmp_path: Path,
) -> None:
    """The dynamic half: what a real interpreter actually ended up holding.

    Runs crawl, letters, match and queue in one fresh process with the child
    process replaced by a recorder, then reports every foreign top-level module
    in ``sys.modules``. A transitive import that no statement in this package
    names would show up here and nowhere else.
    """
    queue = tmp_path / "queue.json"
    queue.write_text(
        json.dumps(
            {
                "version": 1,
                "items": [{"vacancy_id": "1", "url": "https://hh.kz/vacancy/1", "title": "x"}],
            }
        ),
        encoding="utf-8",
    )
    report = tmp_path / "loaded.json"

    program = textwrap.dedent(
        """
        import io, json, sys
        from wwao import cli

        queue, report = sys.argv[1], sys.argv[2]
        commands = []

        def record(command):
            commands.append(list(command))
            return 0

        class Terminal(io.StringIO):
            def isatty(self):
                return True

        codes = []
        for argv in (
            ["crawl", "--dry-run"],
            ["letters", "--limit", "3"],
            ["match"],
            ["queue", "--from", queue],
        ):
            codes.append(
                cli.main(argv, run=record, stdout=io.StringIO(), stderr=io.StringIO())
            )
        # apply too: it must build the agent's command line without loading the
        # agent here. Given streams that claim to be a terminal, because the
        # refusal would otherwise return before anything was built.
        codes.append(
            cli.main(
                ["apply", "--send"],
                run=record,
                stdin=Terminal(),
                stdout=Terminal(),
                stderr=io.StringIO(),
            )
        )
        crossed = sorted(
            {name.split(".")[0] for name in sys.modules if name.split(".")[0] in
             {"app", "agent", "playwright"}}
        )
        report_text = json.dumps({"commands": commands, "crossed": crossed, "codes": codes})
        open(report, "w", encoding="utf-8").write(report_text)
        """
    )
    environment = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONIOENCODING": "utf-8"}

    finished = subprocess.run(
        [sys.executable, "-c", program, str(queue), str(report)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )

    assert finished.returncode == 0, finished.stderr
    loaded = json.loads(report.read_text(encoding="utf-8"))
    assert loaded["crossed"] == [], (
        f"процесс CLI загрузил {loaded['crossed']}. Подкоманды должны запускать "
        "чужой код дочерним процессом, а не импортировать его."
    )
    # And it did do the work: five subcommands, every one of which now has
    # something to start. `match` was the exception until the scorer was
    # written, and that it now spawns a script which DOES import the backend
    # world is exactly what the assertion above is about: the CLI process
    # stays clean because it spawns rather than imports.
    assert loaded["codes"] == [0, 0, 0, 0, 0]
    assert [command[1:3] for command in loaded["commands"]][-1] == ["-m", "agent.run"]


def test_the_agent_is_reached_only_as_a_process_and_never_as_an_import() -> None:
    """The one place this package names the agent, and it is a string.

    ``agent.run`` here is an argument to ``python -m``, not a module this
    process loads. That is what lets ``agent/tests/test_isolation.py`` — which
    proves nothing under ``agent/`` imports ``app`` — cover the ``apply``
    subcommand too: the process that applies is the agent's own.
    """
    commands: list[list[str]] = []

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    def record(command: Sequence[str]) -> int:
        commands.append(list(command))
        return 0

    cli.main(
        ["apply", "--send"],
        run=record,
        stdin=Terminal(),
        stdout=Terminal(),
        stderr=io.StringIO(),
    )

    assert commands == [[sys.executable, "-m", "agent.run", "--send"]]
    assert cli.AGENT_MODULE not in _imported_modules(REPO_ROOT / "wwao" / "cli.py")


def test_nothing_on_the_way_to_a_confirmation_reads_the_environment() -> None:
    """Nothing outside the terminal may stand in for the person at it.

    The task forbids «no environment variable that pre-answers the prompt». The
    guarantee is made by geography rather than by inspecting variable names one
    at a time: ``cli.py`` holds the whole of the ``apply`` route — the terminal
    check, the closed flag set, the command that is built — and it reads no
    environment at all, so there is no variable, including the ones nobody has
    invented yet, that can reach any of it. Everything else a wrapped tool needs
    is read by that tool, in its own process, where it belongs.

    One variable is read in this package, in ``queue_view.fetch_over_http``:
    the local token the queue endpoint is behind. It is on the read-only path
    that cannot send anything, and it is asserted below to be the only one.
    """
    readers = {"getenv", "environ", "environb", "putenv"}
    reads_environment: dict[str, list[str]] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        found = sorted(
            {
                node.attr if isinstance(node, ast.Attribute) else node.id
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                if isinstance(node, ast.Attribute | ast.Name)
                and (node.attr if isinstance(node, ast.Attribute) else node.id) in readers
            }
        )
        if found:
            reads_environment[path.name] = found

    assert "cli.py" not in reads_environment, (
        f"подтверждение стало зависеть от окружения: cli.py читает {reads_environment}. "
        "Слово в подтверждении набирает человек, и подменить его извне нельзя."
    )
    assert set(reads_environment) <= {"queue_view.py"}, (
        f"окружение читается не только на пути очереди: {reads_environment}"
    )


def test_the_only_environment_variable_this_package_names_is_the_queue_token() -> None:
    """One name, and it is a credential for a read-only endpoint.

    Pinning the name rather than the count: a second variable added here is not
    automatically wrong, but it has to be argued for in front of this test
    rather than appearing in a diff.
    """
    named = {
        node.value
        for path in PACKAGE.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.isupper()
        and "_" in node.value
    }

    assert named == {"AGENT_API_TOKEN"}, f"CLI знает про переменные окружения: {sorted(named)}"


def test_every_wrapped_step_names_a_file_the_repository_can_show_you() -> None:
    """A router's whole content is where each step lives, so that has to be true.

    ``match`` is the one that does not exist yet, and it is the one the CLI has
    a whole message about; the other two must be real files or the router is
    pointing at nothing.
    """
    present = {tool.name: tool.script.is_file() for tool in cli.WRAPPED}

    assert present["crawl"] and present["letters"]
    assert all(tool.script.parent == cli.SCRIPTS for tool in cli.WRAPPED)
