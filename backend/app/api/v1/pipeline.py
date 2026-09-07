"""Starting a crawl and reading what it and previous ones did."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.pipeline_run import PipelineRunRepository
from app.db.session import get_session
from app.schemas.pipeline import PipelineRunRead
from app.schemas.pipeline_job import PipelineJobList, PipelineJobRead
from app.services import pipeline as pipeline_service
from app.services import sources as sources_service

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.post(
    "/run",
    response_model=PipelineJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enqueue a crawl and answer with a job to poll",
)
async def run(
    request: Request,
    response: Response,
    source_slugs: Annotated[list[str] | None, Query(alias="source")] = None,
    dry_run: Annotated[bool, Query()] = False,
    force: Annotated[bool, Query()] = False,
    wait_seconds: Annotated[int, Query(ge=0, le=pipeline_service.MAX_WAIT_SECONDS)] = 0,
) -> PipelineJobRead:
    """Accept a crawl and hand back a job, instead of holding the connection.

    This used to await the whole run, which was the right shape while every
    source was a bounded feed and stopped being right when ``hh`` landed: it
    walks a corpus rather than a feed, takes a slice per run, and a slice at its
    own polite rate is roughly twenty minutes. Nothing holds an HTTP connection
    open for that.

    **What "enqueue" means.** An asyncio task in this process and a dict of
    handles — no broker, because there is none in the dependency list and a
    single-user dashboard does not earn a second deployment unit for work this
    process can already run. **The job is not a row**, and ``PipelineRun`` could
    not have become one: it is per source, opened only once a source starts
    fetching, absent entirely for a dry run, and — the reason that settles it —
    a row would outlive the only task able to finish it, leaving ``running``
    behind for ever after a restart. The whole argument, with what was rejected,
    is in ``app.services.pipeline``.

    **What the caller polls.** ``GET /pipeline/jobs/{id}``, whose URL is in the
    ``Location`` header of this response. While the crawl is in flight that
    shows ``running`` and the seconds so far; per-source progress appears in
    ``GET /pipeline/runs`` as each source opens and closes its own row, which is
    the record that was already there and outlives the process.

    **A second crawl while one is running is refused with 409**, naming the
    running job. Not queued and not run alongside: sources are process
    singletons sharing one rate limiter, so a second crawl would double the
    request rate against a host that has already answered one of our runs with
    a captcha, and hh would fetch the same slice twice. A ``dry_run`` is exempt
    both ways — it fetches nothing, and inspecting the plan is exactly what
    somebody does while a long crawl runs — so it is executed inline and
    answered here with its report already filled in.

    **If the process dies mid-crawl the job dies with it** and its id becomes a
    404 that says so. That is the intended answer rather than a gap: what the
    crawl managed to do stays in ``pipeline_run`` and ``vacancy``, and a handle
    that outlived the thing it is a handle on could only lie.

    ``wait_seconds`` keeps the old synchronous path for the sources that are
    bounded feeds: ``?source=arbeitnow&wait_seconds=30`` answers in one request
    with the same counters, now under ``report``. 200 when the job finished
    inside the wait, 202 when it did not.

    ``dry_run`` decides everything and fetches nothing, which is how you check a
    plan before it spends a metered request. ``force`` skips only a short
    cooldown; a source whose terms impose a long one stays where it is.
    """
    job = await pipeline_service.request_run(
        source_slugs=source_slugs, dry_run=dry_run, force=force, wait_seconds=wait_seconds
    )
    response.headers["Location"] = str(request.url_for("read_job", job_id=job.id))
    if job.status.is_terminal:
        response.status_code = status.HTTP_200_OK
    return job


@router.get(
    "/jobs",
    response_model=PipelineJobList,
    summary="Crawl jobs this process still remembers",
)
async def list_jobs(
    limit: Annotated[int, Query(ge=1, le=pipeline_service.JOB_HISTORY)] = 20,
) -> PipelineJobList:
    """Recent jobs, newest first, and whether a crawl is running right now.

    Short by construction: these are handles on crawls this process started, not
    history. History is ``GET /pipeline/runs``, which is in the database.
    """
    return pipeline_service.list_jobs(limit)


@router.get(
    "/jobs/{job_id}",
    response_model=PipelineJobRead,
    summary="One crawl job",
)
async def read_job(job_id: UUID) -> PipelineJobRead:
    """The job to poll while a crawl runs, and its report once it is done.

    A 404 here means this process does not have that job: either the id is
    wrong, or the server restarted, or the crawl is old enough to have been
    forgotten. The response says so, because "unknown id" and "your run is gone"
    need different reactions from whoever is reading.
    """
    return pipeline_service.read_job(job_id)


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
