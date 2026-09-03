"""Async SQLAlchemy engine and session lifecycle.

Everything here is async by design: there is no sync ``Session`` anywhere in
the codebase, and ``create_all`` is never called outside tests.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

engine: AsyncEngine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    future=True,
)

session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session that is always closed.

    The session is *not* committed automatically — services decide when a unit
    of work ends, so a read-only request never opens a write transaction.
    """
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close every pooled connection; called from the application lifespan."""
    await engine.dispose()
