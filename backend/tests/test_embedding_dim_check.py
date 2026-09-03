"""The startup guard that compares EMBEDDING_DIM against the live schema.

``Vector(1024)`` is a literal inside a migration, ``EMBEDDING_DIM`` is
configuration, and nothing links the two. The guard exists so a drift becomes
one clear line at boot instead of an ``expected 1024 dimensions, not 768``
somewhere inside a resume upload weeks later. That only holds if the guard is
loud on a mismatch and quiet in the three situations where refusing to boot
would be worse than booting: schema not migrated yet, database unreachable, and
everything actually agreeing.
"""

from collections.abc import AsyncIterator, Iterator
from typing import Any, cast

import pytest
import pytest_asyncio
import structlog
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from structlog.testing import capture_logs

from app import main
from app.core.exceptions import SchemaMismatchError
from app.db.checks import VECTOR_COLUMNS, column_vector_dimension, verify_embedding_dimension

#: What the migration declares. Hard-coded on purpose: reading it back from
#: settings would make the assertion tautological.
MIGRATED_WIDTH = 1024


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    """The lifespan reconfigures structlog globally; undo that for other modules."""
    try:
        yield
    finally:
        structlog.reset_defaults()


class UnreachableSession:
    """Session whose every statement fails, standing in for a dead database."""

    async def scalar(self, *_: Any, **__: Any) -> Any:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    async def execute(self, *_: Any, **__: Any) -> Any:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))


@pytest_asyncio.fixture
async def unmigrated_session(scratch_database: str) -> AsyncIterator[AsyncSession]:
    """Session on a brand-new database that has never seen ``alembic upgrade``."""
    engine = create_async_engine(scratch_database, poolclass=NullPool, future=True)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await engine.dispose()


@pytest.fixture
def lifespan_on_test_database(monkeypatch: pytest.MonkeyPatch, async_engine: AsyncEngine) -> None:
    """Point the real lifespan at the migrated test database instead of dev."""
    monkeypatch.setattr(
        main,
        "session_factory",
        async_sessionmaker(bind=async_engine, expire_on_commit=False),
    )


# ── reading the declared width ────────────────────────────────────────


@pytest.mark.parametrize(("table_name", "column_name"), VECTOR_COLUMNS)
async def test_declared_width_is_read_from_the_live_schema(
    db_session: AsyncSession, table_name: str, column_name: str
) -> None:
    """Every vector column the guard knows about must really be vector(1024).

    If this ever disagrees with the migration, the guard is checking a column
    that no longer exists in the shape it assumes.
    """
    assert await column_vector_dimension(db_session, table_name, column_name) == MIGRATED_WIDTH


@pytest.mark.parametrize(
    ("table_name", "column_name"),
    [
        ("vacancy", "no_such_column"),
        ("no_such_table", "embedding"),
    ],
)
async def test_absent_column_reads_as_unknown_rather_than_zero(
    db_session: AsyncSession, table_name: str, column_name: str
) -> None:
    """``None`` is what lets the caller tell "not migrated" from "wrong width"."""
    assert await column_vector_dimension(db_session, table_name, column_name) is None


async def test_non_vector_column_is_reported_as_a_mismatch(db_session: AsyncSession) -> None:
    """A column silently redefined as text would break every embedding write."""
    with pytest.raises(SchemaMismatchError) as caught:
        await column_vector_dimension(db_session, "vacancy", "title")

    assert "vacancy.title" in str(caught.value)


# ── the guard itself ──────────────────────────────────────────────────


async def test_matching_dimensions_produce_no_warning(db_session: AsyncSession) -> None:
    """Silence is the contract: a warning at every boot trains people to ignore it."""
    with capture_logs() as logs:
        await verify_embedding_dimension(db_session)

    assert [entry for entry in logs if entry["log_level"] in {"warning", "error"}] == []


@pytest.mark.parametrize("configured_dim", [768, 1536])
async def test_mismatch_names_both_widths(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, configured_dim: int
) -> None:
    """The message has to say what the schema holds and what the config wants.

    Whoever reads it at 3am must be able to act without opening psql, so both
    numbers appear; a bare "schema mismatch" would be nearly as bad as no guard.
    """
    monkeypatch.setattr(main.settings, "embedding_dim", configured_dim)

    with pytest.raises(SchemaMismatchError) as caught:
        await verify_embedding_dimension(db_session)

    message = str(caught.value)
    assert f"vector({MIGRATED_WIDTH})" in message
    assert str(configured_dim) in message


async def test_unmigrated_database_warns_instead_of_refusing_to_boot(
    unmigrated_session: AsyncSession,
) -> None:
    """A fresh deploy runs the app before the first migration; blocking that deadlocks it."""
    with capture_logs() as logs:
        await verify_embedding_dimension(unmigrated_session)

    warnings = [entry for entry in logs if entry["log_level"] == "warning"]
    assert {entry["reason"] for entry in warnings} == {"column_missing"}
    # Every configured column is reported, not just the first one found missing.
    assert len(warnings) == len(VECTOR_COLUMNS)


async def test_unreachable_database_warns_instead_of_refusing_to_boot() -> None:
    """/health is the place to report a dead database; the process must survive to say so."""
    with capture_logs() as logs:
        await verify_embedding_dimension(cast(AsyncSession, UnreachableSession()))

    warnings = [entry for entry in logs if entry["log_level"] == "warning"]
    assert [entry["reason"] for entry in warnings] == ["OperationalError"]


# ── the lifespan really runs it ───────────────────────────────────────


async def test_startup_runs_the_guard_and_still_boots(
    app: FastAPI, lifespan_on_test_database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must run on every boot, against a real session, without blocking it.

    A plain "the lifespan did not raise" assertion would survive the guard
    being deleted from ``main.lifespan`` outright, so the real function is
    wrapped rather than replaced: it still queries the migrated schema, and the
    wrapper records that startup actually reached it.
    """
    checked: list[int | None] = []
    real_check = main.verify_embedding_dimension

    async def spy(session: AsyncSession) -> None:
        # Read through the very session startup handed over, while it is open:
        # proof it is a live connection to the schema and not a stub.
        checked.append(await column_vector_dimension(session, *VECTOR_COLUMNS[0]))
        await real_check(session)

    monkeypatch.setattr(main, "verify_embedding_dimension", spy)

    async with app.router.lifespan_context(app):
        assert checked == [MIGRATED_WIDTH]


async def test_mismatch_prevents_startup(
    app: FastAPI, lifespan_on_test_database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drifting schema must stop the process at boot, not at the first write.

    This drives the application's real lifespan context, so it fails if the
    check is ever dropped from ``main.lifespan`` — only the session factory is
    redirected at the migrated test database.
    """
    monkeypatch.setattr(main.settings, "embedding_dim", MIGRATED_WIDTH + 1)

    with pytest.raises(SchemaMismatchError):
        async with app.router.lifespan_context(app):
            pytest.fail("startup completed despite an embedding width mismatch")
