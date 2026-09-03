"""Migrations must apply, reverse and re-apply on a genuinely empty database.

This is the test that catches the classic PostgreSQL enum trap: Alembic
autogenerate emits no CREATE TYPE, so a migration can pass on a developer
machine where the types already exist and fail on the first clean deploy.
Running upgrade → downgrade → upgrade against a database created for this test
is the only thing that actually proves otherwise.
"""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from helpers import run_alembic

EXPECTED_TABLES = {
    "application",
    "candidate_profile",
    "match",
    "pipeline_run",
    "profile_skill",
    "vacancy",
    "vacancy_skill",
    "vacancy_source",
}

EXPECTED_ENUM_TYPES = {
    "application_status",
    "employment_type",
    "match_bucket",
    "parse_status",
    "pipeline_run_status",
    "remote_type",
    "salary_period",
    "seniority",
    "skill_level",
}

EXPECTED_PG_INDEXES = {
    "ix_pg_candidate_profile_embedding_hnsw",
    "ix_pg_match_profile_score",
    "ix_pg_vacancy_embedding_hnsw",
    "ix_pg_vacancy_published_at_active",
    "ix_pg_vacancy_salary_normalized_active",
    "ix_pg_vacancy_search_vector_gin",
}

TABLES_SQL = text(
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
)
ENUMS_SQL = text(
    "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
    "WHERE n.nspname = 'public' AND t.typtype = 'e'"
)
INDEXES_SQL = text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")


async def _names(connection: AsyncConnection, sql: text) -> set[str]:
    """Every value of a single-column query, as a set."""
    return {row[0] for row in (await connection.execute(sql)).all()}


@pytest_asyncio.fixture
async def scratch_connection(scratch_database: str) -> AsyncIterator[AsyncConnection]:
    """Connection to the throwaway database, outside any transaction."""
    engine = create_async_engine(scratch_database, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


async def test_upgrade_downgrade_upgrade_on_a_clean_database(
    scratch_database: str, scratch_connection: AsyncConnection
) -> None:
    """The full round trip works on a database that has never seen the schema."""
    await run_alembic(scratch_database, "head")
    assert await _names(scratch_connection, TABLES_SQL) == EXPECTED_TABLES
    assert await _names(scratch_connection, ENUMS_SQL) == EXPECTED_ENUM_TYPES

    await run_alembic(scratch_database, "base")
    assert await _names(scratch_connection, TABLES_SQL) == set()
    # The enum types must go too. Autogenerate would have left them behind, and
    # the next upgrade would fail with "type already exists".
    assert await _names(scratch_connection, ENUMS_SQL) == set()

    # Re-applying is what a downgrade is for; if CREATE TYPE were implicit this
    # is where it would blow up.
    await run_alembic(scratch_database, "head")
    assert await _names(scratch_connection, TABLES_SQL) == EXPECTED_TABLES
    assert await _names(scratch_connection, ENUMS_SQL) == EXPECTED_ENUM_TYPES


async def test_upgrade_creates_the_hand_written_indexes(
    scratch_database: str, scratch_connection: AsyncConnection
) -> None:
    """HNSW, GIN and the partial btrees are created, not just the ORM's own."""
    await run_alembic(scratch_database, "head")

    indexes = await _names(scratch_connection, INDEXES_SQL)

    assert indexes >= EXPECTED_PG_INDEXES


@pytest.mark.parametrize(
    ("index_name", "method"),
    [
        ("ix_pg_vacancy_embedding_hnsw", "hnsw"),
        ("ix_pg_candidate_profile_embedding_hnsw", "hnsw"),
        ("ix_pg_vacancy_search_vector_gin", "gin"),
    ],
)
async def test_special_indexes_use_the_intended_access_method(
    scratch_database: str,
    scratch_connection: AsyncConnection,
    index_name: str,
    method: str,
) -> None:
    """A btree fallback would still answer queries, just far too slowly."""
    await run_alembic(scratch_database, "head")

    definition = await scratch_connection.scalar(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
        {"name": index_name},
    )

    assert definition is not None
    assert f"USING {method}" in definition


async def test_vector_extension_is_created_and_dropped(
    scratch_database: str, scratch_connection: AsyncConnection
) -> None:
    """pgvector has to exist before any vector column, and go away on downgrade."""
    await run_alembic(scratch_database, "head")
    assert await scratch_connection.scalar(
        text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    )

    await run_alembic(scratch_database, "base")
    assert (
        await scratch_connection.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        is None
    )


async def test_embedding_columns_have_the_configured_width(
    scratch_database: str, scratch_connection: AsyncConnection
) -> None:
    """The migration's literal and the model's Vector(dim) must agree."""
    await run_alembic(scratch_database, "head")

    declared = await scratch_connection.scalar(
        text(
            "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "WHERE c.relname = 'vacancy' AND a.attname = 'embedding'"
        )
    )

    assert declared == "vector(1024)"


async def test_search_vector_is_a_stored_generated_column(
    scratch_database: str, scratch_connection: AsyncConnection
) -> None:
    """It must be generated and persisted, not a plain column somebody fills in."""
    await run_alembic(scratch_database, "head")

    generated = await scratch_connection.scalar(
        text(
            "SELECT is_generated FROM information_schema.columns "
            "WHERE table_name = 'vacancy' AND column_name = 'search_vector'"
        )
    )

    assert generated == "ALWAYS"
