"""Helpers shared by tests.

Imported as a plain top-level module (``from helpers import ...``): pytest puts
``backend/tests`` on sys.path because it holds no ``__init__.py``, and making it
a package instead would collide with the ``app`` package layout.
"""

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, make_url

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def alembic_config(database_url: str) -> Config:
    """Alembic config pointed at an explicit database."""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(REPO_ROOT / "backend" / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


async def run_alembic(database_url: str, target: str) -> None:
    """Run an Alembic command from inside an async test.

    ``command.upgrade`` drives the async env.py through ``asyncio.run``, which
    raises if a loop is already running. A worker thread has no loop, so this is
    the one safe way to call it from an async test.
    """
    action = command.downgrade if target == "base" else command.upgrade
    await asyncio.to_thread(action, alembic_config(database_url), target)


def maintenance_url(database_url: str) -> URL:
    """Same server, connected to the always-present ``postgres`` database.

    CREATE DATABASE cannot run while connected to the database being created,
    nor inside a transaction.
    """
    return make_url(database_url).set(database="postgres")
