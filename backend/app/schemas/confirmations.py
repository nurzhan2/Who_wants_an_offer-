"""The confirmation modal's wire types. See ``app.services.confirmations``."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.agent import QueueItem


class ConfirmationState(BaseModel):
    """A confirmation on record, and whether the agent would still use it."""

    confirmed_at: datetime
    valid: bool
    #: Why it no longer applies, in Russian, when ``valid`` is false.
    reason: str | None = None


class ConfirmationCard(BaseModel):
    """Everything the owner reads before confirming one application."""

    vacancy_id: UUID
    title: str
    company: str | None = None
    #: The source the agent would act on, or the first source when it cannot.
    source: str | None = None
    #: The page the agent opens — or, for other sources, where to apply by hand.
    url: str | None = None
    external_id: str | None = None
    published_at: datetime | None = None
    last_seen_at: datetime | None = None
    is_active: bool = True
    #: hh's resume-visibility sentence, while the recorded sends still carry it.
    resume_notice: str | None = None
    #: Every reason this card cannot be confirmed now, in Russian. Empty means
    #: it can.
    blockers: list[str] = Field(default_factory=list)
    #: Exactly what the agent would be handed: letter, score and explanation,
    #: ATS summary, hh's lines. ``None`` while there are blockers.
    item: QueueItem | None = None
    #: The digest to send back when confirming. ``None`` while there are
    #: blockers.
    card_digest: str | None = None
    state: ConfirmationState | None = None
    ttl_hours: int


class ConfirmRequest(BaseModel):
    """``POST /api/v1/tracker/confirmations/{vacancy_id}``."""

    card_digest: str = Field(min_length=64, max_length=64)


class ConfirmedVacancy(BaseModel):
    """One application the agent would send on the next dashboard run."""

    external_id: str
    title: str
    company: str | None = None
    confirmed_at: datetime


class ConfirmedList(BaseModel):
    """``GET /api/v1/tracker/confirmations``."""

    items: list[ConfirmedVacancy] = Field(default_factory=list)
    #: Confirmations on record that the agent would not use any more — expired,
    #: or given to a card that has since changed.
    no_longer_valid: int = 0
