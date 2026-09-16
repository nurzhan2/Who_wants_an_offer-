"""Account-level notices every dashboard screen shows. See ``app.services.notices``."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class NoticeKind(StrEnum):
    """What a notice is about. One member today; the envelope is for the next."""

    RESUME_VISIBILITY = "resume_visibility"


class Notice(BaseModel):
    """One thing the owner has to know on every screen."""

    kind: NoticeKind
    title: str
    #: hh's own sentence, verbatim, so the setting can be found by its name.
    quote: str | None = None
    body: str
    #: True when newer evidence suggests the cause has gone. The notice is
    #: still returned, so the screen can say so instead of going quiet.
    resolved: bool = False
    #: How many recorded sends carried hh's sentence.
    applications: int = 0
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


class Notices(BaseModel):
    """``GET /api/v1/notices``."""

    items: list[Notice] = Field(default_factory=list)
