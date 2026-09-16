"""The digest the launcher compares with the API's ``/health``.

That the API computes the same digest is checked from the backend's side, in
``backend/tests/test_health.py``: this package's tests may not import ``app``.
"""

from pathlib import Path

from wwao.fingerprint import code_fingerprint


def test_line_endings_do_not_change_the_digest(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    crlf = code_fingerprint(tmp_path)
    (tmp_path / "a.py").write_bytes(b"x = 1\ny = 2\n")
    assert code_fingerprint(tmp_path) == crlf


def test_any_edit_changes_the_digest(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    before = code_fingerprint(tmp_path)
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert code_fingerprint(tmp_path) != before
