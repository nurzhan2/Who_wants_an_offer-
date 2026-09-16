"""Shared fixtures for the CLI's tests."""

from pathlib import Path

import pytest

from wwao import queue_view


@pytest.fixture(autouse=True)
def no_repository_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's real ``.env`` out of every test.

    The CLI reads the local token from ``.env`` when the environment has none,
    and the machine these tests run on has a real one.
    """
    monkeypatch.setattr(queue_view, "DOTENV", tmp_path / "absent.env")
