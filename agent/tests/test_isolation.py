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
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
BACKEND_ROOT = REPO_ROOT / "backend" / "app"


def _imported_modules(path: Path) -> set[str]:
    """Every module name this file imports, at any depth."""
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
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


def test_the_profile_and_the_screenshots_are_ignored_by_git() -> None:
    """The profile holds a live hh session; a screenshot shows a logged-in account."""
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    for pattern in ("agent/profile/", "agent/screenshots/", "agent/*.sqlite3"):
        assert pattern in ignored, f"{pattern} must never be committable"
