"""Alembic environment, async engine, URL taken from application settings.

Two knobs matter here and are both on:

* ``compare_type`` — autogenerate notices a changed column type. Without it a
  ``Numeric(12, 2)`` silently stays a ``Numeric(10, 2)`` forever.
* ``compare_server_default`` — the same for defaults.

``render_as_batch`` is deliberately off: it exists for SQLite's inability to
ALTER, and this project does not support SQLite.
"""

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings
from app.db import models  # noqa: F401  - imported for its side effect: registers every table
from app.db.base import Base

config = context.config

# Tests point migrations at a throwaway database by setting sqlalchemy.url on
# the Config before invoking a command; everything else falls back to settings,
# so no connection string ever lives in a tracked file.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(
    obj: Any,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: Any,
) -> bool:
    """Keep autogenerate away from objects it cannot model.

    HNSW, GIN and partial indexes have no representation in the ORM metadata,
    so autogenerate would propose dropping them on every run. They are written
    as raw SQL in the migrations and named with an ``ix_pg_`` prefix; anything
    carrying that prefix is hand-managed and off limits.
    """
    return not (type_ == "index" and name is not None and name.startswith("ix_pg_"))


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of talking to a database."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations on an already-established synchronous connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations through it."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for the online mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
