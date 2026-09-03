"""Shared pytest fixtures.

Three deliberate choices worth knowing before adding tests here:

* **There is no ``event_loop`` fixture.** Overriding it is deprecated in
  pytest-asyncio >= 0.23 and emits a warning that ``filterwarnings = error``
  turns into a failure. Loop scope is configured in ``pyproject.toml``
  (``asyncio_default_fixture_loop_scope``) instead.
* **The schema comes from Alembic, never from ``create_all``.** The test
  database is migrated to head once per session, so every run re-proves that
  the migrations actually build the schema the code expects.
* **Skips are fatal in CI.** Database tests skip on a laptop with no Docker,
  but a skipped test fails the run when ``CI`` is set — see
  ``pytest_sessionfinish``. A green build has to mean everything ran. pytest
  reports an xfail as a skip, so that counts too: a known bug belongs in the
  code being fixed, not parked behind a marker.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import make_url, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db.repositories import (
    MatchRepository,
    PipelineRunRepository,
    ProfileRepository,
    VacancyRepository,
)
from app.db.session import get_session
from app.main import create_app
from helpers import alembic_config, maintenance_url

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


def unreachable_reason(database_url: str) -> str | None:
    """Why the database cannot be reached, or None when it can.

    Probed separately from the migration on purpose. Wrapping the migration in
    a broad ``except`` would turn a genuinely broken migration into a skip, and
    a skip reads as "no database here" rather than "the schema is broken".
    """

    async def probe() -> None:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    try:
        asyncio.run(probe())
    except (OSError, SQLAlchemyError) as exc:
        # asyncpg raises ConnectionRefusedError (an OSError) straight through
        # rather than wrapping it, so OperationalError alone is not enough.
        return type(exc).__name__
    return None


@pytest.fixture(scope="session")
def reachable_database(test_database_url: str) -> str:
    """The test DSN, or a skip for the whole session if nothing answers there.

    Everything that touches PostgreSQL goes through this, so a laptop without
    Docker gets one clear reason instead of a wall of connection errors.
    """
    reason = unreachable_reason(test_database_url)
    if reason is not None:
        pytest.skip(f"PostgreSQL unavailable at {test_database_url}: {reason}")
    return test_database_url


@pytest.fixture(scope="session")
def migrated_database(reachable_database: str) -> str:
    """Bring the test database to head once per session.

    Running the real migrations rather than ``create_all`` means every test run
    also exercises them, and a drift between models and migrations shows up as
    a failure here instead of at deploy time. A migration error is therefore
    allowed to propagate: only an unreachable server is a skip.
    """
    command.upgrade(alembic_config(reachable_database), "head")
    return reachable_database


@pytest_asyncio.fixture
async def async_engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    """Engine bound to the migrated test database."""
    engine = create_async_engine(migrated_database, poolclass=NullPool, future=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(async_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Session wrapped in a transaction that is rolled back after the test.

    Nothing a test writes survives it, so tests stay order-independent even
    though they share one database.
    """
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


# ── repositories ──────────────────────────────────────────────────────


@pytest.fixture
def vacancies(db_session: AsyncSession) -> VacancyRepository:
    """Vacancy repository bound to the test transaction."""
    return VacancyRepository(db_session)


@pytest.fixture
def profiles(db_session: AsyncSession) -> ProfileRepository:
    """Profile repository bound to the test transaction."""
    return ProfileRepository(db_session)


@pytest.fixture
def matches(db_session: AsyncSession) -> MatchRepository:
    """Match repository bound to the test transaction."""
    return MatchRepository(db_session)


@pytest.fixture
def pipeline_runs(db_session: AsyncSession) -> PipelineRunRepository:
    """Pipeline run repository bound to the test transaction."""
    return PipelineRunRepository(db_session)


# ── a database of its own, for migration tests ────────────────────────


@pytest_asyncio.fixture
async def scratch_database(reachable_database: str) -> AsyncIterator[str]:
    """An empty database created for one test and dropped afterwards.

    The upgrade/downgrade/upgrade test needs a database with no schema at all,
    and must not touch the one every other test is using.
    """
    name = f"wwao_scratch_{uuid4().hex[:12]}"
    admin = create_async_engine(
        maintenance_url(reachable_database),
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
        # render_as_string, not str(): SQLAlchemy renders the password as
        # "***" in __str__, and the resulting DSN fails to authenticate.
        yield make_url(reachable_database).set(database=name).render_as_string(hide_password=False)
    finally:
        async with admin.connect() as connection:
            # Anything still connected would block the DROP.
            await connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        await admin.dispose()


# ── application fixtures ──────────────────────────────────────────────


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


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark everything that touches the database, so `-m "not db"` works."""
    for item in items:
        fixtures: tuple[str, ...] = getattr(item, "fixturenames", ())
        if any(name in fixtures for name in ("db_session", "async_engine", "scratch_database")):
            item.add_marker(pytest.mark.db)
