"""Where a source got to last time, keyed by whatever the connector calls it.

Two methods and no cleverness, on purpose. The value is JSONB the connector
owns, so this module has nothing to validate and nothing to interpret; what it
does own is the guarantee that a write is one statement, because two sources —
or two slices of one crawl — can be updating neighbouring keys at the same
moment and a read-modify-write here would lose one of them.
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SourceState


class SourceStateRepository:
    """Reads and writes for one source's saved crawl position."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, source_slug: str, key: str) -> dict[str, Any] | None:
        """The stored value, or None when this source has never written this key.

        ``Any`` in the value type with the reason CLAUDE.md asks for: the shape
        belongs to the connector that wrote it, and the connector validates it
        with its own model on the way back in.
        """
        stmt = select(SourceState.value).where(
            SourceState.source_slug == source_slug,
            SourceState.key == key,
        )
        stored = (await self.session.execute(stmt)).scalar_one_or_none()
        return dict(stored) if stored is not None else None

    async def set(self, source_slug: str, key: str, value: dict[str, Any]) -> None:
        """Store a value, replacing whatever was there.

        Replaces rather than merges: a connector that wants to keep a field is
        the one that knows it, and a silent merge here would resurrect a key its
        author had deliberately dropped.
        """
        stmt = (
            pg_insert(SourceState)
            .values(source_slug=source_slug, key=key, value=value)
            .on_conflict_do_update(
                index_elements=[SourceState.source_slug, SourceState.key],
                set_={"value": value, "updated_at": func.now()},
            )
        )
        await self.session.execute(stmt)
