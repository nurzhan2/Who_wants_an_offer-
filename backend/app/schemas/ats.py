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
"""

from enum import StrEnum

from pydantic import BaseModel, Field, computed_field


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
