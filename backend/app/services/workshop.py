"""The workshop: the queue of vacancies waiting for a letter, and writing one.

The one place the dashboard is allowed to change anything, and what it changes
is a document. A letter is generated, checked and saved onto the tracker row.
Nothing here sends it, nothing here can, and the boundary is not a matter of
this module's discipline: ``backend/`` has no browser, no session on hh and no
credentials of the owner's, and ``agent/`` — which has all three — cannot be
imported from here (``agent/tests/test_isolation.py`` parses the import graph
and fails the build if it ever is).

So the screen this serves ends at a letter a person can read. Sending it is
``wwao apply --send``, which shows the same letter on a confirmation card and
waits for a human at the keyboard.

Everything else in this module is reporting. ``write`` returns what actually
happened — whether the model wrote the text or the rule-based fallback did,
what the guard caught on the way, how much past-outcome evidence went into the
prompt — because those runs reach the database looking identical and a screen
that could not tell them apart would credit the feedback loop for letters
written without any of it.
"""

from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.letters import store
from app.letters.guard import ENGLISH, RUSSIAN
from app.letters.service import LetterOutcome, write_letter
from app.llm.router import LLMRouter
from app.schemas.dashboard import LetterProblemRead, QueuedLetter, WorkshopResult

logger = get_logger(__name__)

#: Default floor for the queue of vacancies worth a letter: None, which
#: ``app/letters/store.queue`` reads as ``agent_queue_min_score`` — the same
#: floor ``app/letters/service.write_batch`` uses, because a screen that offered
#: letters for vacancies the batch would not write is a screen that disagrees
#: with the tool it is a front end for. A hard-coded 70 was the skip bucket on
#: the title formula's scale.
DEFAULT_MIN_SCORE: Decimal | None = None

#: How many vacancies the queue panel shows at once.
DEFAULT_QUEUE_LIMIT = 20

#: The one outcome of :func:`write` that is not about the vacancy asked for. A
#: named constant because the router answers it with a different status code,
#: and a second spelling of the string would turn a 409 into a silent 200.
NO_PROFILE = "no_active_profile"


async def queue(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_QUEUE_LIMIT,
    min_score: Decimal | None = DEFAULT_MIN_SCORE,
    include_written: bool = True,
) -> list[QueuedLetter]:
    """The best-scoring vacancies for the active profile, letter or not.

    ``include_written`` defaults on here and off in the batch writer, and the
    difference is the difference between a screen and a job. The batch skips
    what it has already written so a repeated run costs nothing; the screen
    shows it, with :attr:`QueuedLetter.has_letter` set, because "this one is
    done" is exactly what somebody looking at a queue needs to see.
    """
    profile = await store.load_profile_facts(session)
    if profile is None:
        logger.info("workshop.queue.no_profile")
        return []
    queued = await store.queue(
        session,
        profile_id=profile.profile_id,
        limit=limit,
        min_score=min_score,
        include_written=include_written,
    )
    return [
        QueuedLetter(
            vacancy_id=item.vacancy_id,
            title=item.title,
            company=item.company,
            score=item.score,
            has_letter=item.has_letter,
        )
        for item in queued
    ]


async def write(
    session: AsyncSession,
    vacancy_id: UUID,
    *,
    force: bool = False,
    router: LLMRouter | None = None,
) -> WorkshopResult:
    """Write one letter for one vacancy and save it.

    ``force`` rewrites a letter that already exists. Off by default so a second
    click costs nothing — the same idempotence the connectors follow, for the
    same reason: the expensive call is the one worth not making twice — and
    because a regeneration would silently replace a letter the owner may have
    edited by hand.

    Commits, because the caller is an HTTP request that has nothing else to do
    with the session and a letter that took a model call to produce must not be
    lost to a rollback nobody asked for.

    ``router`` is the seam a test writes through. Left out, the configured one
    is used; passed in, a caller can exercise the fallback path without a
    model, which is the branch that has to keep working when no provider is
    reachable.
    """
    profile = await store.load_profile_facts(session)
    if profile is None:
        return WorkshopResult(vacancy_id=vacancy_id, title="", skipped=NO_PROFILE)

    outcome = await write_letter(session, vacancy_id, profile, force=force, router=router)
    if outcome.saved:
        await session.commit()
    return _result(outcome)


def _result(outcome: LetterOutcome) -> WorkshopResult:
    """One generation, as the screen reads it."""
    letter = outcome.letter
    return WorkshopResult(
        vacancy_id=outcome.vacancy_id,
        title=outcome.title,
        company=outcome.company,
        saved=outcome.saved,
        skipped=outcome.skipped,
        text=letter.text if letter is not None else None,
        characters=outcome.characters,
        # ``source`` and not "no problems": a letter the model wrote after two
        # rejected attempts is still the model's, and one the fallback assembled
        # cleanly on the first try is still the fallback's. The distinction the
        # screen needs is which text a person is about to send.
        from_model=letter is not None and letter.source == "model",
        matched_skills=outcome.matched,
        missing_skills=outcome.missing,
        problems=[
            LetterProblemRead(code=problem.value, message=RUSSIAN.get(problem, ENGLISH[problem]))
            for problem in outcome.problems
        ],
        evidence=outcome.evidence,
        evidence_is_enough=outcome.evidence.is_enough,
    )
