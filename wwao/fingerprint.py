"""Which code a running API was started from, told apart without importing it.

Added 2026-09-17. ``python -m wwao up`` reused whatever answered ``/health`` on
port 8000, and on the owner's machine that was an API started days earlier from
older code: the dashboard's new routes were not there, and the watcher answered
every poll with a 404 it could not explain. The package version cannot tell the
two apart — it is ``0.1.0`` for both — so the API reports a digest of its own
source instead, and this module computes the same digest from the files on
disk.

The algorithm is deliberately trivial and is written twice: here, because this
package must not import ``app``, and in ``app.services.health``. The test
``backend/tests/test_health.py`` holds the two to the same answer.
"""

import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Final

#: The API's source tree, relative to the repository root.
APP_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "backend" / "app"


def code_fingerprint(root: Path = APP_DIR) -> str:
    """SHA-256 over every ``*.py`` under ``root``: relative path, then content.

    Line endings are normalised so a checkout with CRLF and one with LF agree.
    """
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()


def fetch_health(url: str) -> dict[str, object] | None:
    """``/health``'s decoded body, or None when nothing answered with JSON.

    A 503 still carries a body — the API reports a degraded database that way —
    so it is read like a 200.
    """
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        raw = error.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None
    try:
        decoded = json.loads(raw)
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None
