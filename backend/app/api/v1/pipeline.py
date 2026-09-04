"""Starting a crawl and reading what previous ones did."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.pipeline_run import PipelineRunRepository
from app.db.session import get_session
from app.pipeline import runner
from app.schemas.pipeline import PipelineRunRead
from app.schemas.source import RunResponse
from app.services import sources as sources_service

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.post(
    "/run",
    response_model=RunResponse,
    summary="Crawl the enabled sources once",
)
async def run(
    source_slugs: Annotated[list[str] | None, Query(alias="source")] = None,
    dry_run: Annotated[bool, Query()] = False,
    force: Annotated[bool, Query()] = False,
) -> RunResponse:
    """Run the pipeline and answer with what it did.

    Synchronous rather than a background task, and that is the right shape for
    now: a run is bounded by the query cap and the per-source rate limits, the
    caller is a person who wants to see the counters, and the alternative —
    scheduling it and polling — is phase 9's problem, when there is a scheduler
    to hang it on.

    ``dry_run`` decides everything and fetches nothing, which is how you check a
    plan before it spends a metered request. ``force`` skips only a short
    cooldown; a source whose terms impose a long one stays where it is.
    """
    report = await runner.run_pipeline(source_slugs=source_slugs, dry_run=dry_run, force=force)
    return sources_service.to_response(report)


@router.get(
    "/runs",
    response_model=list[PipelineRunRead],
    summary="Recent runs, newest first",
)
async def list_runs(
    session: Annotated[AsyncSession, Depends(get_session)],
    source: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[PipelineRunRead]:
    """Run history, optionally for one source."""
    return await sources_service.recent_runs(session, source_slug=source, limit=limit)


@router.get(
    "/runs/{run_id}",
    response_model=PipelineRunRead,
    summary="One run",
)
async def read_run(
    run_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PipelineRunRead:
    """One run record, including whatever went wrong during it."""
    found = await PipelineRunRepository(session).get(run_id)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return PipelineRunRead.model_validate(found)
