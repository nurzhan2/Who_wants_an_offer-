"""One vacancy in, one saved letter out — and the same thing over a queue.

This is the only module that knows the whole sequence:

    load the rows -> compute the overlap -> generate -> check -> save

Everything it reports is a fact about what happened, not a summary of it: which
vacancy, where the text came from, what the checks caught, whether anything was
written. A run that produced ten fallbacks and a run that produced ten model
letters are not the same run, and a report that cannot tell them apart is the
kind of green tick nobody should trust.

The letter is saved and never sent. Sending belongs to ``agent/``, from a
browser, under the user's own account, and only after a human has confirmed that
particular letter for that particular vacancy.
"""

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.letters import store
from app.letters.context import ProfileFacts, build_context
from app.letters.generator import GeneratedLetter, LetterUnwritableError, generate
from app.letters.guard import LetterProblem
from app.llm.router import LLMRouter, get_router

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
) -> LetterOutcome:
    """Generate and save one letter.

    ``force`` overwrites a letter that is already stored. Without it an existing
    letter is left alone: a batch that regenerates what it wrote yesterday burns
    the expensive call for nothing, and would quietly replace a letter the person
    may have edited by hand.
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
    if dry_run:
        return LetterOutcome(
            vacancy_id=vacancy_id,
            title=facts.title,
            company=facts.company,
            matched=len(context.overlap.matched),
            missing=len(context.overlap.missing),
            skipped="dry_run",
        )

    try:
        letter = await generate(context, router=router or get_router())
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
            matched=len(context.overlap.matched),
            missing=len(context.overlap.missing),
        )
        return LetterOutcome(
            vacancy_id=vacancy_id,
            title=facts.title,
            company=facts.company,
            matched=len(context.overlap.matched),
            missing=len(context.overlap.missing),
            skipped="letter_unwritable",
        )

    await store.save_letter(session, vacancy_id=vacancy_id, text=letter.text)

    logger.info(
        "letters.written",
        vacancy_id=str(vacancy_id),
        source=letter.source,
        attempts=letter.attempts,
        characters=len(letter.text),
        matched=len(context.overlap.matched),
        missing=len(context.overlap.missing),
        rejected_for=[problem.value for problem in letter.rejected_for],
    )
    return LetterOutcome(
        vacancy_id=vacancy_id,
        title=facts.title,
        company=facts.company,
        letter=letter,
        matched=len(context.overlap.matched),
        missing=len(context.overlap.missing),
        characters=len(letter.text),
        saved=True,
    )


async def write_batch(
    session: AsyncSession,
    *,
    profile_id: UUID | None = None,
    limit: int = 10,
    min_score: Decimal = Decimal("70"),
    router: LLMRouter | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> list[LetterOutcome]:
    """Work down the queue of vacancies that still need a letter.

    Sequential on purpose. The heavy tasks route to the Claude Code CLI, whose
    provider holds a concurrency semaphore of its own; firing a hundred of these
    at once would queue on that semaphore anyway while making the run impossible
    to interrupt cleanly halfway through.
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
            )
        )
    return outcomes
