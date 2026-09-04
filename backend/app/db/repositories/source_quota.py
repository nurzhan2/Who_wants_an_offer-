"""The metered-request ledger, one row per source per UTC day.

It exists because nothing else can answer the question. ``pipeline_run`` counts
runs; a metered API charges per *page*, and a run that died halfway through
pagination spent real credits and left no record of them. A number derived from
run history therefore undercounts exactly when the limit is about to be hit,
which is the one moment it matters.

The day is the vendors' own boundary — JSearch's Basic plan is 100 requests
reset at midnight UTC — so keying on it makes the increment a single atomic
statement rather than a read, an add and a write with a race in the middle.
"""

from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SourceQuota


def utc_day(now: datetime | None = None) -> date:
    """Today, on the boundary the vendors reset against."""
    return (now or datetime.now(UTC)).astimezone(UTC).date()


class SourceQuotaRepository:
    """Reads and writes for the daily credit counter."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def spend(self, source_slug: str, *, day: date | None = None, amount: int = 1) -> int:
        """Record requests as SENT and return the new total for the day.

        Called before the request leaves, never after it succeeds: a 500 has
        already cost a credit, and a counter that only tracks successes walks
        straight past the limit and into a 429 with no explanation for it.

        One statement, so two connectors running concurrently cannot both read
        the same total and write it back.
        """
        stmt = (
            pg_insert(SourceQuota)
            .values(source_slug=source_slug, day=day or utc_day(), used=amount)
            .on_conflict_do_update(
                index_elements=[SourceQuota.source_slug, SourceQuota.day],
                set_={"used": SourceQuota.used + amount, "updated_at": func.now()},
            )
            .returning(SourceQuota.used)
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def used_today(self, source_slug: str, *, day: date | None = None) -> int:
        """Requests already spent today. Zero when the source has not run."""
        stmt = select(SourceQuota.used).where(
            SourceQuota.source_slug == source_slug,
            SourceQuota.day == (day or utc_day()),
        )
        return int((await self.session.execute(stmt)).scalar_one_or_none() or 0)

    async def remaining(
        self, source_slug: str, quota: int | None, *, day: date | None = None
    ) -> int | None:
        """How many requests are left, or None when the source is not metered."""
        if quota is None:
            return None
        return max(0, quota - await self.used_today(source_slug, day=day))
