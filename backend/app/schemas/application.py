"""Application tracker contracts."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.db.enums import ApplicationStatus
from app.schemas.common import ReadModel


class ApplicationCreate(BaseModel):
    """Start tracking a vacancy."""

    vacancy_id: UUID
    status: ApplicationStatus = ApplicationStatus.SAVED
    applied_at: datetime | None = None
    notes: str | None = None
    cover_letter: str | None = None


class ApplicationUpdate(BaseModel):
    """Move an application along the kanban, or annotate it."""

    status: ApplicationStatus | None = None
    applied_at: datetime | None = None
    notes: str | None = None
    cover_letter: str | None = None


class ApplicationRead(ReadModel):
    """Tracker entry."""

    id: UUID
    vacancy_id: UUID
    status: ApplicationStatus
    applied_at: datetime | None
    notes: str | None
    cover_letter: str | None
    created_at: datetime
    updated_at: datetime


class ApplicationStats(BaseModel):
    """How many applications sit in each status."""

    by_status: dict[ApplicationStatus, int] = Field(default_factory=dict)
    total: int = 0
