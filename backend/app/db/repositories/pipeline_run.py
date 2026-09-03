"""Pipeline run bookkeeping."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import PipelineRunStatus
from app.db.models import PipelineRun
from app.schemas.pipeline import PipelineRunCreate, PipelineRunFinish


class PipelineRunRepository:
    """Reads and writes for per-source run records."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def start(self, run: PipelineRunCreate) -> PipelineRun:
        """Open a run record before a source starts fetching."""
        instance = PipelineRun(source_slug=run.source_slug, status=run.status)
        self.session.add(instance)
        await self.session.flush()
        return instance

    async def finish(self, run_id: UUID, outcome: PipelineRunFinish) -> PipelineRun | None:
        """Close a run record with its counters and any errors."""
        instance = await self.session.get(PipelineRun, run_id)
        if instance is None:
            return None
        instance.status = outcome.status
        instance.found = outcome.found
        instance.new = outcome.new
        instance.updated = outcome.updated
        instance.errors = list(outcome.errors)
        instance.finished_at = func.now()
        await self.session.flush()
        return instance

    async def get(self, run_id: UUID) -> PipelineRun | None:
        """One run record."""
        return await self.session.get(PipelineRun, run_id)

    async def recent(self, *, source_slug: str | None = None, limit: int = 50) -> list[PipelineRun]:
        """Most recent runs, newest first."""
        stmt = select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(limit)
        if source_slug is not None:
            stmt = stmt.where(PipelineRun.source_slug == source_slug)
        return list((await self.session.execute(stmt)).scalars().all())

    async def last_successful(self, source_slug: str) -> PipelineRun | None:
        """Last run of a source that did not fail.

        Incremental crawling starts from this run's timestamp, which is why a
        partial run counts: it did fetch something.
        """
        stmt = (
            select(PipelineRun)
            .where(
                PipelineRun.source_slug == source_slug,
                PipelineRun.status.in_((PipelineRunStatus.SUCCESS, PipelineRunStatus.PARTIAL)),
            )
            .order_by(PipelineRun.started_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def latest_per_source(self, slugs: Sequence[str] | None = None) -> list[PipelineRun]:
        """One row per source: its most recent run.

        Powers the sources page. DISTINCT ON keeps it to a single query rather
        than one per registered connector.
        """
        stmt = (
            select(PipelineRun)
            .distinct(PipelineRun.source_slug)
            .order_by(PipelineRun.source_slug, PipelineRun.started_at.desc())
        )
        if slugs:
            stmt = stmt.where(PipelineRun.source_slug.in_(slugs))
        return list((await self.session.execute(stmt)).scalars().all())
