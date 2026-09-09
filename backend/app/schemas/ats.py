"""What an ATS readability audit reports.

These models are stored as JSONB on the profile and returned by the API, so a
change here is a schema change: the stored reports of older profiles must keep
validating.

The report answers in two halves, produced at different moments.

The **structural** half reads the uploaded file — geometry, glyphs, contacts,
headings — and is ready the instant the file lands, before any model has looked
at it. The **coverage** half is the comparison the whole thing is really for:
the structure the model read out of the resume (which saw the page correctly)
against what is recoverable from the text layer alone (which is all an employer
gets). It can only be computed once extraction has finished, so a report may
carry it or not, and ``None`` means "not measured", never "nothing was lost".

The **keyword** half answers a third question, and only when a vacancy is named:
of the requirements this posting actually lists, which ones does this document
spell the way an employer's parser searches for them. It is the same report
object — one model, whatever screen renders it — because the alternative is
three shapes of "ATS report" that disagree about the same document.
"""

from enum import StrEnum

from pydantic import BaseModel, Field, computed_field

# The one thing this module takes from elsewhere, and deliberately: where a
# requirement came from is a fact about the vacancy row, written by
# ``app.normalize.sync`` and read by matching and by this report alike. A second
# spelling of it here would be a second vocabulary for one column, which is how
# two screens end up disagreeing about the same requirement. ``app.db.enums``
# holds no ORM — that is why it exists — so the import costs nothing.
from app.db.enums import RequirementSource


class Severity(StrEnum):
    """How much a finding matters.

    ``critical`` means a machine reading the file will get the wrong answer or
    no answer. ``warning`` means it will probably cope but might not.
    """

    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class Overall(StrEnum):
    """The one-word verdict a banner is drawn from.

    Separate from the score because a number invites comparison shopping — 78
    against 84 means nothing — while this says what to do: nothing, something,
    or start over.
    """

    OK = "ok"
    DEGRADED = "degraded"
    UNREADABLE = "unreadable"


class FindingCode(StrEnum):
    """Stable identifiers, so the UI can key off them and tests can name them."""

    NO_TEXT_LAYER = "NO_TEXT_LAYER"
    COLUMN_INTERLEAVING = "COLUMN_INTERLEAVING"
    CONTACTS_NOT_TEXT = "CONTACTS_NOT_TEXT"
    DATES_NOT_EXTRACTABLE = "DATES_NOT_EXTRACTABLE"
    BROKEN_GLYPHS = "BROKEN_GLYPHS"
    UNMAPPED_FONT = "UNMAPPED_FONT"
    TEXT_IN_TABLES = "TEXT_IN_TABLES"
    MISSING_SECTIONS = "MISSING_SECTIONS"
    CONTENT_LOST = "CONTENT_LOST"
    FORMAT_NOT_PDF = "FORMAT_NOT_PDF"
    HIDDEN_TEXT = "HIDDEN_TEXT"
    KEYWORD_STUFFING = "KEYWORD_STUFFING"
    DATE_FORMAT_MIXED = "DATE_FORMAT_MIXED"
    EMPLOYMENT_GAP = "EMPLOYMENT_GAP"
    LENGTH_OUT_OF_RANGE = "LENGTH_OUT_OF_RANGE"
    REQUIREMENTS_NOT_NAMED = "REQUIREMENTS_NOT_NAMED"


class DocumentKind(StrEnum):
    """What is being audited, which decides which checks even apply.

    A cover letter has no employment dates and no «Опыт работы» heading, and
    reporting their absence as defects would be noise wearing the costume of an
    audit. So the kind selects the check set rather than the caller doing it.
    """

    RESUME = "resume"
    COVER_LETTER = "cover_letter"


class DocumentOrigin(StrEnum):
    """Who wrote the document under audit.

    ``uploaded`` is the file the candidate sent us. ``generated`` is a document
    this system produced, audited before a person ever sees it — a check on our
    own output, where a defect is ours to fix rather than theirs.
    """

    UPLOADED = "uploaded"
    GENERATED = "generated"


class KeywordStatus(StrEnum):
    """Where one of the vacancy's requirements stands in this document.

    The distinction between the middle value and the last one is the whole
    point of the keyword half, and it must survive into the interface: one is
    fixable by writing a better variant of a true document, the other is not
    fixable at all except by learning the skill. Collapsing them into "missing"
    turns an audit into a suggestion to lie.
    """

    #: The candidate holds this skill and the document spells it the way the
    #: posting spells it. This is what a keyword-matching parser can find and
    #: what an interview can confirm — both halves, because either alone is a
    #: number that misleads somebody.
    PRESENT = "present"
    #: The candidate holds this skill — the profile says so — and this variant
    #: of the document does not name it, or names it in other words. Fixable by
    #: generating a variant that says it, which invents nothing.
    UNSTATED = "unstated"
    #: The profile does not have it. Reported and left alone: the honest answer
    #: is that this requirement is not covered. This wins even when the document
    #: does contain the string — a letter that names the gap in a sentence
    #: saying the candidate has not used it is matched by an employer's filter
    #: and is still not coverage.
    ABSENT = "absent"


class Finding(BaseModel):
    """One thing an applicant tracking system will struggle with."""

    code: FindingCode
    severity: Severity
    title: str
    #: What goes wrong, in terms of what the machine sees.
    explanation: str
    #: What the file actually produced. Seeing the mangled text is what makes
    #: the finding believable — a score on its own is an opinion.
    example_fragment: str | None = None
    #: A concrete action, not advice. "Lay it out in one column", not
    #: "improve readability".
    fix: str
    #: Points this finding took off the score, so the number is explained
    #: rather than asserted.
    penalty: int = Field(ge=0, le=100)


class Recoverable(BaseModel):
    """One kind of content, counted under both readings.

    ``total`` is what the model found in the resume; ``recovered`` is how much
    of that a parser reading only the text layer would still get right. The gap
    is the finding, and ``lost`` names the items on the wrong side of it so the
    candidate is told *which* jobs disappear, not merely how many.
    """

    total: int = Field(ge=0)
    recovered: int = Field(ge=0)
    lost: list[str] = Field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        """True when nothing was lost between the two readings."""
        return self.recovered >= self.total


class ATSCoverage(BaseModel):
    """How much of the resume survives being read by machine.

    This is the number the candidate came for: "an ATS will see 2 of your 4
    jobs". Everything else in the report explains why.
    """

    work_periods: Recoverable
    dates: Recoverable
    skills: Recoverable

    @property
    def is_complete(self) -> bool:
        """True when both readings agree about everything."""
        return all(part.is_complete for part in (self.work_periods, self.dates, self.skills))


class RequirementMatch(BaseModel):
    """One requirement of one vacancy, and what this document does with it."""

    #: Spelled exactly as the posting spelled it, because that is the string an
    #: employer's parser searches for. Never canonicalised for display.
    requirement: str
    status: KeywordStatus
    #: The spelling actually found in the document. Equal to
    #: :attr:`requirement` (bar case) when the status is ``present``; on an
    #: ``unstated`` requirement it is the *other* spelling the document used —
    #: «постгрес» where the posting asks for «PostgreSQL» — which is the
    #: difference between rewriting a line and adding a skill. On an ``absent``
    #: one it is set only when the document says the word anyway, which is what
    #: a letter naming a gap does: the filter matches, and there is nothing
    #: behind the match.
    found_as: str | None = None
    #: How the profile records this skill, when the candidate holds it. Present
    #: only on ``unstated``: it is the evidence that naming it invents nothing.
    held_as: str | None = None
    #: Whether the posting marks this a hard requirement. Nice-to-haves are
    #: reported too — they are still things the employer asked for — but a
    #: screen that shows everything at one weight is a screen nobody reads.
    is_required: bool = True
    #: Whether the employer named this requirement in a field of their own, or
    #: it was read out of their description. The report already separates "held
    #: but not written down" from "not held"; this is a third thing, about the
    #: requirement rather than about the document, and a candidate rewriting a
    #: CV around an inferred requirement is entitled to know that is what it is.
    source: RequirementSource = RequirementSource.EMPLOYER_FIELD


class ATSKeywords(BaseModel):
    """This document read against one vacancy's requirement list.

    Literal, deliberately. A capable reader knows «постгрес» is PostgreSQL; the
    keyword filter between the candidate and that reader does not, and modelling
    it as anything cleverer than string search would report a resume as covered
    when the filter will drop it.
    """

    #: In the order the posting listed them: a candidate reading this is looking
    #: at the employer's own priorities, not at ours.
    requirements: list[RequirementMatch] = Field(default_factory=list)

    @property
    def present(self) -> list[RequirementMatch]:
        """Requirements this document names in the posting's own words."""
        return [r for r in self.requirements if r.status is KeywordStatus.PRESENT]

    @property
    def unstated(self) -> list[RequirementMatch]:
        """Held by the candidate, not named here. The fixable half."""
        return [r for r in self.requirements if r.status is KeywordStatus.UNSTATED]

    @property
    def absent(self) -> list[RequirementMatch]:
        """Not held and not claimed. Reported, never suggested as an addition."""
        return [r for r in self.requirements if r.status is KeywordStatus.ABSENT]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def literal_coverage(self) -> float:
        """Share of the requirement list this document really covers, 0.0-1.0.

        Really: named in the posting's own words *and* backed by the profile.
        Counting a literal hit on its own would let a document raise this number
        by listing requirements it goes on to say the candidate does not meet.

        Zero requirements gives 1.0 rather than 0.0: a posting that lists none
        cannot be failed on the ones it did not list, and a screen showing «0%»
        for it would be telling the candidate to fix nothing in particular.
        """
        if not self.requirements:
            return 1.0
        return round(len(self.present) / len(self.requirements), 4)


class ATSReport(BaseModel):
    """Whether a machine can read this resume, and what to do about it.

    Deliberately produced without an LLM of its own. The point is to model a
    dumb parser — the kind an employer's tracking system actually runs — so the
    answer has to come from the same text layer that parser would read. The
    coverage half reuses the extraction the pipeline already paid for; it never
    makes a call to get one.
    """

    score: int = Field(ge=0, le=100)
    findings: list[Finding] = Field(default_factory=list)
    #: Which checks ran. A check that could not run (a plain text file has no
    #: columns to interleave) is absent here rather than silently passing.
    checks_run: list[FindingCode] = Field(default_factory=list)
    #: Standard headings a parser would find, by the names it looks for.
    sections_detected: list[str] = Field(default_factory=list)
    #: None until extraction has finished. Absence means "not measured".
    coverage: ATSCoverage | None = None
    #: None when no vacancy was named. Absence means "not compared against
    #: anything", never "nothing was missing".
    keywords: ATSKeywords | None = None
    #: Defaulted so that every report stored before these fields existed keeps
    #: validating, and defaulted to what those reports actually were: an
    #: uploaded resume.
    document_kind: DocumentKind = DocumentKind.RESUME
    origin: DocumentOrigin = DocumentOrigin.UPLOADED
    source_format: str
    page_count: int = 0
    word_count: int = 0

    @property
    def critical(self) -> list[Finding]:
        """Findings that mean the file will not be read correctly."""
        return [f for f in self.findings if f.severity is Severity.CRITICAL]

    @property
    def is_machine_readable(self) -> bool:
        """True when nothing critical stands in the way."""
        return not self.critical

    @computed_field  # type: ignore[prop-decorator]
    @property
    def overall(self) -> Overall:
        """The verdict, derived rather than stored.

        Derived so it cannot drift from the findings it summarises: a stored
        verdict edited by hand, or written by an older version of this code,
        would be the one part of the report nothing checks.
        """
        if self.critical:
            return Overall.UNREADABLE
        # Driven by severity rather than by "any finding at all": the note that
        # a DOCX could not be checked for layout is information, not a defect,
        # and grading every non-PDF as degraded would make the word meaningless.
        degraded = any(f.severity is Severity.WARNING for f in self.findings)
        if degraded or (self.coverage is not None and not self.coverage.is_complete):
            return Overall.DEGRADED
        return Overall.OK


#: How many unstated requirements a summary names before it stops. The list is
#: for a console card and a badge, not for the report screen: past a handful it
#: stops being something a person reads while deciding whether to send.
SUMMARY_NAMES = 5


class ATSSummary(BaseModel):
    """The report reduced to what fits on a confirmation card.

    A projection of :class:`ATSReport`, never a second source of truth: it is
    built by :meth:`of` and by nothing else, so a card and a report screen
    cannot end up disagreeing about the same document. It exists because the
    card is a terminal printing plain text seconds before an application is
    sent, and pasting a full report into it would bury the one line that
    matters.
    """

    overall: Overall
    score: int = Field(ge=0, le=100)
    #: Titles of the findings that mean a parser gets this wrong. Titles rather
    #: than codes: the card is read by a person, not by a client.
    critical: list[str] = Field(default_factory=list)
    requirements_total: int = 0
    requirements_present: int = 0
    #: Named, because "3 requirements unstated" is not actionable and
    #: "PostgreSQL, Kafka, Docker are not in this variant" is.
    unstated: list[str] = Field(default_factory=list)
    absent: int = 0

    @classmethod
    def of(cls, report: ATSReport) -> "ATSSummary":
        """Reduce a full report to the card's shape."""
        keywords = report.keywords
        return cls(
            overall=report.overall,
            score=report.score,
            critical=[finding.title for finding in report.critical],
            requirements_total=len(keywords.requirements) if keywords else 0,
            requirements_present=len(keywords.present) if keywords else 0,
            unstated=[r.requirement for r in keywords.unstated[:SUMMARY_NAMES]] if keywords else [],
            absent=len(keywords.absent) if keywords else 0,
        )
