"""One vacancy in, one stored and audited document out — for either kind.

This is the only module that knows the whole sequence, and there are two of
them because the two buttons on a vacancy do genuinely different work:

    CV      load the rows -> build the context -> arrange -> check -> render ->
            audit the file -> withhold or store
    letter  delegate to app.letters, which already does all of that for prose ->
            render -> audit the file -> withhold or store

The letter path delegates rather than duplicates. ``app/letters`` already
generates, checks and stores a letter, and it is the module the sending agent
reads from; reimplementing any of it here would give the same feature two
implementations that would drift, and the one that drifted would be the one an
employer reads. What this module adds to a letter is what it adds to a CV: a
file, an audit of that file, and a version that is never overwritten.

**Withholding is a first-class outcome, not an error.** The brief is explicit:
a document that fails a hard rule is not handed over, and the person is shown
what is wrong. So :class:`DocumentOutcome` carries a document *or* a reason, and
a caller cannot get at the file without going past the reason. The two ways of
softening that — storing it anyway with a warning, or returning it and letting
the UI decide — both end with the owner sending a document the system knew was
wrong.

**Nothing here sends anything.** A document is generated, audited, stored and
handed to the person. Sending an application is ``agent/``'s, from a browser,
under the user's own account, and only after a human has confirmed that
particular application.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.enums import DocumentKind, DocumentSource, RuleScope
from app.documents import contacts, generator, render, review, rules
from app.documents import store as document_store
from app.documents.context import CVContext, build_context
from app.documents.review import DocumentReview, ReviewedDocument
from app.letters import store as letter_store
from app.letters.context import ProfileFacts
from app.letters.service import write_letter
from app.llm.router import LLMRouter
from app.services import contacts as contact_service
from app.workshop import store as workshop_store

logger = get_logger(__name__)

#: Why nothing was produced, in machine-readable form. The UI branches on these
#: and shows the Russian sentence beside each; they are not log strings.
REASONS_RU: dict[str, str] = {
    "profile_not_found": "нет активного профиля",
    "vacancy_not_found": "вакансия не найдена",
    "no_experience": (
        "в профиле нет мест работы — резюме загружено до того, как они начали "
        "сохраняться. Загрузи резюме заново, и опыт появится"
    ),
    "cv_unwritable": "в профиле слишком мало данных, чтобы собрать резюме",
    "letter_unwritable": "не удалось написать письмо, которое проходит проверки",
    "not_machine_readable": "документ не проходит ATS-проверку и поэтому не выдан",
}


@dataclass(frozen=True, slots=True)
class DocumentOutcome:
    """What happened to one generation request.

    Either ``document`` and ``stored_id`` are set, or ``reason`` is. Nothing in
    between: a document that was withheld has no file to hand over, and one that
    was handed over has no reason to explain.
    """

    vacancy_id: UUID
    kind: DocumentKind
    document: ReviewedDocument | None = None
    #: The row this was stored as, when it was stored.
    stored_id: UUID | None = None
    version: int | None = None
    source: DocumentSource | None = None
    #: Machine-readable, from :data:`REASONS_RU`. Set when nothing was handed over.
    reason: str | None = None
    #: What the checks caught on the way, whether or not it ended in a document.
    problems: tuple[str, ...] = ()
    #: Set when the document was withheld by the audit rather than by the guard,
    #: so the screen can show the report that withheld it.
    withheld_review: DocumentReview | None = None
    #: The hard rules in force, as a person reads them. Carried on every outcome
    #: so a screen explaining a withheld document does not need a second call.
    #: Empty only on an outcome built without a session to read the workshop's
    #: list from; every path in this module fills it.
    hard_rules: tuple[str, ...] = ()

    @property
    def delivered(self) -> bool:
        """Whether the person gets a file out of this."""
        return self.document is not None

    @property
    def reason_ru(self) -> str | None:
        """The reason in the language the person reads."""
        return REASONS_RU.get(self.reason, self.reason) if self.reason else None


async def load_cv_context(
    session: AsyncSession, *, vacancy_id: UUID, profile: ProfileFacts
) -> CVContext | None:
    """Everything a CV for this pair may know, or None if the vacancy is gone.

    The vacancy facts and the profile facts come from the letter store, on
    purpose: they are the same facts, read by the same queries, so a CV and a
    letter written for one vacancy on one day cannot disagree about what the
    posting asked for. What this adds is the two things a letter has no use for
    — the jobs, and the contact block.

    The contact block comes from ``profile_contact`` and falls back to the resume
    text for whatever nobody has filled in; see
    :func:`app.documents.contacts.from_stored`. Reading the columns rather than
    the page is what makes the "Мои данные" screen mean anything: a corrected
    phone number has to reach the document, or the form is decoration.
    """
    facts = await letter_store.load_vacancy_facts(session, vacancy_id)
    if facts is None:
        return None
    return build_context(
        facts,
        profile,
        contacts=contacts.from_stored(
            await contact_service.get_contacts(session, profile.profile_id),
            text=await document_store.load_resume_text(session, profile.profile_id),
            name=profile.name,
            city=profile.locations[0] if profile.locations else None,
        ),
        experience=await document_store.load_experience(session, profile.profile_id),
        education=await document_store.load_education(session, profile.profile_id),
    )


async def write_cv(
    session: AsyncSession,
    vacancy_id: UUID,
    profile: ProfileFacts,
    *,
    router: LLMRouter | None = None,
) -> DocumentOutcome:
    """Generate, check, render, audit and store one CV for one vacancy.

    Always a new version. There is no "skip if one exists" here, and the
    difference from :func:`app.letters.service.write_letter` — which does skip —
    is deliberate: a letter lives in one column that a regeneration overwrites,
    so not repeating the expensive call protects something. A CV lives in a
    version chain, and the reason the owner pressed the button a second time is
    that they want to see what changed.
    """
    # The owner's rules, scoped to CV, plus the built-ins nobody can switch off.
    # Read once: they identify the stored row, they are checked against the text,
    # and they are what a refusal is explained in terms of, and three reads could
    # disagree if the owner saved an edit mid-generation.
    cv_rules = await workshop_store.active_rules(session, scope=RuleScope.CV)
    described = rules.describe(cv_rules)

    context = await load_cv_context(session, vacancy_id=vacancy_id, profile=profile)
    if context is None:
        return DocumentOutcome(
            vacancy_id=vacancy_id,
            kind=DocumentKind.CV,
            reason="vacancy_not_found",
            hard_rules=described,
        )
    if not context.experience:
        # Refused rather than rendered without an experience section. A CV that
        # silently omits a career reads to an employer as a candidate with none,
        # and the cause is recoverable in one action, which the reason says.
        return DocumentOutcome(
            vacancy_id=vacancy_id,
            kind=DocumentKind.CV,
            reason="no_experience",
            hard_rules=described,
        )

    try:
        generated = await generator.generate(context, router=router, rules=cv_rules)
    except generator.CVUnwritableError as exc:
        logger.error(
            "documents.cv.unwritable",
            vacancy_id=str(vacancy_id),
            profile_id=str(profile.profile_id),
            problems=[problem.value for problem in exc.problems],
        )
        return DocumentOutcome(
            vacancy_id=vacancy_id,
            kind=DocumentKind.CV,
            reason="cv_unwritable",
            problems=tuple(problem.value for problem in exc.problems)
            or tuple(violation.rule_id for violation in exc.broke_rules),
            hard_rules=described,
        )

    content = render.to_docx(generated.arrangement, context)
    filename = render.filename_for(context)
    reviewed = ReviewedDocument(
        filename=filename,
        file_format=render.FILE_FORMAT,
        text=generated.text,
        review=review.review(content, filename=filename, text=generated.text, context=context),
        content=content,
    )
    problems = tuple(problem.value for problem in generated.rejected_for)

    if not reviewed.review.may_be_handed_over:
        # The guard passed and the auditor did not. Nothing is stored: a stored
        # row is a document the owner can download, and this is one the system
        # has just decided it should not.
        logger.warning(
            "documents.cv.withheld",
            vacancy_id=str(vacancy_id),
            profile_id=str(profile.profile_id),
            score=reviewed.review.ats.score,
            critical=[finding.code.value for finding in reviewed.review.ats.critical],
        )
        return DocumentOutcome(
            vacancy_id=vacancy_id,
            kind=DocumentKind.CV,
            reason="not_machine_readable",
            problems=problems,
            withheld_review=reviewed.review,
            hard_rules=described,
        )

    row = await document_store.save(
        session,
        profile_id=profile.profile_id,
        vacancy_id=vacancy_id,
        kind=DocumentKind.CV,
        payload=generated.arrangement.model_dump(mode="json"),
        text=generated.text,
        file_format=render.FILE_FORMAT,
        ats_report=reviewed.review.ats,
        rules_version=rules.version(cv_rules),
        source=_source_of(generated.source),
        problems=problems,
    )
    logger.info(
        "documents.cv.written",
        vacancy_id=str(vacancy_id),
        profile_id=str(profile.profile_id),
        version=row.version,
        source=generated.source,
        attempts=generated.attempts,
        characters=len(generated.text),
        skills=len(generated.arrangement.skills),
        jobs=len(generated.arrangement.experience),
        named=len(reviewed.review.coverage.named),
        held_but_unnamed=len(reviewed.review.coverage.held_but_unnamed),
        not_held=len(reviewed.review.coverage.not_held),
        rejected_for=list(problems),
    )
    return DocumentOutcome(
        vacancy_id=vacancy_id,
        kind=DocumentKind.CV,
        document=reviewed,
        stored_id=row.id,
        version=row.version,
        source=row.source,
        problems=problems,
        hard_rules=described,
    )


async def write_cover_letter(
    session: AsyncSession,
    vacancy_id: UUID,
    profile: ProfileFacts,
    *,
    router: LLMRouter | None = None,
) -> DocumentOutcome:
    """Generate a letter through ``app.letters``, then file, audit and version it.

    ``force=True`` on the delegated call, and it is the same decision as in
    :func:`write_cv`: the owner pressed the button, and the letters module's own
    "skip if one exists" exists to stop a *batch* re-paying for work it already
    did, not to refuse a person who asked for a new one.
    """
    # The letter's own scope. A CV rule has nothing to say about a letter, and
    # stamping the row with the CV set would make the documents screen compare
    # two letters "under different rules" because a CV rule moved between them.
    letter_rules = await workshop_store.active_rules(session, scope=RuleScope.COVER_LETTER)
    described = rules.describe(letter_rules)

    outcome = await write_letter(session, vacancy_id, profile, router=router, force=True)
    if outcome.skipped == "vacancy_not_found" or outcome.letter is None:
        reason = (
            "vacancy_not_found" if outcome.skipped == "vacancy_not_found" else "letter_unwritable"
        )
        return DocumentOutcome(
            vacancy_id=vacancy_id,
            kind=DocumentKind.COVER_LETTER,
            reason=reason,
            problems=tuple(problem.value for problem in outcome.problems),
            hard_rules=described,
        )

    context = await load_cv_context(session, vacancy_id=vacancy_id, profile=profile)
    if context is None:  # pragma: no cover - the letter above already loaded it
        return DocumentOutcome(
            vacancy_id=vacancy_id,
            kind=DocumentKind.COVER_LETTER,
            reason="vacancy_not_found",
            hard_rules=described,
        )

    text = outcome.letter.text
    content = render.letter_to_docx(text, context)
    filename = render.letter_filename_for(context)
    reviewed = ReviewedDocument(
        filename=filename,
        file_format=render.FILE_FORMAT,
        text=text,
        review=DocumentReview(
            # Audited as a letter, not as a resume. The unmodified audit scores a
            # perfectly good letter 28 and calls it unreadable, because it has no
            # contacts, no section headings and no employment dates — none of
            # which a letter has, and the first of which hh's spam filter is the
            # reason for. See :data:`app.documents.review.NOT_ABOUT_A_LETTER`.
            ats=review.audit_letter_file(content, filename=filename),
            # And measured for readability only: "does it name PostgreSQL
            # literally" is a question about a CV, and answering it here would
            # invite padding a letter with keywords, which is the other half of
            # what that spam filter is for.
            coverage=review.NO_COVERAGE,
        ),
        content=content,
    )
    problems = tuple(problem.value for problem in outcome.problems)

    if not reviewed.review.may_be_handed_over:
        # The same second gate the CV goes through, on the checks that are
        # questions about a letter. Reaching it means the file itself is broken
        # — no text layer, or glyphs that came out as noise — which is worth
        # refusing for the same reason: a stored row is a document the owner can
        # download and attach.
        #
        # The *letter* is unaffected and stays in ``application.cover_letter``,
        # where it was written a few lines above and where the sending agent
        # reads it from. It passed its own checks; what failed is the file this
        # module made out of it, and refusing an attachment is not a reason to
        # throw away a letter that can still be pasted into hh's form.
        logger.warning(
            "documents.letter.withheld",
            vacancy_id=str(vacancy_id),
            profile_id=str(profile.profile_id),
            score=reviewed.review.ats.score,
            critical=[finding.code.value for finding in reviewed.review.ats.critical],
        )
        return DocumentOutcome(
            vacancy_id=vacancy_id,
            kind=DocumentKind.COVER_LETTER,
            reason="not_machine_readable",
            problems=problems,
            withheld_review=reviewed.review,
            hard_rules=described,
        )

    row = await document_store.save(
        session,
        profile_id=profile.profile_id,
        vacancy_id=vacancy_id,
        kind=DocumentKind.COVER_LETTER,
        payload={"text": text, "source": outcome.letter.source},
        text=text,
        file_format=render.FILE_FORMAT,
        ats_report=reviewed.review.ats,
        rules_version=rules.version(letter_rules),
        source=_source_of(outcome.letter.source),
        problems=problems,
    )
    return DocumentOutcome(
        vacancy_id=vacancy_id,
        kind=DocumentKind.COVER_LETTER,
        document=reviewed,
        stored_id=row.id,
        version=row.version,
        source=row.source,
        problems=problems,
        hard_rules=described,
    )


async def rebuild(session: AsyncSession, document_id: UUID) -> ReviewedDocument | None:
    """Rebuild a stored version's file from its arrangement, for a download.

    The file is not kept; the arrangement is, and rendering is a pure function
    of it and the profile — see :mod:`app.documents.render`. So a download is a
    re-render rather than a read, and a version downloaded a month later is the
    same document it was, unless the profile behind it changed, in which case it
    is the document that profile now supports. That is the honest behaviour for
    a file that carries somebody's employment history: a stored blob would keep
    asserting a job the resume no longer claims.

    The audit is the stored one, not a fresh one. It is what the document was
    handed over under, and recomputing it here would quietly replace the record
    of that decision with today's opinion.
    """
    row = await document_store.by_id(session, document_id)
    if row is None:
        return None
    profile = await letter_store.load_profile_facts(session, row.profile_id)
    if profile is None:  # pragma: no cover - the row's FK is ON DELETE CASCADE
        return None
    context = await load_cv_context(session, vacancy_id=row.vacancy_id, profile=profile)
    if context is None:
        return None

    if row.kind is DocumentKind.COVER_LETTER:
        content = render.letter_to_docx(row.text, context)
        filename = render.letter_filename_for(context)
        coverage = review.NO_COVERAGE
    else:
        arrangement = render.CVArrangement.model_validate(row.payload)
        content = render.to_docx(arrangement, context)
        filename = render.filename_for(context)
        coverage = review.coverage_of(row.text, context)

    return ReviewedDocument(
        filename=filename,
        file_format=row.file_format,
        text=row.text,
        review=DocumentReview(ats=row.ats_report, coverage=coverage),
        content=content,
    )


def _source_of(source: str) -> DocumentSource:
    """The stored enum for a generator's ``"model"`` / ``"fallback"``."""
    return DocumentSource.MODEL if source == "model" else DocumentSource.FALLBACK
