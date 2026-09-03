"""Pipeline run contracts."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.db.enums import PipelineRunStatus
from app.schemas.common import ReadModel


class PipelineRunCreate(BaseModel):
    """Open a run record before a source starts fetching."""

    source_slug: str = Field(min_length=1, max_length=50)
    status: PipelineRunStatus = PipelineRunStatus.RUNNING


class PipelineRunFinish(BaseModel):
    """Close a run record with its counters and whatever went wrong."""

    status: PipelineRunStatus
    found: int = Field(default=0, ge=0)
    new: int = Field(default=0, ge=0)
    updated: int = Field(default=0, ge=0)
    #: One entry per failure. A broken source must not abort the whole run,
    #: so its errors are recorded here instead of raising out of the pipeline.
    errors: list[dict[str, Any]] = Field(default_factory=list)


class PipelineRunRead(ReadModel):
    """One source run."""

    id: UUID
    source_slug: str
    status: PipelineRunStatus
    started_at: datetime
    finished_at: datetime | None
    found: int
    new: int
    updated: int
    errors: list[dict[str, Any]]

    @property
    def duration_seconds(self) -> float | None:
        """Wall time of the run, or None while it is still going."""
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()
