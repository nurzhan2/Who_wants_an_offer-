"""Shared fixtures for the agent's tests."""

from pathlib import Path

import pytest

from agent import queue


@pytest.fixture(autouse=True)
def no_repository_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's real ``.env`` out of every test.

    The queue client reads the local token from ``.env`` when the environment
    has none, and the machine these tests run on has a real one.
    """
    monkeypatch.setattr(queue, "DOTENV", tmp_path / "absent.env")
