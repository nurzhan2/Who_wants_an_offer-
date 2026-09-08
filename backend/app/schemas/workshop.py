"""API contracts for the workshop: references, rules and the trial letter.

The rule payload is the same discriminated union the checker and the prompt use
(:data:`app.workshop.rules.RuleParams`), not a flattened copy of it. One
definition means the shape a client sends, the shape stored in ``params``, the
shape the checker reads and the shape rendered into the prompt are the same
object — and that a kind added to the union appears in the OpenAPI schema
without a second edit that might not happen.

``id`` is a string rather than a UUID throughout, because a built-in rule has no
UUID. It has ``builtin:no_links``, and that is deliberate rather than a
compromise: the mutation endpoints take a UUID path parameter, so a built-in is
not addressable there at all. "Undeletable" is a property of the URL space
before it is a property of any check.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.enums import ReferenceKind, RuleKind, RuleScope, RuleSeverity
from app.schemas.common import ReadModel
from app.workshop.rules import MAX_SUBJECT_CHARS, RuleParams

#: Longest a title or a human-facing message may be. Both are labels in a list.
MAX_TITLE_CHARS = 200
MAX_MESSAGE_CHARS = 500
MAX_NOTE_CHARS = 1_000


class ReferenceRead(ReadModel):
    """One stored reference, as the dashboard lists it.

    The full text is not here. A reference is a whole CV, the list shows five of
    them, and a list endpoint that ships five CVs to render five rows is a
    design nobody notices until the page is slow. :attr:`preview` is what a row
    needs; the single-reference endpoint returns the text.
    """

    id: UUID
    kind: ReferenceKind
    title: str
    note: str | None = None
    is_active: bool
    characters: int
    preview: str
    source_filename: str | None = None
    source_format: str | None = None
    size_bytes: int | None = None
    created_at: datetime
    updated_at: datetime


class ReferenceDetail(ReferenceRead):
    """One reference with the text itself, for the editor."""

    text: str


class ReferenceCreated(BaseModel):
    """What the upload endpoint answers with.

    Carries the extractor's warnings, which is the whole reason it is not just
    a :class:`ReferenceDetail`. "This PDF is two columns and came out
    interleaved" has to reach the person at the moment they upload it, while
    they still have the file open and can paste the text instead.
    """

    reference: ReferenceDetail
    warnings: tuple[str, ...] = ()


class ReferenceUpdate(BaseModel):
    """Fields a stored reference may be corrected in.

    Not the text: a reference is a document that was uploaded, and editing it in
    place would make the stored text something nobody has read. Delete it and
    add the corrected one.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE_CHARS)
    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS)
    is_active: bool | None = None


class RuleRead(BaseModel):
    """One rule, the owner's or a built-in, as the dashboard lists it."""

    #: UUID string, or ``builtin:<kind>``. See the module docstring.
    id: str
    kind: RuleKind
    scope: RuleScope
    severity: RuleSeverity
    params: RuleParams
    message: str
    is_active: bool
    #: True for a rule that cannot be edited, switched off or deleted. Shown in
    #: the list rather than hidden, because a person is entitled to know which
    #: constraints they cannot lift and why.
    is_builtin: bool
    #: The same rule as the model is asked for it, in English. Displayed beside
    #: the owner's own sentence so the two can be compared: the prompt says what
    #: will be measured, and a rule that does not measure what its author meant
    #: is visible here rather than in a letter three weeks later.
    asked_as: str


class RuleCreate(BaseModel):
    """A new rule.

    ``params`` carries its own ``kind``, so there is no separate kind field to
    contradict it. The message is required: a rule with no sentence is one whose
    author will not recognise it in a list of nine, and it is the only thing a
    person is shown when a generation is refused.
    """

    model_config = ConfigDict(extra="forbid")

    scope: RuleScope = RuleScope.COVER_LETTER
    severity: RuleSeverity = RuleSeverity.HARD
    params: RuleParams
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    is_active: bool = True


class RuleUpdate(BaseModel):
    """Fields a stored rule may be changed in.

    ``params`` may be replaced whole, and the replacement goes through the same
    truthfulness check the original did: a rule that could not be created must
    not be reachable by editing one that could.
    """

    model_config = ConfigDict(extra="forbid")

    scope: RuleScope | None = None
    severity: RuleSeverity | None = None
    params: RuleParams | None = None
    message: str | None = Field(default=None, min_length=1, max_length=MAX_MESSAGE_CHARS)
    is_active: bool | None = None


class ViolationRead(BaseModel):
    """One rule a document broke, in both languages it has to be said in."""

    rule_id: str
    kind: RuleKind
    severity: RuleSeverity
    #: The owner's own sentence. What the dashboard shows.
    message: str
    #: What was actually measured, in English. What the model was told.
    detail: str


class VacancyChoice(ReadModel):
    """One vacancy the preview can be run against.

    Three fields, because that is what a picker needs. It is not a vacancy list
    endpoint and must not grow into one: the dashboard's own list is phase 7's,
    and two of them would disagree about filtering the week after they shipped.
    """

    id: UUID
    title: str
    company: str | None = None
    city: str | None = None


class PreviewRequest(BaseModel):
    """Ask for a trial letter for one vacancy under the current rules."""

    model_config = ConfigDict(extra="forbid")

    vacancy_id: UUID
    #: Which resume to write from. The active profile when omitted, which is
    #: what the dashboard sends and what a single-profile installation means.
    profile_id: UUID | None = None


class PreviewResponse(BaseModel):
    """The trial letter, or an account of why there is not one.

    Nothing is saved. This is the workshop's "try it" button, and a preview that
    wrote into ``application.cover_letter`` would let a person discover their
    experiment by finding it in the apply queue.

    ``written`` is false in exactly one case — every hard constraint could not
    be met, so :attr:`broken_rules` says which — and the refusal is returned as
    data rather than as an error status because it is the answer to the
    question, not a failure of the request. It is the most useful thing the
    workshop can tell somebody about a rule they have just written.
    """

    written: bool
    text: str | None = None
    #: ``model`` or ``fallback``. A person deciding whether their rules work
    #: needs to know that the text in front of them was assembled from the
    #: database rather than written.
    source: str | None = None
    language: str | None = None
    characters: int = 0
    attempts: int = 0
    #: Soft rules the returned text still breaks.
    warnings: tuple[ViolationRead, ...] = ()
    #: Hard rules that stopped it. Non-empty only when ``written`` is false.
    broken_rules: tuple[ViolationRead, ...] = ()
    #: Why it could not be written, in one sentence, when ``written`` is false.
    detail: str | None = None
    #: How many rules and reference documents this generation was given. A
    #: preview that used none of them looks exactly like one that used four.
    rules_applied: int = 0
    references_used: int = 0
    #: How many past letters were shown as few-shot examples. Almost always
    #: zero — see :mod:`app.letters.examples`.
    examples_used: int = 0


__all__ = [
    "MAX_MESSAGE_CHARS",
    "MAX_NOTE_CHARS",
    "MAX_SUBJECT_CHARS",
    "MAX_TITLE_CHARS",
    "PreviewRequest",
    "PreviewResponse",
    "ReferenceCreated",
    "ReferenceDetail",
    "ReferenceRead",
    "ReferenceUpdate",
    "RuleCreate",
    "RuleRead",
    "RuleUpdate",
    "VacancyChoice",
    "ViolationRead",
]
