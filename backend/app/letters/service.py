"""One vacancy in, one saved letter out — and the same thing over a queue.

This is the only module that knows the whole sequence:

    load the rows -> compute the overlap -> pick the examples -> generate ->
    check -> save

Everything it reports is a fact about what happened, not a summary of it: which
vacancy, where the text came from, what the checks caught, whether anything was
written. A run that produced ten fallbacks and a run that produced ten model
letters are not the same run, and a report that cannot tell them apart is the
kind of green tick nobody should trust. The same goes for the examples: a letter
written with two past letters in its prompt and a letter written with none reach
the database looking identical, so :attr:`LetterOutcome.evidence` carries the
counts that say which happened.

**The example step is the feedback loop, and it is few-shot prompting.** Nothing
is trained; letters that got an answer are pasted into the next prompt. The
common case is that there are none — see :mod:`app.letters.examples` — and in
that case this step changes nothing at all.

**The workshop step is two different things wearing one name.** A reference
document changes the prompt and nothing else. A hard rule changes what may be
saved: it is measured against the finished text, and a letter that breaks one is
not written at all — the outcome then carries ``letter_unwritable`` and the list
of what was broken, which is the honest answer to a rule this profile cannot
satisfy. Both are read once per run, like the examples, and both are empty until
the owner sets something.

The letter is saved and never sent. Sending belongs to ``agent/``, from a
browser, under the user's own account, and only after a human has confirmed that
particular letter for that particular vacancy.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.enums import ReferenceKind, RuleScope
from app.letters import examples as few_shot
from app.letters import store
from app.letters.context import LetterContext, ProfileFacts, build_context
from app.letters.examples import ChosenExample, OutcomeEvidence
from app.letters.generator import GeneratedLetter, LetterUnwritableError, generate
from app.letters.guard import LetterProblem
from app.llm.router import LLMRouter, get_router
from app.schemas.ats import ATSReport, DocumentKind
from app.services import ats as ats_service
from app.workshop import store as workshop_store
from app.workshop.references import ReferenceText
from app.workshop.rules import BUILTIN_RULES, RuleSpec, RuleViolation

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LetterOutcome:
    """What happened to one vacancy in a run."""

    vacancy_id: UUID
    title: str
    company: str | None
    #: None when nothing was generated — see :attr:`skipped`.
    letter: GeneratedLetter | None = None
    matched: int = 0
    missing: int = 0
    characters: int = 0
    saved: bool = False
    #: Set to a machine-readable reason when no letter was written: the vacancy
    #: is gone, a letter already exists, this was a dry run, or nothing could be
    #: written that passes the checks (``letter_unwritable``). The last one is a
    #: failure rather than a skip and is logged at error level, but it reaches
    #: the report the same way, because the report is what a person reads.
    skipped: str | None = None
    #: What was known about past outcomes when this letter was written, in
    #: counts. Whatever displays it has to be able to say «данных пока мало» and
    #: mean it, which is why this is counts and a flag rather than a rate.
    evidence: OutcomeEvidence = field(default_factory=OutcomeEvidence)
    #: Soft rules the saved letter still breaks, and the hard ones that stopped
    #: it being written at all. A soft violation travels beside a letter that
    #: was saved; a hard one comes with ``skipped="letter_unwritable"`` and is
    #: the whole of the explanation the owner gets, so it is carried on the
    #: outcome rather than left in a log.
    warnings: tuple[RuleViolation, ...] = ()
    broken_rules: tuple[RuleViolation, ...] = ()
    #: The ATS audit of the text this run produced, read against the vacancy it
    #: was written for. None only when nothing was generated. See
    #: :func:`write_letter` for why the audit happens before the save.
    ats: ATSReport | None = None

    @property
    def problems(self) -> tuple[LetterProblem, ...]:
        """Everything the checks caught while generating this one."""
        return self.letter.rejected_for if self.letter else ()


async def write_letter(
    session: AsyncSession,
    vacancy_id: UUID,
    profile: ProfileFacts,
    *,
    router: LLMRouter | None = None,
    force: bool = False,
    dry_run: bool = False,
    pool: few_shot.ExamplePool | None = None,
    workshop: "Workshop | None" = None,
) -> LetterOutcome:
    """Generate and save one letter.

    ``force`` overwrites a letter that is already stored. Without it an existing
    letter is left alone: a batch that regenerates what it wrote yesterday burns
    the expensive call for nothing, and would quietly replace a letter the person
    may have edited by hand.

    ``pool`` is the run's past outcomes, read once by :func:`write_batch` and
    passed down. Left out, this loads them itself, so writing a single letter
    from the command line gets the same examples a batch would.

    ``workshop`` is the owner's rules and reference documents, read the same way
    and for the same reason. Left out it is loaded here, so one letter written
    from the command line obeys the same rules a batch does — a rule that
    applied to nine letters and not to the tenth would be worse than no rule.
    """
    facts = await store.load_vacancy_facts(session, vacancy_id)
    if facts is None:
        return LetterOutcome(
            vacancy_id=vacancy_id, title="", company=None, skipped="vacancy_not_found"
        )

    if not force and await store.existing_letter(session, vacancy_id) is not None:
        return LetterOutcome(
            vacancy_id=vacancy_id,
            title=facts.title,
            company=facts.company,
            skipped="letter_exists",
        )

    context = build_context(facts, profile)
    chosen, evidence = await _examples_for(session, context, profile, pool=pool)
    if dry_run:
        return LetterOutcome(
            vacancy_id=vacancy_id,
            title=facts.title,
            company=facts.company,
            matched=len(context.overlap.matched),
            missing=len(context.overlap.missing),
            evidence=evidence,
            skipped="dry_run",
        )

    bench = workshop if workshop is not None else await load_workshop(session)

    try:
        letter = await generate(
            context,
            router=router or get_router(),
            examples=chosen,
            rules=bench.rules,
            references=bench.references,
        )
    except LetterUnwritableError as exc:
        # Nothing is saved. A letter that fails a hard constraint is worse than
        # an empty column: the column is visible in the report below and in the
        # dashboard, while a saved stub looks exactly like a finished letter
        # until an employer reads it. One vacancy's worth of "no" does not end
        # the batch — the next vacancy has a different context.
        logger.error(
            "letters.unwritable",
            vacancy_id=str(vacancy_id),
            problems=[problem.value for problem in exc.problems],
            broken_rules=[violation.rule_id for violation in exc.violations],
            matched=len(context.overlap.matched),
            missing=len(context.overlap.missing),
        )
        return LetterOutcome(
            vacancy_id=vacancy_id,
            title=facts.title,
            company=facts.company,
            matched=len(context.overlap.matched),
            missing=len(context.overlap.missing),
            evidence=evidence,
            broken_rules=exc.violations,
            skipped="letter_unwritable",
        )

    # The system auditing its own output. Everything above this point checks
    # the letter against rules about letters; this checks the finished text the
    # way an employer's parser will read it, and it runs before the save rather
    # than on the way to a screen, so a document that fails it never becomes a
    # thing a person can send by clicking once.
    report = await ats_service.audit_generated_for_vacancy(
        session,
        letter.text,
        kind=DocumentKind.COVER_LETTER,
        profile_id=profile.profile_id,
        vacancy_id=vacancy_id,
    )
    if not report.is_machine_readable:
        # Only one finding can land here on plain text: hidden characters. A
        # letter is written from a job description somebody else wrote, so an
        # invisible keyword block reaching the text is a thing that arrived
        # rather than a thing we chose — and it would go out under the owner's
        # name. Not saved, and loud, for the reason the module docstring gives
        # about saving something that breaks a hard constraint.
        logger.error(
            "letters.failed_ats_audit",
            vacancy_id=str(vacancy_id),
            findings=[finding.code.value for finding in report.critical],
            score=report.score,
        )
        return LetterOutcome(
            vacancy_id=vacancy_id,
            title=facts.title,
            company=facts.company,
            matched=len(context.overlap.matched),
            missing=len(context.overlap.missing),
            evidence=evidence,
            ats=report,
            skipped="letter_failed_audit",
        )

    # Imported here rather than at module scope: ``app.documents`` loads the CV
    # service, which imports this module, so a top-level import closes that
    # circle. Moving the fingerprint into a module of its own would give one
    # function two homes, which is what the merge was cleaning up.
    from app.documents import rules as document_rules

    await store.save_letter(
        session,
        vacancy_id=vacancy_id,
        text=letter.text,
        profile_id=profile.profile_id,
        # The rules that actually judged this letter: the built-in guard and
        # whatever the owner had active in the workshop at the time. Recorded as
        # the same fingerprint a generated document carries, so the two tables
        # answer "which rules wrote this" in one vocabulary.
        rules_version=document_rules.version(
            # The workshop's two undeletable rules are enforced inside
            # ``generate`` rather than carried on ``bench`` — that is what stops
            # them being checked twice — but they judged this letter as much as
            # the owner's own did, so the identity of the set includes them.
            tuple(rule for rule in BUILTIN_RULES if rule.applies_to(RuleScope.COVER_LETTER))
            + tuple(bench.rules),
            scope=RuleScope.COVER_LETTER,
        ),
    )

    logger.info(
        "letters.written",
        vacancy_id=str(vacancy_id),
        source=letter.source,
        attempts=letter.attempts,
        characters=len(letter.text),
        matched=len(context.overlap.matched),
        missing=len(context.overlap.missing),
        rejected_for=[problem.value for problem in letter.rejected_for],
        broke_rules=[violation.rule_id for violation in letter.broke_rules],
        warnings=[violation.rule_id for violation in letter.warnings],
        examples_used=letter.examples_used,
        references_used=len(bench.references),
        outcomes_known=evidence.answered,
        ats_score=report.score,
        requirements_present=len(report.keywords.present) if report.keywords else None,
    )
    return LetterOutcome(
        vacancy_id=vacancy_id,
        title=facts.title,
        company=facts.company,
        letter=letter,
        matched=len(context.overlap.matched),
        missing=len(context.overlap.missing),
        characters=len(letter.text),
        evidence=evidence,
        warnings=letter.warnings,
        ats=report,
        saved=True,
    )


@dataclass(frozen=True, slots=True)
class Workshop:
    """The owner's rules and reference documents, read once for a run.

    A pair rather than two arguments because they are read together, passed
    together and empty together: an owner who has set nothing gets an empty one
    and a prompt identical to the one sent before the workshop existed.
    """

    rules: tuple[RuleSpec, ...] = ()
    references: tuple[ReferenceText, ...] = ()


async def load_workshop(session: AsyncSession) -> Workshop:
    """The rules and references that apply to a cover letter, right now.

    Only the owner's own rules: the two built-ins are enforced unconditionally
    inside :func:`app.letters.generator.generate` and would be checked twice if
    they travelled here as well. Only the active ones, and only those scoped to
    cover letters — a rule about a CV has nothing to say about a letter.
    """
    return Workshop(
        rules=await workshop_store.stored_rules(
            session, scope=RuleScope.COVER_LETTER, active_only=True
        ),
        references=await workshop_store.active_references(session, kind=ReferenceKind.COVER_LETTER),
    )


async def _examples_for(
    session: AsyncSession,
    context: LetterContext,
    profile: ProfileFacts,
    *,
    pool: few_shot.ExamplePool | None,
) -> tuple[tuple[ChosenExample, ...], OutcomeEvidence]:
    """The past letters this vacancy gets shown, and what is known about them.

    Selection happens per vacancy because "similar" is a question about *this*
    vacancy's requirement list; the query behind it happens once per run.
    """
    if pool is None:
        pool = await store.load_examples(session, profile_id=profile.profile_id)
    return few_shot.select(pool, context)


async def write_batch(
    session: AsyncSession,
    *,
    profile_id: UUID | None = None,
    limit: int = 10,
    min_score: Decimal | None = None,
    router: LLMRouter | None = None,
    force: bool = False,
    dry_run: bool = False,
    pool: few_shot.ExamplePool | None = None,
    workshop: Workshop | None = None,
) -> list[LetterOutcome]:
    """Work down the queue of vacancies that still need a letter.

    Sequential on purpose. The heavy tasks route to the Claude Code CLI, whose
    provider holds a concurrency semaphore of its own; firing a hundred of these
    at once would queue on that semaphore anyway while making the run impossible
    to interrupt cleanly halfway through.

    ``pool`` lets a caller that already has the run's past outcomes — a script
    that wants to print how much is known before it prints the letters — hand
    them in instead of paying for the same query twice.
    """
    profile = await store.load_profile_facts(session, profile_id)
    if profile is None:
        return []

    queued = await store.queue(
        session,
        profile_id=profile.profile_id,
        limit=limit,
        min_score=min_score,
        include_written=force,
    )
    # Once for the run: the answered applications do not change while a batch is
    # being written, and which of them suit a given vacancy is decided per
    # vacancy from this same list.
    if pool is None:
        pool = await store.load_examples(session, profile_id=profile.profile_id)
    # Once for the run, like the pool and for the same reason: the rules do not
    # change while a batch is being written, and re-reading them per vacancy
    # would also let a mid-run edit apply to half the letters.
    if workshop is None:
        workshop = await load_workshop(session)
    outcomes: list[LetterOutcome] = []
    for item in queued:
        outcomes.append(
            await write_letter(
                session,
                item.vacancy_id,
                profile,
                router=router,
                force=force,
                dry_run=dry_run,
                pool=pool,
                workshop=workshop,
            )
        )
    return outcomes
