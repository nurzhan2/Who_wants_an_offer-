"""The job envelope: what ``POST /api/v1/pipeline/run`` answers with.

By subject this belongs beside :class:`~app.schemas.pipeline.PipelineRunRead`,
and it is here instead for one mechanical reason. A finished job carries the run
report; ``RunResponse`` lives in ``app.schemas.source``; and that module already
imports ``app.schemas.pipeline`` for ``SourceStatus.last_run``. Declaring the job
there would close an import cycle whose failure depends on which of the two
modules is imported first — a bug that passes every test that happens to import
them in the lucky order and breaks the one process that does not. A third module
has no such edge, and needs no forward reference, no ``model_rebuild`` and no
import-order convention for anybody to remember.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.source import RunResponse


class PipelineJobStatus(StrEnum):
    """Where a crawl job is.

    Deliberately not ``PipelineRunStatus``. That one is a native PostgreSQL enum
    describing what one SOURCE did, where ``partial`` is a real answer — some
    pages arrived and some did not. This describes the job, which is the handle
    on a whole crawl and never reaches the database, so it can carry the two
    members a queue needs, ``queued`` and ``cancelled``, without an ``ALTER TYPE``
    and without teaching the schema about a thing the schema does not store.

    A job is ``success`` when the crawl ran to the end, even if a source inside
    it failed: whether one connector broke is answered per source, in ``report``
    and in the ``pipeline_run`` rows, and flattening that into the job's status
    would lose which source it was.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    #: The process was asked to stop while the crawl was running.
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """True when nothing more will happen to this job."""
        return self in {
            PipelineJobStatus.SUCCESS,
            PipelineJobStatus.FAILED,
            PipelineJobStatus.CANCELLED,
        }


class PipelineJobRead(BaseModel):
    """One crawl, as the caller polling it sees it."""

    id: UUID
    status: PipelineJobStatus
    #: One line for the person watching the dashboard, in Russian like every
    #: other user-facing string in this API. Never a traceback and never a
    #: credential: this is rendered in a browser and screenshotted into chats.
    message: str
    dry_run: bool = False
    force: bool = False
    #: Sources named in the request. ``None`` means "every enabled source",
    #: which is what an empty ``?source=`` list asks for and is why this is not
    #: flattened to an empty list.
    source_slugs: list[str] | None = None
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: Wall time so far while the crawl runs, total once it has finished. A
    #: caller polling a twenty-minute hh crawl has nothing else to show, so it
    #: is computed on read rather than only written at the end.
    duration_seconds: float | None = None
    #: The counters, present once the crawl finished. Exactly the body the
    #: endpoint used to return synchronously, one level down.
    report: RunResponse | None = None
    #: Why it ended badly, in words meant for a person. ``None`` unless the
    #: status is ``failed`` or ``cancelled``.
    error: str | None = None


class PipelineJobList(BaseModel):
    """The jobs this process still remembers, newest first."""

    jobs: list[PipelineJobRead] = Field(default_factory=list)
    #: True while a crawl is running. The dashboard needs this to disable its
    #: own button, and computing it from the list would make the client repeat
    #: the rule that decides what "in flight" means.
    busy: bool = False
