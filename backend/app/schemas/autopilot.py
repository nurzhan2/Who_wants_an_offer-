"""The autopilot's wire types: what passed the selection, what did not, and the batch.

Three payloads, and the reason they live together is that they are three views
of one decision. :class:`SetAsideItem` says why a vacancy will not be applied
to; :class:`BatchItem` carries exactly what the owner reads before saying yes to
one that will; :class:`BatchPlan` is both lists plus the ceiling on how many of
them one "yes" may cover.

**Nothing here sends anything, and the shape says so.** A confirmation is a
digest of the card the owner read (``app.services.agent_queue.card_digest``), so
confirming a batch is N separate confirmations of N separate texts — which is
what keeps «подтвердить пачку» from meaning «подтвердить что угодно, что
окажется в очереди к моменту отправки». The agent still re-reads every vacancy
page, still mints one mandate per vacancy, and still refuses a letter whose
digest moved.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.agent import QueueItem


class SetAsideKind(StrEnum):
    """Why one vacancy is not in the batch, as something a client can branch on.

    The sentence beside it is what a person reads; this is what a screen groups
    by, and what a test names without quoting Russian prose.
    """

    #: No letter has been written for it yet.
    NO_LETTER = "no_letter"
    #: The crawler last saw it gone, or hh had closed it for applications.
    ARCHIVED = "archived"
    CLOSED = "closed"
    #: The posting asks for more experience than the owner has, by more than
    #: ``AGENT_MAX_EXPERIENCE_GAP_YEARS``.
    EXPERIENCE_GAP = "experience_gap"
    #: It asks for a language above the level the resume claims.
    LANGUAGE = "language"
    #: Nobody has read the posting's own page recently enough to send against it.
    STALE_PAGE = "stale_page"
    #: The stored letter breaks a workshop rule that stops a document.
    LETTER_RULES = "letter_rules"
    #: The stored letter fails the ATS audit.
    LETTER_AUDIT = "letter_audit"
    #: The stored URL does not name the posting's own id, so the agent would
    #: reject the batch it arrived in.
    UNSERVABLE = "unservable"


class SetAsideItem(BaseModel):
    """One vacancy the autopilot will not apply to, and why.

    Not thrown away: it carries its own id and link so the owner can open it,
    decide for themselves, and apply by hand.
    """

    vacancy_id: UUID
    external_id: str
    title: str
    company: str | None = None
    url: str
    score: Decimal | None = None
    kind: SetAsideKind
    #: One Russian sentence naming what the check found.
    reason: str


class BatchItem(BaseModel):
    """One application the owner is being asked to confirm, with its digest."""

    vacancy_id: UUID
    #: Exactly what the agent would be handed: letter, score, explanation, ATS
    #: summary, hh's earlier lines.
    item: QueueItem
    #: What to send back to confirm *this text*. Recomputed on confirmation and
    #: refused when it moved.
    card_digest: str = Field(min_length=64, max_length=64)
    #: Already confirmed, and the confirmation still describes this card.
    confirmed: bool = False
    confirmed_at: datetime | None = None


class BatchPlan(BaseModel):
    """``GET /api/v1/tracker/batch``: everything the "отправить все" screen draws."""

    items: list[BatchItem] = Field(default_factory=list)
    set_aside: list[SetAsideItem] = Field(default_factory=list)
    #: How many of ``items`` one confirmation may cover right now.
    limit: int
    #: Why the limit is that number, in Russian. Two different rules can set it
    #: — the batch ceiling and the smaller first batch after a quiet period —
    #: and a screen that shows a number without the rule invites raising it.
    limit_reason: str
    #: When the last application actually went out, as the tracker recorded it.
    last_sent_at: datetime | None = None
    #: True while the smaller first-batch ceiling applies.
    first_batch: bool = False


class BatchConfirmItem(BaseModel):
    """One "yes", bound to the card it was given to."""

    vacancy_id: UUID
    card_digest: str = Field(min_length=64, max_length=64)


class BatchConfirmRequest(BaseModel):
    """``POST /api/v1/tracker/batch``: the ticked rows, each with its digest."""

    items: list[BatchConfirmItem] = Field(min_length=1, max_length=100)


class BatchConfirmOutcome(BaseModel):
    """What happened to one row of the batch."""

    vacancy_id: UUID
    confirmed: bool
    #: Why not, when it was refused. Russian, for the screen.
    detail: str | None = None


class BatchConfirmResult(BaseModel):
    """The answer to a batch confirmation: per row, and nothing sent."""

    confirmed: int = 0
    outcomes: list[BatchConfirmOutcome] = Field(default_factory=list)
    limit: int
    limit_reason: str
