"""Crawls as jobs: accepting one, running it, and answering for it meanwhile.

``POST /pipeline/run`` used to await the whole crawl. That was right while every
source was a bounded feed and wrong the moment ``hh`` landed: it walks a corpus
of some fourteen thousand pages a city, takes a slice per run, and a slice at its
own polite rate is roughly twenty minutes. Nothing sensible holds an HTTP
connection open for that — not a browser, not a reverse proxy, not a person.

So the endpoint accepts the crawl and hands back a handle. Four decisions make
that work, and each of them had an alternative worth rejecting out loud.

**"Enqueue" means an asyncio task in this process, and the queue is a dict.**
There is no Celery, RQ or Arq in the dependency list, and CLAUDE.md says not to
add one without need. A broker would buy durability across restarts and a worker
that can be scaled out — and would cost a second process, a second deployment
unit and a message contract, for a single-user dashboard whose crawl is already
just an ``async`` function this process is perfectly able to run. The resume
upload made the same call one phase earlier (``BackgroundTasks`` plus a status
to poll), and this is the same shape with a handle of its own.

**The job state is NOT a row, and ``PipelineRun`` could not have carried it.**
That was checked before anything was added, because a table would have been the
obvious move. It does not fit:

* ``pipeline_run`` is one row per SOURCE, opened by ``runner._run_source`` after
  eligibility has been decided. A crawl of four sources is four rows and no
  single id to hand back.
* There is no row at all during the window the caller most needs an answer for —
  between "accepted" and "the first source starts fetching" — and none ever for
  a dry run, or for a source that sat the run out.
* A row would outlive the only thing that can finish it. The task advancing the
  job lives in this process; if the process dies, a ``running`` row stays
  ``running`` for ever and every reader has to guess whether it is alive. The
  brief's own words: a row stuck at "running" is worse than no row.

Keeping the job in memory makes that last failure impossible rather than
recoverable. A restart loses the job, and losing it is the honest report: the
crawl died with the process, so its handle should too. What a crawl DID remains
durable in ``pipeline_run`` and ``vacancy``, which is where it already lived.

**A second crawl while one is in flight is refused (409), not queued.** Two
concurrent crawls of one source is a real hazard, not a theoretical one: sources
are process singletons (``registry._INSTANCES``), so both runs would share one
rate limiter while making two streams of requests against a host that has
already answered one of our runs with a captcha, and ``hh`` would read one
crawl position from ``source_state``, fetch the same slice twice and advance it
once. Refusing is per process and not per source, for two reasons: a request
that names no source means *every enabled source*, so "do these two overlap"
has no answer in the common case; and every crawl ends in one embedding pass
over the deduplicated rows, which disjoint source sets still share. The refusal
names the running job so the caller can poll it instead of retrying blind.

A dry run is exempt from all of this, in both directions. It decides everything
and fetches nothing, so it cannot double a request rate, and it is exactly what
somebody wants to look at *while* a long crawl is running. It is therefore run
inline and answered in the request, with the same envelope.

**One process is the boundary of every guarantee here.** Under ``uvicorn
--workers N`` each worker gets its own registry, the refusal above stops
holding, and a job id issued by one worker is a 404 at the next. The API is a
single process today (``make dev`` runs one uvicorn, and there is no app image
yet), and this module is written so that the day that changes, this paragraph is
the thing to reread rather than a mystery to debug.
"""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Final
from uuid import UUID, uuid4

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.pipeline import runner
from app.schemas.pipeline_job import PipelineJobList, PipelineJobRead, PipelineJobStatus
from app.schemas.source import RunResponse
from app.services import sources as sources_service
from app.sources.registry import get_source

logger = get_logger(__name__)

#: Finished jobs kept for polling. A client that asked for a crawl and then
#: reloaded its page has to be able to find it again; a client that has been
#: away for a hundred crawls should read ``pipeline_run`` instead, which is the
#: durable record. Small on purpose: this is a cache of handles, not history.
JOB_HISTORY: Final[int] = 20

#: Longest a caller may ask the request to wait for the crawl to finish. Past a
#: minute the answer is "poll": bounded feeds finish in seconds, and hh will not
#: finish inside any number a proxy will tolerate.
MAX_WAIT_SECONDS: Final[int] = 60


class PipelineBusyError(AppError):
    """A crawl is already running and a second one was asked for."""

    status_code = HTTPStatus.CONFLICT
    title = "A crawl is already running"
    problem_type = "pipeline-busy"


class PipelineJobNotFoundError(AppError):
    """No job with that id in this process."""

    status_code = HTTPStatus.NOT_FOUND
    title = "Pipeline job not found"
    problem_type = "pipeline-job-unknown"


@dataclass(slots=True)
class _Job:
    """The mutable half of a job. Never leaves this module."""

    id: UUID
    dry_run: bool
    force: bool
    source_slugs: tuple[str, ...] | None
    status: PipelineJobStatus
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    report: RunResponse | None = None
    error: str | None = None
    #: Set exactly once, when the job reaches a terminal state. What a caller
    #: waiting on the request is actually waiting for.
    done: asyncio.Event = field(default_factory=asyncio.Event)
    #: Held so the task is not garbage collected mid-crawl: the event loop keeps
    #: only a weak reference to it.
    task: asyncio.Task[None] | None = None


class JobRegistry:
    """The jobs this process knows about, in the order they were accepted.

    Not thread safe and does not need to be: everything here runs on the one
    event loop, and :meth:`accept` does its check and its insert without an
    ``await`` between them, so two requests cannot both find the slot free.
    """

    def __init__(self) -> None:
        self._jobs: OrderedDict[UUID, _Job] = OrderedDict()

    def in_flight(self) -> _Job | None:
        """The crawl currently running, if there is one.

        Dry runs are not crawls for this purpose: they fetch nothing, so they
        neither block a real run nor are blocked by one.
        """
        for job in self._jobs.values():
            if not job.dry_run and not job.status.is_terminal:
                return job
        return None

    def accept(self, *, source_slugs: tuple[str, ...] | None, dry_run: bool, force: bool) -> _Job:
        """Register a job, or refuse because one is already running."""
        if not dry_run:
            running = self.in_flight()
            if running is not None:
                raise PipelineBusyError(
                    "Обход уже идёт. Дождитесь окончания или посмотрите, что он "
                    "успел сделать: второй одновременный обход удвоил бы нагрузку "
                    "на те же сайты.",
                    job_id=str(running.id),
                )
        job = _Job(
            id=uuid4(),
            dry_run=dry_run,
            force=force,
            source_slugs=source_slugs,
            status=PipelineJobStatus.QUEUED,
            queued_at=datetime.now(UTC),
        )
        self._jobs[job.id] = job
        self._evict()
        return job

    def get(self, job_id: UUID) -> _Job | None:
        """One job, or None when this process never had it or forgot it."""
        return self._jobs.get(job_id)

    def recent(self, limit: int) -> list[_Job]:
        """Newest first, like the run history next to it."""
        return list(reversed(self._jobs.values()))[:limit]

    def _evict(self) -> None:
        """Forget the oldest finished jobs once there are too many.

        Only finished ones: dropping a running job would lose the handle on a
        crawl that is still making requests, and the caller would have no way to
        find out how it ended.
        """
        for job_id, job in list(self._jobs.items()):
            if len(self._jobs) <= JOB_HISTORY:
                return
            if job.status.is_terminal:
                del self._jobs[job_id]


#: Process-wide, for the reason the module docstring gives. Replaced wholesale
#: in tests, which is also what "the process restarted" looks like from here.
_registry = JobRegistry()


async def request_run(
    *,
    source_slugs: list[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    wait_seconds: int = 0,
) -> PipelineJobRead:
    """Accept a crawl and answer with its job, waiting only if asked to.

    ``wait_seconds`` is how the synchronous path survives for the sources that
    are bounded feeds: ``?source=arbeitnow&wait_seconds=30`` still returns the
    counters in one request, one level down under ``report``. It is not the
    default because the default has to be right for hh.
    """
    slugs = _validated(source_slugs)
    job = _registry.accept(source_slugs=slugs, dry_run=dry_run, force=force)
    logger.info(
        "pipeline.job_accepted",
        job_id=str(job.id),
        sources=list(slugs) if slugs is not None else None,
        dry_run=dry_run,
        force=force,
    )

    if dry_run:
        # Answered in the request: it fetches nothing, so there is nothing to
        # wait for and nothing to protect anybody from.
        await _execute(job)
    else:
        job.task = asyncio.create_task(_execute(job), name=f"pipeline-run-{job.id}")
        await _wait(job, wait_seconds)
    return view(job)


def read_job(job_id: UUID) -> PipelineJobRead:
    """One job, or a 404 that explains why it might be missing."""
    job = _registry.get(job_id)
    if job is None:
        raise PipelineJobNotFoundError(
            "Задача обхода не найдена. Задачи живут в памяти процесса: после "
            "перезапуска их не остаётся. История самих запусков при этом "
            "сохраняется и доступна отдельно."
        )
    return view(job)


def list_jobs(limit: int = JOB_HISTORY) -> PipelineJobList:
    """The jobs this process remembers, newest first."""
    return PipelineJobList(
        jobs=[view(job) for job in _registry.recent(limit)],
        busy=_registry.in_flight() is not None,
    )


def _validated(source_slugs: list[str] | None) -> tuple[str, ...] | None:
    """Check the requested slugs before a job is opened for them.

    A typo used to be answered synchronously, by the registry, and it still is:
    turning it into a job that fails a moment later would mean the caller has to
    poll to learn they misspelled a word, and — worse — a misspelled request
    would take the single in-flight slot from a crawl that could have run.
    """
    if not source_slugs:
        return None
    for slug in source_slugs:
        get_source(slug)
    return tuple(source_slugs)


async def _execute(job: _Job) -> None:
    """Run the crawl and record how it ended, whatever happens.

    Every exit sets a terminal status. A job whose task has ended and whose
    status still says ``running`` would be the in-memory version of the stuck
    row this design exists to avoid, so the ``finally`` closes that door even
    for a failure nobody anticipated.
    """
    job.status = PipelineJobStatus.RUNNING
    job.started_at = datetime.now(UTC)
    try:
        report = await runner.run_pipeline(
            source_slugs=list(job.source_slugs) if job.source_slugs is not None else None,
            dry_run=job.dry_run,
            force=job.force,
        )
    except asyncio.CancelledError:
        # The process is going down, or somebody cancelled the task. Recorded
        # rather than swallowed, and re-raised so the loop's shutdown works.
        job.status = PipelineJobStatus.CANCELLED
        job.error = "Обход прерван: задачу остановили."
        logger.warning("pipeline.job_cancelled", job_id=str(job.id))
        raise
    except AppError as exc:
        # A domain failure the pipeline states in its own words — no active
        # profile, a broken configuration. Those words are written for the
        # person who has to fix it, so they are passed through unchanged.
        job.status = PipelineJobStatus.FAILED
        job.error = exc.detail
        logger.warning("pipeline.job_failed", job_id=str(job.id), problem_type=exc.problem_type)
    except Exception as exc:
        # Anything else is a bug. The traceback goes to the log, where it is
        # useful; the response gets a sentence, because an exception's text can
        # carry a URL with a key in it.
        job.status = PipelineJobStatus.FAILED
        job.error = "Обход прервала ошибка. Подробности записаны в журнал."
        logger.exception("pipeline.job_crashed", job_id=str(job.id), error=type(exc).__name__)
    else:
        job.status = PipelineJobStatus.SUCCESS
        job.report = sources_service.to_response(report)
        logger.info(
            "pipeline.job_finished",
            job_id=str(job.id),
            found=report.found,
            new=report.new,
            seconds=round(report.duration_seconds, 1),
        )
    finally:
        if not job.status.is_terminal:  # pragma: no cover - only a BaseException gets here
            job.status = PipelineJobStatus.FAILED
            job.error = "Обход прерван неизвестной ошибкой."
        job.finished_at = datetime.now(UTC)
        job.done.set()


async def _wait(job: _Job, seconds: int) -> None:
    """Give the crawl up to ``seconds`` to finish before answering."""
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(job.done.wait(), timeout=min(seconds, MAX_WAIT_SECONDS))
    except TimeoutError:
        # Not an error: the caller asked to wait a while, not to wait for ever.
        # They get the running job and poll it, which is the normal path anyway.
        logger.info("pipeline.job_still_running", job_id=str(job.id), waited=seconds)


def view(job: _Job) -> PipelineJobRead:
    """The wire form of a job, with the clock read at the moment of asking."""
    return PipelineJobRead(
        id=job.id,
        status=job.status,
        message=_message(job),
        dry_run=job.dry_run,
        force=job.force,
        source_slugs=list(job.source_slugs) if job.source_slugs is not None else None,
        queued_at=job.queued_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        duration_seconds=_elapsed(job),
        report=job.report,
        error=job.error,
    )


def _elapsed(job: _Job) -> float | None:
    """Seconds spent crawling: so far while it runs, in total once it stops."""
    if job.started_at is None:
        return None
    end = job.finished_at or datetime.now(UTC)
    return round((end - job.started_at).total_seconds(), 3)


def _message(job: _Job) -> str:
    """One line about this job, for the dashboard to render as it is."""
    if job.status is PipelineJobStatus.QUEUED:
        return "Обход принят, сейчас начнётся."
    if job.status is PipelineJobStatus.RUNNING:
        # No number here on purpose: how long a duration reads best is the
        # dashboard's decision, and ``duration_seconds`` is right beside this.
        return (
            "Обход идёт: сначала источники, потом векторы для новых вакансий — на "
            "процессоре это несколько минут. Полный обход hh — около двадцати минут; "
            "что успел сделать каждый источник, видно в истории запусков."
        )
    if job.status is PipelineJobStatus.SUCCESS and job.report is not None:
        if job.dry_run:
            return f"План построен: {job.report.plan.queries} запросов, ничего не скачивалось."
        return (
            f"Обход завершён: найдено {job.report.found}, "
            f"новых {job.report.new}, дублей схлопнуто {job.report.duplicates}."
        )
    return job.error or "Обход завершён."
