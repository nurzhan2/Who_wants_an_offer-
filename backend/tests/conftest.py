"""Shared pytest fixtures.

Notes on two deliberate choices:

* There is no ``event_loop`` fixture. Overriding it is deprecated in
  pytest-asyncio >= 0.23 and emits a warning that ``filterwarnings = error``
  turns into a failure; loop scope is configured in ``pyproject.toml``
  (``asyncio_default_fixture_loop_scope``) instead.
* Database-backed tests skip when PostgreSQL is unreachable, which keeps a
  laptop without Docker usable — but a skip in CI fails the run, see
  ``pytest_sessionfinish`` below.
"""

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import text

from app.core.config import settings
from app.db.session import get_session
from app.main import create_app

CI_ENV_VALUES = {"1", "true", "yes"}


def running_in_ci() -> bool:
    """True on GitHub Actions and any CI that sets the conventional CI env var."""
    return os.getenv("CI", "").strip().lower() in CI_ENV_VALUES


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """DSN of the throwaway test database."""
    explicit = os.getenv("TEST_DATABASE_URL")
    if explicit:
        return explicit
    return f"{settings.database_url.rstrip('/')}_test"


@pytest_asyncio.fixture
async def async_engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    """Engine bound to the test database, skipped when the server is unreachable."""
    engine = create_async_engine(test_database_url, poolclass=NullPool, future=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # any driver error means "no database reachable here"
        await engine.dispose()
        pytest.skip(f"PostgreSQL unavailable at {test_database_url}: {type(exc).__name__}")
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(async_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Session wrapped in a transaction that is rolled back after the test."""
    async with async_engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            if transaction.is_active:
                await transaction.rollback()


@pytest.fixture
def app() -> FastAPI:
    """A fresh application instance per test, so overrides never leak."""
    return create_app()


@pytest_asyncio.fixture
async def async_client(app: FastAPI, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """HTTP client talking to the app in-process, backed by the test transaction."""

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


class BrokenSession:
    """Stand-in session whose every statement fails, to exercise degraded paths."""

    async def execute(self, *_: Any, **__: Any) -> Any:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))


@pytest_asyncio.fixture
async def client_without_db(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Client whose database dependency always fails."""

    async def override_get_session() -> AsyncIterator[Any]:
        yield BrokenSession()

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


# ── CI guard: a skipped test is an unverified test ────────────────────

_skipped_node_ids: list[str] = []


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Collect every skipped test so the session hook can act on it."""
    if report.skipped and report.nodeid not in _skipped_node_ids:
        _skipped_node_ids.append(report.nodeid)


def pytest_terminal_summary(terminalreporter: Any) -> None:
    """Explain loudly why the run is about to fail on skips."""
    if _skipped_node_ids and running_in_ci():
        terminalreporter.section("skipped tests are not allowed in CI", red=True)
        for node_id in _skipped_node_ids:
            terminalreporter.write_line(f"  {node_id}")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the run in CI if anything was skipped: green must mean fully verified."""
    if _skipped_node_ids and running_in_ci() and exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
