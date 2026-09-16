"""Contracts for generated documents.

Two things in here are shaped by requirements rather than by convenience.

**A withheld document and a delivered one are the same response type.** The
brief says a document that fails a hard rule is not handed over and the person
is shown what is wrong, so :class:`GeneratedDocumentRead` carries ``delivered``
and either a document or a reason. A client that forgets to check the flag gets
no file and no download id rather than a file it should not have had.

**Requirement coverage keeps its three lists apart.** "Required, held, and named
in this CV", "required, held, and not named in this version" and "required and
not held" are three different situations for the person reading them: the second
is fixed by regenerating, the third is not fixable at all, and a UI that showed
them alike would be inviting the owner to write down a skill they do not have.
The type refuses to let them be merged.
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field

from app.db.enums import DocumentKind, DocumentSource
from app.documents.employer import EmployerSignals
from app.schemas.ats import ATSReport
from app.schemas.common import ReadModel


class RequirementCoverageRead(BaseModel):
    """What a document says about what the vacancy asked for."""

    #: Required, held, and named in this document the way the vacancy spells it.
    named: list[str] = Field(default_factory=list)
    #: Required and held, but not named in this version. Fixable by regenerating.
    held_but_unnamed: list[str] = Field(default_factory=list)
    #: Required and not held. Reported with nothing suggested: the fix for a
    #: missing skill is to learn it, and a hint otherwise would be inviting the
    #: one thing the generator's guard exists to forbid.
    not_held: list[str] = Field(default_factory=list)
    #: Requirements no employer stated: read out of the description. Crosses the
    #: three lists above rather than being a fourth one, and is shown as a mark
    #: on the entries it names.
    inferred: list[str] = Field(default_factory=list)
    #: Share of the requirement list the document names literally, 0.0-1.0. What
    #: a keyword-matching parser would arrive at, which is a different question
    #: from the match score and must not be displayed as one.
    literal_coverage: float = 0.0


class DocumentReviewRead(BaseModel):
    """The report a document is handed over with."""

    ats: ATSReport
    coverage: RequirementCoverageRead


class GeneratedDocumentRead(BaseModel):
    """The answer to "generate a document for this vacancy".

    ``delivered`` is the field to branch on. False means nothing was stored and
    nothing can be downloaded; ``reason`` and ``reason_ru`` say why, and
    ``review`` still carries the audit when it was the audit that withheld it.
    """

    vacancy_id: UUID
    kind: DocumentKind
    delivered: bool
    #: The stored version, when one was stored. This is the id a download uses.
    document_id: UUID | None = None
    version: int | None = None
    source: DocumentSource | None = None
    filename: str | None = None
    file_format: str | None = None
    #: The document as plain text — what the audit read, and what a dashboard
    #: shows next to the report without downloading anything.
    text: str | None = None
    review: DocumentReviewRead | None = None
    #: Machine-readable reason nothing was delivered.
    reason: str | None = None
    #: The same reason in the language the owner reads.
    reason_ru: str | None = None
    #: What the checks caught on the way, whether or not a document came out.
    problems: list[str] = Field(default_factory=list)
    #: The hard rules in force, as a person reads them. Sent with every answer so
    #: a screen explaining a withheld document needs no second request.
    hard_rules: list[str] = Field(default_factory=list)


class DocumentVersionRead(ReadModel):
    """One stored version in the history of a (profile, vacancy, kind).

    The history is the point of the feature: the owner edits the rules,
    regenerates, and compares. So every row carries the rule set it was written
    under and the score it got, which are the two things that explain a
    difference between two versions.
    """

    id: UUID
    vacancy_id: UUID
    kind: DocumentKind
    version: int
    rules_version: str
    source: DocumentSource
    ats_score: int
    ats_overall: str
    created_at: datetime


class DocumentSummaryRead(DocumentVersionRead):
    """A stored version with enough about its vacancy to list it on a screen."""

    vacancy_title: str
    company: str | None = None


class DocumentCandidateRead(BaseModel):
    """One vacancy the two buttons appear on, and what it already has."""

    vacancy_id: UUID
    title: str
    company: str | None = None
    score: Decimal
    #: How many versions of each kind exist for this vacancy already. Zero means
    #: the button has never been pressed; two means pressing it again produces a
    #: third version rather than replacing anything.
    cv_versions: int = 0
    letter_versions: int = 0
    #: What the employer published about themselves on this posting — when they
    #: were last active, their accreditation, whether hh is checking them, how
    #: many people have applied. Read from the stored payload and never looked
    #: up: see :mod:`app.documents.employer`.
    employer: EmployerSignals = EmployerSignals()
    #: Whether the agent applies to it (its source lists the vacancy) or the
    #: owner does, by hand, on :attr:`url` — «откликнуться самому».
    via_agent: bool = False
    source_slug: str = ""
    url: str = ""
