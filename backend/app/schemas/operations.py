"""Long operations started from the dashboard, and how they are reported.

The overview screen has one button per step of the daily routine: collect
vacancies, compute the missing vectors, rescore, write letters, read what hh
answered, send what the owner confirmed. Every one of those takes minutes, so a
button starts an operation and the screen polls it; nothing holds a request
open.

Two families share this envelope and must not be confused, because they run in
different processes with different rights:

* **Backend operations** — ``crawl``, ``embed``, ``match``, ``letters``. They
  run inside the API process, anonymously, and touch nothing but the database
  and the public pages the crawler already reads.
* **Agent operations** — ``outcomes`` and ``send``. They need the owner's
  browser and hh session, which the API does not have and must never have
  (CLAUDE.md, «два разных исполнителя»). The API only *records the request*;
  the local watcher (``python -m wwao watch``) claims it over the token-guarded
  seam, runs the agent in its own process and reports back. Until a watcher
  claims it, the operation says so, instead of pretending to run.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class OperationKind(StrEnum):
    """What a button starts."""

    CRAWL = "crawl"
    EMBED = "embed"
    MATCH = "match"
    LETTERS = "letters"
    OUTCOMES = "outcomes"
    SEND = "send"

    @property
    def needs_agent(self) -> bool:
        """True when only the local agent, under the owner's login, can do this."""
        return self in {OperationKind.OUTCOMES, OperationKind.SEND}


class OperationStatus(StrEnum):
    """Where an operation is.

    ``waiting_agent`` exists only for the agent family: the request is recorded
    and no watcher has claimed it yet. It is not ``queued`` because the remedy
    differs — a queued backend operation starts by itself, a request nobody
    claims needs the owner to start the watcher.
    """

    QUEUED = "queued"
    WAITING_AGENT = "waiting_agent"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """True when nothing more will happen to this operation."""
        return self in {
            OperationStatus.SUCCESS,
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
        }


class OperationRead(BaseModel):
    """One operation as the dashboard polls it."""

    id: UUID
    kind: OperationKind
    status: OperationStatus
    #: One Russian line for the person watching. Never a traceback.
    message: str
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    #: How far it got, when the operation can count: vectors written, letters
    #: saved, applications read. ``None`` means "cannot count", never zero.
    done: int | None = None
    #: How much there is, when that is known up front. ``None`` otherwise.
    total: int | None = None
    #: Short result lines once the operation has finished, in Russian.
    report: list[str] = Field(default_factory=list)
    error: str | None = None


class OperationsState(BaseModel):
    """Every operation kind, with its newest run and whether it may start now."""

    operations: list[OperationRead] = Field(default_factory=list)
    #: Kinds that are running (or waiting for the agent) right now. The
    #: dashboard disables their buttons from this rather than re-deriving the
    #: rule from the list.
    busy: list[OperationKind] = Field(default_factory=list)
    #: When a local watcher last asked for work. ``None`` means no watcher has
    #: been seen by this process, which is what the agent buttons have to say.
    agent_seen_at: datetime | None = None


class StartOperation(BaseModel):
    """``POST /api/v1/operations``: which button was pressed."""

    kind: OperationKind


class AgentClaim(BaseModel):
    """What the watcher receives when it asks for work."""

    operation: OperationRead | None = None


class AgentProgress(BaseModel):
    """What the watcher reports about an operation it claimed."""

    status: OperationStatus
    message: str | None = Field(default=None, max_length=2000)
    report: list[str] = Field(default_factory=list, max_length=50)
