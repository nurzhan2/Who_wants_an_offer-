"""Contract between the backend and the local apply agent.

The wire shape of record is not this file. It is ``agent/queue.py``'s
``QueueItem`` and ``Result``, which were written first and which the agent
already parses; these models exist so the backend can *serve* that shape
without a raw dict crossing a layer boundary (CLAUDE.md rule 3).

Two things about it are load-bearing and easy to break by accident.

**``vacancy_id`` on this wire is the source's own id, not ours.** The agent
keys everything on hh's numeric id: its journal rows, its idempotency check,
the page it opens. ``agent/queue.py`` enforces that with ``vacancy_id.isdigit()``
*and* with a comparison against the id inside ``url``. Our ``Vacancy.id`` is a
UUIDv7 and would fail both. So the queue serves ``vacancy_source.external_id``
and the result endpoint resolves it back to a UUID.

**``url`` is served, never rebuilt.** The crawler stores the address it actually
read, which for hh is a regional subdomain — ``https://almaty.hh.kz/vacancy/…``
— because ``app/sources/hh.py`` walks a per-host sitemap. That is the URL the
agent opens and the URL the human reads on the confirmation card, and
reconstructing it from an id against a bare host would silently move both to a
page hh redirects. See ``app/services/agent_queue.py`` for the check that keeps
this true.

The additions to ``agent/queue.py``'s shape are :attr:`QueueItem.match`,
:attr:`QueueItem.source` and the two extra advisory flags, plus the result
fields ``hh_blocking_warning``, ``negotiations_total``, ``last_state`` and
``sent_letter``. They are additive: ``QueueItem.from_json`` reads by key and
ignores what it does not know, so an agent built against the older shape still
parses these payloads. Everything the agent *does* read is pinned against its
own source by ``backend/tests/test_agent_queue.py``, which parses
``agent/queue.py`` rather than importing it.

Additive in that direction only. A field the backend accepts and the agent
never sends is a column that stays NULL — honest, and visibly empty. A field
the agent sends and the backend has not declared is a 422 on a result
describing an application that has already gone out, which is why the drift
test asserts ``agent`` ⊆ ``backend`` and why anything removed from this file
has to be removed from ``agent/queue.py`` first.

``sent_letter`` is the one addition the agent side has to grow before it can
carry anything: ``agent.queue.Result`` has no such field, so today every
result leaves ``application.sent_letter`` NULL. Nothing here fills that gap
from ``cover_letter``, and the reason is under the field itself.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Final
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints

from app.db.enums import MatchBucket
from app.schemas.ats import ATSSummary
from app.schemas.match import MatchComponentScores, MatchedSkill, MissingSkill

#: Carried in both payloads. Mirrors ``agent.queue.CONTRACT_VERSION``: a queue
#: produced by an older backend than the agent expects is a thing to notice
#: rather than to guess at, and the agent already refuses a mismatch.
CONTRACT_VERSION: Final[int] = 1

#: A source's own identifier for a posting. Bounded like the column it comes
#: from (``vacancy_source.external_id``) and otherwise unconstrained: hh's ids
#: are numeric, and writing that rule in here would put one source's habits
#: into the contract every source has to fit.
ExternalId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]

#: hh's own sentences, quoted. Long enough for the whole modal line and capped
#: so a broken page cannot post a megabyte into the tracker.
WarningText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]

#: The letter as it was typed. Not stripped and not otherwise touched: this is
#: the evidence of what an employer read under the owner's name, and a schema
#: that quietly trims it is a schema that stores something nobody sent.
#:
#: The ceiling is twice hh's measured ``letterMaxLength`` of 10 000
#: (``agent/letter.py``) rather than equal to it. A cap that exactly matches
#: another site's current limit turns hh raising that limit into a 422 on a
#: result describing an application that has already gone out — the same
#: mistake :attr:`ApplicationResult.last_state` is written to avoid.
SentLetterText = Annotated[str, StringConstraints(max_length=20000)]


class AgentStatus(StrEnum):
    """Where the agent left one application.

    A deliberate duplicate of ``agent/state.py``'s ``Status``. The two packages
    do not import each other — that boundary is the whole point of
    ``agent/tests/test_isolation.py`` — so the enum is written twice and pinned
    together by ``backend/tests/test_agent_queue.py``, which parses the agent's
    source rather than importing it.

    Closed, unlike :attr:`ApplicationResult.last_state`, because this set is
    ours: it is the state machine in ``agent/state.py``, and a status outside it
    is a bug in the agent rather than news from hh.
    """

    QUEUED = "queued"
    CONFIRMED = "confirmed"
    SENT = "sent"
    FAILED = "failed"
    NEEDS_MANUAL = "needs_manual"
    SKIPPED = "skipped"


class MatchExplanation(BaseModel):
    """Why the score is that score.

    Served whole rather than as a number because a person reads it: the
    confirmation card the agent prints is the last thing between a generated
    letter and a real application, and «78» tells them nothing they can act on
    while «нет: Kubernetes» tells them plenty.
    """

    score: Decimal = Field(ge=0, le=100)
    bucket: MatchBucket
    #: The LLM's or the rule engine's sentence about this pairing.
    verdict: str | None = None
    #: What to lead with. Written for the letter; useful on the card too.
    application_angle: str | None = None
    components: MatchComponentScores = Field(default_factory=MatchComponentScores)
    matched_skills: list[MatchedSkill] = Field(default_factory=list)
    missing_required: list[MissingSkill] = Field(default_factory=list)
    missing_nice: list[MissingSkill] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    experience_gap_years: Decimal | None = None


class DashboardConfirmation(BaseModel):
    """The owner's "yes" to one card, given in the dashboard and still valid.

    Served on a queue item only while it still describes what would be sent:
    the card the queue would show now has ``card_digest``, the letter has
    ``letter_digest``, and the confirmation is younger than
    ``AGENT_CONFIRMATION_TTL_HOURS``. Anything else is served as no
    confirmation at all, and the agent's dashboard mode then leaves the item
    alone. The agent re-checks the letter itself before minting a mandate
    bound to ``card_digest``. See ``app.services.confirmations``.
    """

    confirmed_at: datetime
    letter_digest: str = Field(min_length=64, max_length=64)
    card_digest: str = Field(min_length=64, max_length=64)


class QueueItem(BaseModel):
    """One vacancy offered to the agent.

    The first eight fields are ``agent.queue.QueueItem``'s, spelled exactly as
    it parses them — the advisory flags are flat rather than nested for that
    reason alone, and moving them under a sub-object would break the agent
    without breaking any test here.
    """

    vacancy_id: ExternalId
    #: The address the crawler actually read. See the module docstring.
    url: str
    title: str
    company: str | None = None
    #: Written by ``app/letters/``. The agent never writes or edits one; it
    #: checks it and either pastes it or stops.
    letter: str | None = None

    # ── prefilter flags: what the crawler knew when it last saw the posting ──
    # Advisory only. The agent re-reads the page before anything is clicked,
    # because a vacancy can close between a crawl and a run.
    closed_for_applicants: bool = False
    archived: bool = False
    external_application: bool = False

    #: The score on the project's 0-100 scale, and one sentence saying why it
    #: is that. Flat, and named exactly this, because that is what
    #: ``agent.queue.QueueItem`` reads and what its confirmation card prints:
    #: «82» does not help the person deciding whether to send, and «не хватает
    #: обязательного: kubernetes» does. Both are optional — an unscored vacancy
    #: must look different on the card from a badly scored one.
    #:
    #: A ``Decimal`` serialises to a JSON string here (``"82.50"``); the agent
    #: accepts a string or a number for exactly that reason.
    score: Decimal | None = Field(default=None, ge=0, le=100)
    score_explanation: str | None = None

    # ── beyond agent/queue.py ────────────────────────────────────────────
    #: Which connector this posting came from. The agent can only act on sites
    #: it knows how to drive, and being told rather than inferring from the
    #: hostname is cheaper for everyone.
    source: str
    #: The same explanation with its structure intact, for anything that wants
    #: to render it rather than print it. The agent reads the flat pair above;
    #: this is here so the dashboard and any later card do not have to parse a
    #: sentence back into skills.
    match: MatchExplanation | None = None
    #: hh hides the employer's name on some postings. A letter addressed to
    #: nobody is a letter worth reading before it is sent.
    anonymous: bool = False
    #: hh is itself checking this employer.
    employer_on_additional_check: bool = False
    #: How this item's letter reads to a machine, and how much of the vacancy's
    #: requirement list it names. The last of the ATS report's three display
    #: places: after the card there is nothing between this and an employer's
    #: inbox. ``None`` means the item carries no letter and nothing was audited,
    #: which a card must not print as a pass.
    ats: ATSSummary | None = None
    #: What hh said about this vacancy on earlier runs, one line each, as the
    #: tracker stored it. Part of the card the owner confirms in the dashboard.
    hh_lines: list[str] = Field(default_factory=list)
    #: The owner's confirmation from the dashboard, when there is a valid one.
    #: ``None`` for every item the owner has not confirmed there — which the
    #: terminal flow does not care about and the dashboard flow refuses to send.
    confirmation: DashboardConfirmation | None = None


class QueueResponse(BaseModel):
    """``GET /api/v1/applications/queue``."""

    version: int = CONTRACT_VERSION
    items: list[QueueItem] = Field(default_factory=list)


class ApplicationResult(BaseModel):
    """What happened to one item, on its way back into the tracker."""

    vacancy_id: ExternalId
    status: AgentStatus
    #: Why the agent ended where it did, written for a person to read.
    reason: str | None = Field(default=None, max_length=2000)

    #: The letter the agent actually typed into the form, character for
    #: character.
    #:
    #: It is asked for here rather than copied from ``application.cover_letter``
    #: at the other end because the two can differ, and silently. That column
    #: holds the letter *as it stands now*: ``app/letters/store.save_letter``
    #: overwrites it in place, so a regeneration run between the queue being
    #: taken and this result arriving replaces the evidence with something no
    #: employer ever saw. And where one vacancy carries two tracker rows, the
    #: queue reads its letter from the oldest row holding one while the result
    #: lands on the oldest row of any kind — which need not be the same row.
    #: Both cases are rare and neither announces itself, and "usually right"
    #: is not a property a record of what was sent under somebody's name is
    #: allowed to have.
    #:
    #: ``None`` means the agent did not report it — an older agent, or a
    #: result that never reached the typing. ``""`` means it reported that
    #: nothing was typed, which hh permits on some vacancies. The two are not
    #: the same fact and are not stored as the same value.
    sent_letter: SentLetterText | None = None

    # ── hh's own words, kept apart from ours ─────────────────────────────
    #: The soft «Такой отклик может получить отказ» line together with the
    #: requirement it names. Persisted because it is hh's analysis of this
    #: application against this vacancy: it names one unmet requirement, which
    #: is more specific than any embedding similarity this project computes.
    #: It never blocks, so it can arrive on a ``sent`` result.
    hh_warning: WarningText | None = None
    #: The blocking demand — hh refusing to accept the application as things
    #: stand, e.g. the resume-visibility requirement. A field of its own
    #: because the two mean opposite things: this one stopped the send.
    hh_blocking_warning: WarningText | None = None

    #: ``negotiations.total`` read after the send. The measured idempotency
    #: answer: hh's own count of applications on this vacancy.
    negotiations_total: int | None = Field(default=None, ge=0)
    #: ``topicList[].lastState`` if one had appeared. ``DISCARD`` was observed
    #: live; the set is hh's and open, so this is a string and deliberately not
    #: an enum. Enumerating it here would turn hh adding a state into a 422 on
    #: a result that has already happened in the world.
    last_state: str | None = Field(default=None, max_length=100)


class ResultsRequest(BaseModel):
    """``POST /api/v1/applications/results``."""

    version: int = CONTRACT_VERSION
    results: list[ApplicationResult] = Field(default_factory=list)


class ResultAck(BaseModel):
    """What the backend did with one reported result."""

    vacancy_id: ExternalId
    #: False when no posting with this id is known; nothing was written.
    accepted: bool
    #: The tracker row this landed on, when it landed on one.
    application_id: UUID | None = None
    #: True when the row had to be created, False when an existing one was
    #: updated. Posting the same result twice yields ``created=False`` the
    #: second time and no second row.
    created: bool = False
    detail: str | None = None


class ResultsResponse(BaseModel):
    """Per-result acknowledgement, so the agent can see what was stored."""

    version: int = CONTRACT_VERSION
    accepted: int = 0
    #: Ids the backend could not resolve to a posting it knows.
    unknown: list[str] = Field(default_factory=list)
    results: list[ResultAck] = Field(default_factory=list)
