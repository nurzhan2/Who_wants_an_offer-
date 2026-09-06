"""The two packages do not know about each other, enforced rather than agreed.

The brief's first section is about blast radius. ``backend/`` is anonymous and
read-only and runs on a server; ``agent/`` acts under a person's account on their
own laptop and sends things. Mixing them means one day a scheduler sends an
application — «однажды случайно отправить отклик из планировщика» — and that is
not a mistake a code review reliably catches, because the import that causes it
looks helpful.

So it is a test. Two directions, both of which have a plausible-looking reason
to be crossed:

*The agent importing the backend* would give it the vacancy repository, the
settings, the letter generation. It would also give it a database URL and an
Anthropic key in a process that drives a browser under somebody's login.

*The backend importing playwright* would put a browser in the server image and
make the crawler capable of doing things the crawler must never do.

Both are checked by parsing rather than by grepping: several modules discuss
these boundaries in prose, and a text search would flag the explanation as the
violation.
"""

import ast
import subprocess
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
BACKEND_ROOT = REPO_ROOT / "backend" / "app"


#: Functions that fetch a module named by a string rather than by syntax. An
#: import statement is what ``ast.Import`` models, and it is not the only way to
#: get a module: ``importlib.import_module("app.core.config")`` crosses this
#: boundary without producing one. That matters more here than anywhere else in
#: the package, because this is the boundary CLAUDE.md draws around blast radius —
#: what keeps a database URL and an Anthropic key out of a process driving a
#: browser under somebody's login.
DYNAMIC_IMPORTERS = frozenset({"import_module", "__import__", "load_module"})


def _imported_modules(path: Path) -> set[str]:
    """Every module name this file imports, by statement or by name.

    Covers ``import x``, ``from x import y``, and a call to one of
    :data:`DYNAMIC_IMPORTERS` with a literal string argument.

    What it does not cover, and cannot: a module name assembled at runtime, read
    from a file, or reached by poking ``sys.modules``. That is the honest limit
    of a static scan and it is written down rather than implied, so the next
    person knows what this test promises. It stops the crossing that actually
    happens — an import added because it was convenient — not a determined one.
    """
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
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


def test_the_agent_never_imports_the_backend() -> None:
    """Not for the settings, not for the schemas, not for a helper.

    The one seam between them is the queue contract in ``agent/queue.py``, which
    speaks HTTP and JSON and shares no code.
    """
    offenders = {
        path.name: sorted(name for name in _imported_modules(path) if name.split(".")[0] == "app")
        for path in AGENT_ROOT.rglob("*.py")
    }
    crossings = {name: imports for name, imports in offenders.items() if imports}

    assert not crossings, (
        f"agent/ imports backend modules: {crossings}. The agent runs under the owner's "
        "account; the backend is anonymous and read-only, and they do not share code."
    )


def test_the_backend_never_imports_playwright() -> None:
    """A browser has no business in a process that crawls anonymously on a server."""
    crossings = {
        path.name
        for path in BACKEND_ROOT.rglob("*.py")
        for name in _imported_modules(path)
        if name.split(".")[0] == "playwright"
    }

    assert not crossings, f"backend imports playwright: {sorted(crossings)}"


def test_the_backend_never_imports_the_agent() -> None:
    """The direction that would put a sender inside the crawler."""
    crossings = {
        path.name
        for path in BACKEND_ROOT.rglob("*.py")
        for name in _imported_modules(path)
        if name.split(".")[0] == "agent"
    }

    assert not crossings, f"backend imports the agent: {sorted(crossings)}"


def test_playwright_is_not_a_backend_dependency() -> None:
    """Declared in a group, so ``uv sync --no-dev`` cannot pull it into the image.

    ``backend/Dockerfile`` builds with ``uv sync --frozen --no-dev``, which
    installs ``[project].dependencies`` and no groups at all. Checking the
    declaration rather than the lockfile is deliberate: the lockfile legitimately
    contains playwright, because the agent's group is locked with everything
    else, and what matters is which list the server installs from.
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = pyproject["project"]["dependencies"]
    optional = [
        name
        for extra in pyproject["project"].get("optional-dependencies", {}).values()
        for name in extra
    ]

    assert not [name for name in runtime + optional if "playwright" in name]
    assert any("playwright" in name for name in pyproject["dependency-groups"]["agent"])


def test_the_agent_keeps_its_own_settings() -> None:
    """Sharing ``app.core.config`` would hand a browser session a database URL and an API key.

    Checked by parsing, like everything else here: ``agent/config.py`` opens by
    explaining that it deliberately does not import the backend's settings, and
    a text search would read the explanation as the offence.
    """
    imports = _imported_modules(AGENT_ROOT / "config.py")

    assert not [name for name in imports if name.startswith("app")]
    assert (AGENT_ROOT / "config.py").is_file()


def test_everything_the_agent_writes_is_ignored_by_git() -> None:
    """Every artefact of a logged-in session, not the three somebody remembered.

    The first version of this listed exactly the patterns that were present, so
    it passed while three more were committable: the probe's reports (which copy
    the owner's own application state for a vacancy out of an authenticated
    page), the session signal, and the run's results. The repo has a live remote
    and ``git add -A`` staged them.
    """
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    for pattern in (
        "agent/profile/",
        "agent/screenshots/",
        "agent/*.sqlite3",
        "agent/queue.json",
        "agent/queue-results.json",
        "agent/answers/",
        "agent/probe/",
        "agent/session_signal.json",
    ):
        assert pattern in ignored, f"{pattern} must never be committable"


def test_nothing_the_agent_writes_is_tracked_right_now() -> None:
    """The patterns are one thing; what is actually in the index is another."""
    tracked = subprocess.run(
        ["git", "ls-files", "agent"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    leaked = [
        name
        for name in tracked
        if any(
            part in name
            for part in ("profile/", "screenshots/", ".sqlite3", "probe/", "session_signal")
        )
    ]

    assert not leaked, f"an artefact of the owner's session is committed: {leaked}"


def test_the_import_scan_sees_a_module_fetched_by_name(tmp_path: Path) -> None:
    """An import statement is not the only way to get a module.

    ``importlib.import_module("app.core.config")`` produces no ``ast.Import``
    node, so this scan used to walk straight past the one crossing its own
    docstring says it prevents. Driven over a source file written here rather
    than over the production tree, so it keeps testing the scanner even after
    the tree changes.
    """
    sneaky = tmp_path / "sneaky.py"
    sneaky.write_text(
        'import importlib\nsettings = importlib.import_module("app.core.config").settings\n',
        encoding="utf-8",
    )

    assert "app.core.config" in _imported_modules(sneaky)


def test_the_import_scan_says_what_it_cannot_see(tmp_path: Path) -> None:
    """A name assembled at runtime is beyond a static scan, and that is documented.

    Pinning the limit rather than leaving it implied: whoever relies on this
    file should know it stops the convenient import, not a determined one.
    """
    assembled = tmp_path / "assembled.py"
    assembled.write_text(
        'import importlib\nsettings = importlib.import_module("ap" + "p.core.config")\n',
        encoding="utf-8",
    )

    assert "app.core.config" not in _imported_modules(assembled)
