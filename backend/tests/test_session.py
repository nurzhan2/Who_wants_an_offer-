"""The session dependency always closes, and rolls back on failure."""

from types import TracebackType
from typing import Any

import pytest

from app.db import session as session_module
from app.db.session import get_session


class RecordingSession:
    """Minimal stand-in that records whether it was rolled back."""

    def __init__(self) -> None:
        self.rolled_back = False
        self.closed = False

    async def __aenter__(self) -> "RecordingSession":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        self.closed = True
        return False

    async def rollback(self) -> None:
        self.rolled_back = True


@pytest.fixture
def recording_session(monkeypatch: pytest.MonkeyPatch) -> RecordingSession:
    """Swap the real session factory for one we can inspect."""
    session = RecordingSession()
    monkeypatch.setattr(session_module, "session_factory", lambda: session)
    return session


async def test_session_is_closed_on_success(recording_session: RecordingSession) -> None:
    """A successful request leaves no session open and nothing rolled back."""
    generator = get_session()
    yielded: Any = await anext(generator)
    assert yielded is recording_session

    with pytest.raises(StopAsyncIteration):
        await anext(generator)

    assert recording_session.closed is True
    assert recording_session.rolled_back is False


async def test_session_rolls_back_on_error(recording_session: RecordingSession) -> None:
    """A failing request must not leave a half-written transaction behind."""
    generator = get_session()
    await anext(generator)

    with pytest.raises(RuntimeError):
        await generator.athrow(RuntimeError("handler blew up"))

    assert recording_session.rolled_back is True
    assert recording_session.closed is True


@pytest.mark.db
async def test_real_session_executes_statements(async_client: Any) -> None:
    """End-to-end proof that the async stack talks to PostgreSQL."""
    response = await async_client.get("/health")

    assert response.json()["components"]["database"]["status"] == "ok"
