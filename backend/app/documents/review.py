"""Auditing what this system produced, before a person is allowed to have it.

The ATS audit was built to answer a question about a file somebody uploaded:
will an employer's parser read this? Turning it on our own output is the same
question asked of the one file we can still do something about — and it is the
only way the claim "this CV is machine-readable" is worth anything, because
otherwise it is the generator marking its own homework.

**The file is read back, not described.** The audit is not handed the text the
renderer intended; it is handed the .docx bytes, and the text it reads is what
:func:`app.resume.extractor.extract` gets *out* of those bytes — the same
function, on the same code path, that reads a resume a person uploads. So the
round trip is real: if writing the file lost something, the audit sees the loss
rather than the intention. Feeding it the renderer's own text would make every
generated document pass by construction and the report would mean nothing.

**Per-vacancy coverage is the other half.** ``ATSReport`` answers "can a machine
read this document". It does not answer "does this document say what this
vacancy is looking for", and for a tailored CV that is the question. Employers'
parsers match literal strings — "PostgreSQL" and "постгрес" are two different
technologies to a machine told to find the first — so :class:`RequirementCoverage`
counts the vacancy's requirement list three ways, and keeps the three apart
because they mean three completely different things to the person reading them:

``named``
    required, held, and spelled in this document the way the vacancy spells it.
    Nothing to do.
``held_but_unnamed``
    required, held, and not named in this version of the CV. **This is fixable
    by regenerating**, and it is the finding the whole feature exists to
    surface.
``not_held``
    required and not held. Not fixable, and deliberately reported with no
    suggestion attached: the fix for a missing skill is to learn it, and a
    report that hinted otherwise would be inviting the one thing the guard
    forbids.

The distinction between the second and the third is the one the brief calls
principled, and it is carried in the type rather than in a comment so that a UI
cannot accidentally render them the same way.
"""

from pydantic import BaseModel, ConfigDict, Field

from app.core.logging import get_logger
from app.documents.context import CVContext
from app.documents.guard import mentions
from app.letters.context import fold
from app.resume import ats_audit, extractor
from app.schemas.ats import ATSReport, FindingCode

logger = get_logger(__name__)


class RequirementCoverage(BaseModel):
    """What this document says about what the vacancy asked for.

    Three lists that must never be merged. See the module docstring: two of them
    are fixable by regenerating and one is not, and a UI that showed them alike
    would be telling the owner to write down a skill they do not have.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Required, held, and named in this document verbatim as the vacancy spells it.
    named: tuple[str, ...] = ()
    #: Required and held, but this version of the CV does not name it. Fixable.
    held_but_unnamed: tuple[str, ...] = ()
    #: Required and not held. Reported plainly, with nothing suggested.
    not_held: tuple[str, ...] = ()
    #: Cuts across the three above rather than joining them: the requirements
    #: the employer never stated, read out of their description instead. A
    #: candidate rewriting a CV around one is entitled to know that is what it
    #: is, and the three lists cannot say it — they are about the document.
    inferred: tuple[str, ...] = ()

    @property
    def required_total(self) -> int:
        """How many requirements the vacancy named at all."""
        return len(self.named) + len(self.held_but_unnamed) + len(self.not_held)

    @property
    def literal_coverage(self) -> float:
        """Share of the requirement list this document names literally, 0.0-1.0.

        The number a keyword-matching parser would arrive at. Distinct from the
        match score, which asks whether the candidate is suitable; this asks
        whether the document *says so* in the words the machine is looking for.
        """
        total = self.required_total
        return len(self.named) / total if total else 0.0


class DocumentReview(BaseModel):
    """Everything known about a finished document, handed over with it.

    The brief requires the report to travel with the document rather than be
    available near it, because the two are only meaningful together: a CV whose
    audit nobody read is a CV nobody checked.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ats: ATSReport
    coverage: RequirementCoverage

    @property
    def may_be_handed_over(self) -> bool:
        """Whether the audit found anything that stops this being given out.

        Only the audit's own critical findings. The honesty checks are enforced
        earlier and unconditionally — a document that fails one never reaches
        here, because nothing renders it — so this is the second gate, not the
        first.
        """
        return self.ats.is_machine_readable


def coverage_of(text: str, context: CVContext) -> RequirementCoverage:
    """Sort the vacancy's requirement list into the three answers.

    Literal on purpose: ``mentions`` is a word-boundary match on the vacancy's
    own spelling, which is what an employer's keyword filter does. A CV that
    covers a requirement under a different name lands in ``held_but_unnamed``,
    and that is correct rather than pedantic — the machine on the other side
    will not make the connection either, and the fix is a regeneration that uses
    the employer's word.
    """
    covered = {fold(skill.required_as) for skill in context.overlap.matched}
    covered.update(fold(name) for name in context.traceable)
    covered.discard("")

    named: list[str] = []
    unnamed: list[str] = []
    not_held: list[str] = []
    for requirement in context.vacancy.key_skills:
        key = fold(requirement)
        if not key:
            continue
        if key not in covered:
            not_held.append(requirement)
        elif mentions(text, requirement):
            named.append(requirement)
        else:
            unnamed.append(requirement)
    return RequirementCoverage(
        named=tuple(named),
        held_but_unnamed=tuple(unnamed),
        not_held=tuple(not_held),
        inferred=tuple(context.vacancy.inferred_skills),
    )


def audit_file(content: bytes, *, filename: str) -> ATSReport:
    """Run the readability audit over bytes this system just produced.

    The extraction step is what makes this a round trip rather than a claim:
    ``extract`` is the same function an uploaded resume goes through, so the
    text being judged is the text a parser gets, and a file that lost something
    on the way out shows the loss here.

    ``extraction=None`` deliberately. The coverage half of ``ATSReport`` compares
    what a model read off the page against what survives into the text layer,
    and that comparison answers nothing about a file with no page: there is no
    layout to lose content to, and re-running an LLM extraction over a document
    this code just wrote would be paying for an answer we already hold. The
    question that *is* worth asking of a generated file — does it say what the
    vacancy asked for — is :func:`coverage_of`, next door.
    """
    document = extractor.extract(content, filename)
    return ats_audit.audit(
        document.file_bytes,
        source_format=document.source_format,
        raw_text=document.raw_text,
    )


#: Findings that ask a question about a resume rather than about any document.
#:
#: Measured on a real generated letter, which the unmodified audit scored 28 and
#: called ``unreadable``. All three of these fired, and every one of them was
#: the letter doing the right thing:
#:
#: ``CONTACTS_NOT_TEXT``
#:     critical, thirty-five points, for having no email and no phone. A cover
#:     letter carrying either is filtered as spam by hh — that is why
#:     :mod:`app.letters.guard` rejects a letter containing one, by reading the
#:     text. So this finding grades a letter down for obeying the rule that
#:     keeps it deliverable at all.
#: ``MISSING_SECTIONS``
#:     for having no «Опыт работы» and «Образование» headings. A letter with
#:     section headings is not a letter.
#: ``DATES_NOT_EXTRACTABLE``
#:     for having no employment periods in it. Neither has any letter.
#:
#: Filtered rather than the audit being changed: the checks are right about the
#: document they were written for, and ``app/resume/ats_audit.py`` belongs to
#: another change. What is wrong is applying all of them to a document of a
#: different kind, and that is this module's mistake to fix.
NOT_ABOUT_A_LETTER: frozenset[FindingCode] = frozenset(
    {
        FindingCode.CONTACTS_NOT_TEXT,
        FindingCode.MISSING_SECTIONS,
        FindingCode.DATES_NOT_EXTRACTABLE,
    }
)


def audit_letter_file(content: bytes, *, filename: str) -> ATSReport:
    """Audit a generated cover letter for the things that are true of a letter.

    The same round trip as :func:`audit_file` — the .docx is read back through
    the extractor — and then the findings that are questions about a resume are
    dropped, along with the checks that raise them. See
    :data:`NOT_ABOUT_A_LETTER` for what goes and why.

    The score is recomputed from what is left rather than kept, because in this
    report the score *is* the arithmetic of the findings: leaving a 28 next to
    an empty finding list would be a number nothing on the screen could explain.
    ``overall`` needs no recomputing — it is derived from the findings, which is
    exactly why it was made derived.

    What remains is a real question and not a formality: whether the text comes
    out of the file as characters at all, and what format it was checked as. A
    letter that fails those is a broken file.
    """
    report = audit_file(content, filename=filename)
    kept = [finding for finding in report.findings if finding.code not in NOT_ABOUT_A_LETTER]
    return report.model_copy(
        update={
            "findings": kept,
            "score": max(0, 100 - sum(finding.penalty for finding in kept)),
            "checks_run": [code for code in report.checks_run if code not in NOT_ABOUT_A_LETTER],
        }
    )


def review(content: bytes, *, filename: str, text: str, context: CVContext) -> DocumentReview:
    """Audit a rendered document and measure it against its vacancy.

    ``text`` is the renderer's own plain text and is used only for the coverage
    count, where it is the right input: coverage asks what the document says,
    and the renderer's text is the document. Readability is measured from the
    file instead, because that asks what survives being written to disk — a
    different question with a different correct input, and conflating them is
    how a self-audit becomes a formality.
    """
    ats = audit_file(content, filename=filename)
    coverage = coverage_of(text, context)
    logger.info(
        "documents.reviewed",
        vacancy_id=str(context.vacancy.vacancy_id),
        profile_id=str(context.profile.profile_id),
        score=ats.score,
        overall=ats.overall.value,
        critical=len(ats.critical),
        requirements=coverage.required_total,
        named=len(coverage.named),
        held_but_unnamed=len(coverage.held_but_unnamed),
        not_held=len(coverage.not_held),
    )
    return DocumentReview(ats=ats, coverage=coverage)


#: Empty coverage, for a document kind that has no requirement list to measure
#: against. A cover letter is audited for readability like anything else, but
#: "does it name PostgreSQL literally" is not a question about a letter.
NO_COVERAGE = RequirementCoverage()


class ReviewedDocument(BaseModel):
    """A finished document, its file, and the report it is handed over with."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    filename: str
    file_format: str
    text: str
    review: DocumentReview
    #: Excluded from serialisation: an API response carries the report and a
    #: download link, never a base64 file nobody asked for.
    content: bytes = Field(exclude=True, repr=False)
