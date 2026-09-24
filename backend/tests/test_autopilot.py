"""The autopilot: what the selection refuses, the chain, and the batch.

The property every test here defends is the one the whole change hangs on:
**one confirmation covers a batch only because nothing reaches the batch that a
person would have caught.** So the file is in three parts.

* The selection. Each check that was added — the experience gap, the language
  requirement, the re-read of the posting's own page, the workshop rules and the
  ATS audit over a stored letter — keeps its vacancy out of the queue *and*
  produces the sentence the owner reads about it. A check that only dropped the
  row would move the September defects from the card to nowhere.
* The chain. It stops at the step that failed and says which; a second run
  continues from there rather than re-crawling; and it never sends.
* The batch. N confirmations, one per card digest; a row whose card moved is
  refused on its own; the ceiling — and the smaller first batch after a quiet
  period — is enforced before anything is written. And nothing, anywhere in this
  path, sends an application.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import uuid7
from app.db.models import Application, CandidateProfile, Vacancy, VacancySource
from app.db.repositories.match import MatchRepository
from app.db.repositories.profile import ProfileRepository
from app.db.repositories.vacancy import VacancyRepository
from app.letters.service import LetterOutcome
from app.matching.scorer import ProfileNotReadyError, ScoringOutcome
from app.pipeline.embedding import EmbeddingOutcome
from app.schemas.ats import Overall
from app.schemas.autopilot import BatchConfirmItem, BatchConfirmRequest, SetAsideKind
from app.schemas.operations import ChainStep, ChainStepStatus, OperationKind
from app.schemas.pipeline_job import PipelineJobList, PipelineJobRead, PipelineJobStatus
from app.schemas.source import PlanSummary, RunResponse
from app.services import agent_queue, autopilot, freshness
from factories import make_match, make_profile, make_vacancy

pytestmark = pytest.mark.db

HH_ID = "137000001"
HH_URL = f"https://almaty.hh.kz/vacancy/{HH_ID}"
LETTER = "Здравствуйте! Пишу по вакансии Python-разработчика: писал бэкенды на FastAPI."
BATCH = f"{settings.api_v1_prefix}/tracker/batch"


async def _ready(
    session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
    *,
    external_id: str = HH_ID,
    letter: str | None = LETTER,
    seen_hours_ago: float = 0.0,
    score: Decimal = Decimal("91"),
    **match_fields: Any,
) -> UUID:
    """One vacancy that passes every check, unless a caller spoils exactly one.

    Everything a test then changes — the gap, the red flags, the page's age, the
    letter — is one of the checks under test, so a failure names the check
    rather than the fixture.
    """
    # One profile for the whole test, whichever vacancy asks first. The queue
    # scores against the newest active profile, so a second profile created by a
    # second call would orphan the first vacancy's match — and the test would
    # fail for a reason that has nothing to do with what it is about.
    existing = await session.scalar(
        select(CandidateProfile.id).where(CandidateProfile.is_active.is_(True)).limit(1)
    )
    if existing is None:
        created = await profiles.create(make_profile())
        await session.execute(
            update(CandidateProfile).where(CandidateProfile.id == created.id).values(is_active=True)
        )
        profile_id = created.id
    else:
        profile_id = existing
    url = f"https://almaty.hh.kz/vacancy/{external_id}"
    result = await vacancies.upsert_by_external_id(
        make_vacancy(f"autopilot-{external_id}"),
        source_slug="hh",
        external_id=external_id,
        url=url,
        raw={"_derived": {"external_id": external_id, "url": url, "key_skills": ["Python"]}},
    )
    await matches.bulk_upsert([make_match(profile_id, result.vacancy_id, score, **match_fields)])
    await session.execute(
        update(Vacancy)
        .where(Vacancy.id == result.vacancy_id)
        .values(last_seen_at=datetime.now(UTC) - timedelta(hours=seen_hours_ago))
    )
    if letter is not None:
        session.add(Application(id=uuid7(), vacancy_id=result.vacancy_id, cover_letter=letter))
    await session.flush()
    return result.vacancy_id


# ── what the selection refuses ────────────────────────────────────────


async def test_a_vacancy_that_passes_everything_is_ready_and_nothing_is_set_aside(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    await _ready(db_session, vacancies, profiles, matches)

    selection = await agent_queue.triage(db_session, limit=10)

    assert [ready.item.vacancy_id for ready in selection.ready] == [HH_ID]
    assert selection.set_aside == []


async def test_more_experience_than_the_threshold_allows_is_set_aside_with_the_number(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """The 9 September defect: six years asked of a candidate with 1.1, in the queue.

    The scorer's own filter starts at four years, which is a different question —
    whether the vacancy belongs in the list at all. This is the narrower one:
    whether an application is worth one of fifteen daily slots.
    """
    await _ready(
        db_session,
        vacancies,
        profiles,
        matches,
        experience_gap_years=Decimal(str(settings.agent_max_experience_gap_years)) + 1,
    )

    selection = await agent_queue.triage(db_session, limit=10)

    assert selection.ready == []
    [aside] = selection.set_aside
    assert aside.kind is SetAsideKind.EXPERIENCE_GAP
    # The threshold itself, in the reason, as a person writes a number of years
    # rather than as a float repr: «до 1 года», never «до 1.0 года». Asserted on
    # the whole phrase, because the number alone is a substring of the wrong
    # rendering too and this check passed against it for a while.
    assert f"до {settings.agent_max_experience_gap_years:g} " in aside.reason
    assert "1.0" not in aside.reason
    assert (await agent_queue.build_queue(db_session, limit=10)).items == []


async def test_exactly_the_threshold_still_passes(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """The cut is "more than", not "at least": the boundary is inside the queue."""
    await _ready(
        db_session,
        vacancies,
        profiles,
        matches,
        experience_gap_years=Decimal(str(settings.agent_max_experience_gap_years)),
    )

    selection = await agent_queue.triage(db_session, limit=10)

    assert [ready.item.vacancy_id for ready in selection.ready] == [HH_ID]


async def test_a_language_asked_above_the_held_level_is_set_aside(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """A language the owner does not hold at all is already a filtered bucket.

    This is the other case: held, but weaker than asked. Scoring flags it and
    goes on scoring; the queue must not send an application into it.
    """
    await _ready(
        db_session,
        vacancies,
        profiles,
        matches,
        red_flags=["английский: требуется C1, заявлен B1"],
    )

    selection = await agent_queue.triage(db_session, limit=10)

    assert selection.ready == []
    [aside] = selection.set_aside
    assert aside.kind is SetAsideKind.LANGUAGE
    assert "C1" in aside.reason


async def test_another_red_flag_is_not_mistaken_for_a_language_gap(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """The column stores prose, so the reader has to be narrower than "a flag"."""
    await _ready(
        db_session,
        vacancies,
        profiles,
        matches,
        red_flags=["зарплата ниже вашей нижней границы"],
    )

    selection = await agent_queue.triage(db_session, limit=10)

    assert [ready.item.vacancy_id for ready in selection.ready] == [HH_ID]


async def test_a_page_nobody_has_read_recently_is_set_aside(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """Measured 17 Sep 2026: every hh row in the corpus was nine days stale.

    The database saying "open" is a memory of the last crawl, and a third of the
    16 Sep queue was archived by the time the agent opened it.
    """
    await _ready(
        db_session,
        vacancies,
        profiles,
        matches,
        seen_hours_ago=settings.agent_page_freshness_hours + 1,
    )

    selection = await agent_queue.triage(db_session, limit=10)

    assert selection.ready == []
    [aside] = selection.set_aside
    assert aside.kind is SetAsideKind.STALE_PAGE
    assert aside.url == HH_URL


async def test_a_letter_breaking_a_hard_workshop_rule_is_set_aside(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """A built-in rule, re-checked over the stored letter rather than trusted.

    The letter passed the rules when it was written. Rules change, letters are
    edited, and this check is the difference between "it passed once" and "it
    passes now".
    """
    await _ready(
        db_session,
        vacancies,
        profiles,
        matches,
        letter=f"{LETTER} Портфолио: https://example.com/me",
    )

    selection = await agent_queue.triage(db_session, limit=10)

    assert selection.ready == []
    [aside] = selection.set_aside
    assert aside.kind is SetAsideKind.LETTER_RULES
    assert "спам" in aside.reason


async def test_a_letter_the_ats_audit_calls_unreadable_is_set_aside(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """The one check on this list a person could not perform by reading the card.

    Zero-width characters inside the text are invisible by definition: the
    letter looks perfect on the batch screen and reads to an applicant tracking
    system as the keyword-stuffing technique it is, which gets the *candidate*
    flagged rather than the file rejected. An eye cannot catch it, so the
    argument that one confirmation replaces a look does not cover it — and that
    is exactly why the audit is run again over the stored letter rather than
    trusted from the day it was written.
    """
    await _ready(
        db_session,
        vacancies,
        profiles,
        matches,
        letter=f"{LETTER}​​​",
    )

    selection = await agent_queue.triage(db_session, limit=10)

    assert selection.ready == []
    [aside] = selection.set_aside
    assert aside.kind is SetAsideKind.LETTER_AUDIT
    assert "скрытый текст" in aside.reason.lower()


async def test_a_letter_the_audit_merely_grades_down_still_goes_out(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """``degraded`` is the ordinary state of a letter, not a defect.

    A letter that does not name every requirement the posting listed is graded
    down and is still a letter worth sending; refusing on that would empty the
    queue and teach nobody anything. Only ``unreadable`` — a critical finding —
    stops one. The line between the two is worth a test of its own, because
    moving it one notch stricter would make the whole feature useless in a way
    that looks like caution.
    """
    vacancy_id = await _ready(
        db_session,
        vacancies,
        profiles,
        matches,
        # The fixture's posting lists Python and this letter never says the word,
        # which is what the audit grades down for.
        letter="Здравствуйте! Пишу по вакансии: собирал бэкенды и довозил их до продакшена.",
    )

    selection = await agent_queue.triage(db_session, limit=10)

    [ready] = selection.ready
    assert ready.vacancy_id == vacancy_id
    assert ready.item.ats is not None
    assert ready.item.ats.overall is Overall.DEGRADED
    assert ready.item.ats.critical == []
    assert ready.item.ats.unstated == ["Python"]


async def test_without_a_letter_the_vacancy_is_set_aside_but_stays_visible(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """Nothing is thrown away: the owner can still open it and apply by hand.

    And with ``require_letter=False`` — the question the chain asks before it
    writes letters — the same vacancy is ready, which is what makes "прошедшие
    отбор" a list the letters step can name.
    """
    await _ready(db_session, vacancies, profiles, matches, letter=None)

    strict = await agent_queue.triage(db_session, limit=10)
    lenient = await agent_queue.triage(db_session, limit=10, require_letter=False)

    assert strict.ready == []
    [aside] = strict.set_aside
    assert aside.kind is SetAsideKind.NO_LETTER
    assert aside.url == HH_URL
    assert [ready.item.vacancy_id for ready in lenient.ready] == [HH_ID]


async def test_the_card_of_a_set_aside_vacancy_says_why_and_what_to_do(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
    async_client: AsyncClient,
) -> None:
    """The single card used to report the one reason it could name, for any cause."""
    vacancy_id = await _ready(
        db_session,
        vacancies,
        profiles,
        matches,
        seen_hours_ago=settings.agent_page_freshness_hours + 1,
    )

    card = (
        await async_client.get(f"{settings.api_v1_prefix}/tracker/confirmations/{vacancy_id}")
    ).json()

    assert card["item"] is None
    [blocker] = card["blockers"]
    assert "перечитывали" in blocker
    assert "цепочку" in blocker


async def test_a_url_that_does_not_name_its_own_vacancy_is_set_aside_not_silently_dropped(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """It has to leave the queue, and it must not leave without a trace.

    The agent rejects a whole batch on that mismatch, so the item cannot be
    served. But before this the row was dropped with a log line and nothing
    else, so a vacancy scoring 91 simply never appeared — on any screen, with no
    reason anywhere a person looks.
    """
    await _ready(db_session, vacancies, profiles, matches, external_id="137000030")
    await db_session.execute(
        update(VacancySource)
        .where(VacancySource.external_id == "137000030")
        .values(url="https://almaty.hh.kz/vacancy/999999999")
    )

    selection = await agent_queue.triage(db_session, limit=10)

    assert selection.ready == []
    [aside] = selection.set_aside
    assert aside.kind is SetAsideKind.UNSERVABLE
    assert "137000030" in aside.reason


async def test_the_page_age_is_the_last_check_so_stale_means_only_that(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """The ordering the chain depends on, asserted rather than assumed.

    ``app.services.autopilot._stale_candidates`` re-reads exactly the rows whose
    kind is ``stale_page``, on the strength of that kind meaning "nothing else
    is wrong with this one". If the age were checked before the experience gap,
    the chain would spend a polite hh request per vacancy it is never going to
    apply to — and a vacancy asking six years would be reported to the owner as
    «страницу давно не перечитывали», which is not what is wrong with it.
    """
    await _ready(
        db_session,
        vacancies,
        profiles,
        matches,
        external_id="137000031",
        seen_hours_ago=settings.agent_page_freshness_hours + 1,
        experience_gap_years=Decimal(str(settings.agent_max_experience_gap_years)) + 1,
    )

    selection = await agent_queue.triage(db_session, limit=10)

    [aside] = selection.set_aside
    assert aside.kind is SetAsideKind.EXPERIENCE_GAP


async def test_what_the_agent_is_handed_is_exactly_what_passed_the_selection(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """The property one confirmation for a batch actually rests on.

    The batch screen is built from ``triage``; the agent is served by
    ``build_queue``. If those were two selections, the owner could confirm one
    list and the agent could be handed another — which is precisely the failure
    «подтвердить пачку» would make invisible, because nobody reads the second
    list. They are the same function, and this says so out loud.
    """
    good = await _ready(db_session, vacancies, profiles, matches, external_id="137000032")
    await _ready(
        db_session,
        vacancies,
        profiles,
        matches,
        external_id="137000033",
        seen_hours_ago=settings.agent_page_freshness_hours + 1,
    )

    selection = await agent_queue.triage(db_session, limit=10)
    served = await agent_queue.build_queue(db_session, limit=10)

    assert [ready.vacancy_id for ready in selection.ready] == [good]
    assert [item.vacancy_id for item in served.items] == [
        ready.item.vacancy_id for ready in selection.ready
    ]
    assert [aside.kind for aside in selection.set_aside] == [SetAsideKind.STALE_PAGE]


async def test_vacancies_set_aside_do_not_starve_the_ready_list(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """The regression delegating ``build_queue`` to ``triage`` introduced.

    The statement can no longer filter letterless vacancies away, because
    «письма ещё нет» is a line the owner reads rather than a row SQL hides — so
    a refused vacancy now spends the same read budget a served one does. Ask for
    three with nine refusals in front of them and the queue must still answer
    three, not "as many as happened to fit".

    Caught twice. The first fix made the budget a bigger multiple of the limit,
    which is the wrong shape: the ratio of refusals to items is a property of
    the corpus, not of the request, and the walk corpus (93 letterless of 102)
    still answered 7 of 9. The budget is a flat ceiling over the candidate set
    now — see :data:`agent_queue.CANDIDATE_CEILING`.
    """
    for index in range(9):
        await _ready(
            db_session,
            vacancies,
            profiles,
            matches,
            external_id=f"13700004{index}",
            letter=None,
        )
    good = [
        await _ready(db_session, vacancies, profiles, matches, external_id=f"13700005{index}")
        for index in range(3)
    ]

    selection = await agent_queue.triage(db_session, limit=3)

    assert sorted(ready.vacancy_id for ready in selection.ready) == sorted(good)
    assert len(selection.set_aside) == 9


async def test_a_vacancy_hh_already_counts_an_application_on_is_not_offered(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """The brief's «negotiations.total == 0», on the number the tracker recorded.

    The agent checks it on the live page and refuses; this refuses a step
    earlier, so the owner is not asked to confirm a batch row that was going to
    be dropped a minute later. It is the third way to learn "already applied" —
    after the status column and the agent's own «skipped» — and it is the one
    that catches an application the owner sent by hand, outside this project, on
    a vacancy it is still offering.

    Not a set-aside reason but an exclusion, like the other two, because it is
    not a near miss anybody might overrule: the application exists.
    """
    vacancy_id = await _ready(db_session, vacancies, profiles, matches, external_id="137000080")
    await db_session.execute(
        update(Application)
        .where(Application.vacancy_id == vacancy_id)
        .values(hh_negotiations_total=1)
    )

    selection = await agent_queue.triage(db_session, limit=10)

    assert (selection.ready, selection.set_aside) == ([], [])


async def test_a_vacancy_nobody_scored_is_not_in_either_list(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """«Посмотреть руками» is a short list of near misses, not the corpus.

    A vacancy under the score floor, or in the ``filtered`` bucket, is not a
    vacancy the autopilot set aside — it is one that was never a candidate. Put
    them in the same list and the list stops being readable, which is the one
    thing it has to be.
    """
    await _ready(
        db_session, vacancies, profiles, matches, external_id="137000034", score=Decimal("40")
    )

    selection = await agent_queue.triage(db_session, limit=10)

    assert (selection.ready, selection.set_aside) == ([], [])


# ── the batch ─────────────────────────────────────────────────────────


async def test_the_batch_carries_every_letter_and_a_digest_per_row(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    await _ready(db_session, vacancies, profiles, matches)

    plan = await autopilot.plan(db_session)

    [row] = plan.items
    assert row.item.letter == LETTER
    assert row.card_digest == agent_queue.card_digest(row.item)
    assert row.confirmed is False


async def test_one_press_records_one_confirmation_per_vacancy_and_sends_nothing(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """The batch is N mandates, not one: each row binds its own text.

    And the thing this whole feature must never quietly acquire: confirming does
    not send. The applications are still ``sent_at IS NULL`` afterwards, and the
    only thing that changed is that the agent would now be *allowed* to send
    them.
    """
    first = await _ready(db_session, vacancies, profiles, matches, external_id="137000002")
    second = await _ready(db_session, vacancies, profiles, matches, external_id="137000003")
    plan = await autopilot.plan(db_session)
    assert {row.vacancy_id for row in plan.items} == {first, second}

    result = await autopilot.confirm(
        db_session,
        BatchConfirmRequest(
            items=[
                BatchConfirmItem(vacancy_id=row.vacancy_id, card_digest=row.card_digest)
                for row in plan.items
            ]
        ),
    )

    assert result.confirmed == 2
    assert all(outcome.confirmed for outcome in result.outcomes)
    served = await agent_queue.build_queue(db_session, limit=10)
    assert len(served.items) == 2
    for item in served.items:
        assert item.confirmation is not None
        assert item.confirmation.letter_digest == agent_queue.letter_digest(LETTER)
    sent = (await db_session.execute(select(Application.sent_at))).scalars().all()
    assert all(value is None for value in sent)


async def test_a_row_whose_card_moved_is_refused_on_its_own(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """One stale digest must not take the rest of the batch down with it."""
    good = await _ready(db_session, vacancies, profiles, matches, external_id="137000004")
    stale = await _ready(db_session, vacancies, profiles, matches, external_id="137000005")
    plan = await autopilot.plan(db_session)
    rows = {row.vacancy_id: row for row in plan.items}

    result = await autopilot.confirm(
        db_session,
        BatchConfirmRequest(
            items=[
                BatchConfirmItem(vacancy_id=good, card_digest=rows[good].card_digest),
                BatchConfirmItem(vacancy_id=stale, card_digest="0" * 64),
            ]
        ),
    )

    assert result.confirmed == 1
    refused = [outcome for outcome in result.outcomes if not outcome.confirmed]
    assert [outcome.vacancy_id for outcome in refused] == [stale]
    assert refused[0].detail is not None


async def test_the_first_batch_after_a_quiet_period_is_the_smaller_one(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """A mistake in the selection is read by employers, so the first one is small."""
    await _ready(db_session, vacancies, profiles, matches)

    plan = await autopilot.plan(db_session)

    assert plan.first_batch is True
    assert plan.limit == min(settings.agent_first_batch_limit, settings.agent_batch_limit)
    assert "перерыва" in plan.limit_reason


async def test_a_recent_send_lifts_the_ceiling_to_the_ordinary_one(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    await _ready(db_session, vacancies, profiles, matches)
    db_session.add(
        Application(
            id=uuid7(),
            vacancy_id=(await db_session.execute(select(Vacancy.id))).scalars().first(),
            sent_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    await db_session.flush()

    plan = await autopilot.plan(db_session)

    assert plan.first_batch is False
    assert plan.limit == settings.agent_batch_limit


async def test_a_batch_larger_than_the_ceiling_confirms_nothing_at_all(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
    async_client: AsyncClient,
) -> None:
    """Refused whole. Half a batch confirmed is the worst of both answers."""
    ready = [
        await _ready(db_session, vacancies, profiles, matches, external_id=f"13700001{index}")
        for index in range(settings.agent_first_batch_limit + 1)
    ]
    plan = await autopilot.plan(db_session)
    assert len(plan.items) == len(ready)

    answer = await async_client.post(
        BATCH,
        json={
            "items": [
                {"vacancy_id": str(row.vacancy_id), "card_digest": row.card_digest}
                for row in plan.items
            ]
        },
    )

    assert answer.status_code == 409
    served = await agent_queue.build_queue(db_session, limit=10)
    assert all(item.confirmation is None for item in served.items)


async def test_the_chain_re_reads_at_most_a_batch_worth_of_pages(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each id here is one polite hh request, and the chain makes this list twice.

    Uncapped it is bounded only by how many rows the triage pass read, which is
    several hundred — so a corpus where nothing has been re-read for a week
    would spend half an hour fetching pages of vacancies no batch could include.
    A batch cannot exceed ``AGENT_BATCH_LIMIT``, so reading past
    ``SELECTION_LIMIT`` cannot change what goes out.
    """
    monkeypatch.setattr(autopilot, "SELECTION_LIMIT", 3)
    for index in range(7):
        await _ready(
            db_session,
            vacancies,
            profiles,
            matches,
            external_id=f"13700006{index}",
            seen_hours_ago=settings.agent_page_freshness_hours + 1,
        )

    stale = await autopilot._stale_candidates(db_session)

    assert len(stale) == 3


# ── the chain ─────────────────────────────────────────────────────────


def _fake_steps() -> list[ChainStep]:
    return [ChainStep(key=key, title=title) for key, title in autopilot.CHAIN_STEPS]


async def test_the_chain_stops_at_the_step_that_failed_and_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []

    async def ok(step: ChainStep) -> list[str]:
        ran.append(step.key)
        return [f"{step.key}: готово"]

    async def boom(step: ChainStep) -> list[str]:
        ran.append(step.key)
        raise autopilot.ChainStepFailedError("модель эмбеддингов недоступна.")

    monkeypatch.setitem(autopilot._RUNNERS, "crawl", ok)
    monkeypatch.setitem(autopilot._RUNNERS, "embed", boom)
    monkeypatch.setattr(autopilot, "_memory", None)
    steps = _fake_steps()

    with pytest.raises(autopilot.ChainStepFailedError) as raised:
        await autopilot.run_chain(steps)

    assert ran == ["crawl", "embed"]
    assert "Эмбеддинги" in str(raised.value.detail)
    assert [step.status for step in steps[:2]] == [
        ChainStepStatus.DONE,
        ChainStepStatus.FAILED,
    ]
    assert steps[2].status is ChainStepStatus.PENDING
    autopilot.forget()


async def test_a_second_run_continues_instead_of_crawling_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twenty minutes of crawling must not be spent again to reach step two."""
    ran: list[str] = []
    fail = {"embed": True}

    async def ok(step: ChainStep) -> list[str]:
        ran.append(step.key)
        return []

    async def flaky(step: ChainStep) -> list[str]:
        ran.append(step.key)
        if fail["embed"]:
            fail["embed"] = False
            raise autopilot.ChainStepFailedError("не в этот раз.")
        return []

    for key in ("crawl", "match", "letters", "queue"):
        monkeypatch.setitem(autopilot._RUNNERS, key, ok)
    monkeypatch.setitem(autopilot._RUNNERS, "embed", flaky)
    monkeypatch.setattr(autopilot, "_memory", None)

    with pytest.raises(autopilot.ChainStepFailedError):
        await autopilot.run_chain(_fake_steps())
    ran.clear()
    second = _fake_steps()
    await autopilot.run_chain(second)

    assert ran == ["embed", "match", "letters", "queue"]
    assert second[0].status is ChainStepStatus.SKIPPED
    assert second[0].note is not None
    autopilot.forget()


async def test_an_interrupted_chain_older_than_the_window_starts_from_the_top(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Yesterday's crawl is exactly the step whose answer has gone stale."""
    ran: list[str] = []

    async def ok(step: ChainStep) -> list[str]:
        ran.append(step.key)
        return []

    for key, _ in autopilot.CHAIN_STEPS:
        monkeypatch.setitem(autopilot._RUNNERS, key, ok)
    monkeypatch.setattr(
        autopilot,
        "_memory",
        autopilot._Memory(
            finished={"crawl", "embed"},
            failed_at="match",
            at=datetime.now(UTC) - autopilot.RESUME_WINDOW - timedelta(minutes=1),
        ),
    )

    await autopilot.run_chain(_fake_steps())

    assert ran == [key for key, _ in autopilot.CHAIN_STEPS]
    autopilot.forget()


def test_the_chain_has_no_step_that_sends() -> None:
    """The line the brief draws, held where it can be read.

    The chain ends with a queue. Sending is the agent, on the owner's machine,
    after a batch confirmation — so no step of this chain may be the one that
    starts it.
    """
    assert [key for key, _ in autopilot.CHAIN_STEPS] == [
        "crawl",
        "embed",
        "match",
        "letters",
        "queue",
    ]
    assert OperationKind.SEND.value not in dict(autopilot.CHAIN_STEPS)
    assert autopilot.OperationKind.SEND.needs_agent if hasattr(autopilot, "OperationKind") else True


# ── the steps themselves ──────────────────────────────────────────────
#
# Everything above drives the chain with its runners replaced, which is the
# right way to test the walk and says nothing about the steps. These exercise
# the step bodies — the code that actually runs at three in the morning — with
# only the two things that cannot be in a test replaced: the crawl process and
# the embedding model.


def _one_session(monkeypatch: pytest.MonkeyPatch, session: AsyncSession) -> None:
    """Make the step bodies work inside the test's own transaction.

    ``_step_letters`` and ``_step_queue`` open sessions of their own — correctly,
    since a chain step is not inside anybody's request — and one opened for real
    would not see rows this test has not committed. The fixture's session joins
    its outer transaction with ``create_savepoint``, so the ``commit()`` inside a
    step releases a savepoint and is still rolled back with everything else.
    """

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield session

    monkeypatch.setattr(autopilot, "session_factory", factory)


def _job(status: PipelineJobStatus, **fields: Any) -> PipelineJobRead:
    """A crawl job in whatever state the test needs."""
    return PipelineJobRead(
        id=uuid7(),
        status=status,
        message="обход",
        queued_at=datetime.now(UTC),
        **fields,
    )


def _report(found: int = 12, new: int = 5, duplicates: int = 2) -> RunResponse:
    return RunResponse(
        started_at=datetime.now(UTC),
        plan=PlanSummary(),
        found=found,
        new=new,
        duplicates=duplicates,
    )


async def test_the_crawl_step_waits_for_a_walk_already_running_instead_of_starting_a_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Somebody pressed «Собрать вакансии» five minutes ago. That is not an error.

    A second request would be refused by the pipeline and the chain would stop
    on its first step for a reason that is not a failure. It joins instead — and
    it says which of the two it is doing, because «идёт обход» and «жду обход,
    запущенный раньше» are twenty minutes apart in what they mean for the person
    watching.
    """
    running = _job(PipelineJobStatus.RUNNING)
    finished = _job(PipelineJobStatus.SUCCESS, report=_report())
    started: list[str] = []

    async def must_not_start(*args: Any, **kwargs: Any) -> PipelineJobRead:
        started.append("request_run")
        raise AssertionError("цепочка не должна запускать второй обход")

    monkeypatch.setattr(
        autopilot.pipeline_service,
        "list_jobs",
        lambda limit=0: PipelineJobList(jobs=[running], busy=True),
    )
    monkeypatch.setattr(autopilot.pipeline_service, "request_run", must_not_start)
    monkeypatch.setattr(autopilot.pipeline_service, "read_job", lambda _: finished)
    step = ChainStep(key="crawl", title="Сбор вакансий")

    lines = await autopilot._step_crawl(step)

    assert started == []
    assert "жду обход" in (step.note or "")
    assert "Найдено: 12, новых: 5" in lines[0]


async def test_the_crawl_step_starts_one_when_nothing_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dry run is the newest job and ends at once, so «newest» is the wrong test.

    Taking it for the crawl in flight would let the chain walk on to scoring
    over a corpus nobody had collected.
    """
    dry = _job(PipelineJobStatus.RUNNING, dry_run=True)
    mine = _job(PipelineJobStatus.QUEUED)
    finished = _job(PipelineJobStatus.SUCCESS, report=_report(found=3, new=1, duplicates=0))

    async def request_run(*args: Any, **kwargs: Any) -> PipelineJobRead:
        return mine

    monkeypatch.setattr(
        autopilot.pipeline_service,
        "list_jobs",
        lambda limit=0: PipelineJobList(jobs=[dry], busy=True),
    )
    monkeypatch.setattr(autopilot.pipeline_service, "request_run", request_run)
    monkeypatch.setattr(autopilot.pipeline_service, "read_job", lambda _: finished)
    step = ChainStep(key="crawl", title="Сбор вакансий")

    lines = await autopilot._step_crawl(step)

    # The note that distinguishes "I started this one" from "I am waiting for
    # yours". Asserted on «идёт обход» rather than on the duration beside it,
    # which is a measurement and will move again.
    assert "идёт обход" in (step.note or "")
    assert "новых: 1" in lines[0]


async def test_a_crawl_that_failed_stops_the_chain_with_the_crawl_s_own_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not «шаг не удался»: the reason hh gave is the only actionable thing here."""
    failed = _job(PipelineJobStatus.FAILED, error="hh показал проверку на робота.")
    monkeypatch.setattr(
        autopilot.pipeline_service,
        "list_jobs",
        lambda limit=0: PipelineJobList(jobs=[_job(PipelineJobStatus.RUNNING)], busy=True),
    )
    monkeypatch.setattr(autopilot.pipeline_service, "read_job", lambda _: failed)

    with pytest.raises(autopilot.ChainStepFailedError) as raised:
        await autopilot._step_crawl(ChainStep(key="crawl", title="Сбор вакансий"))

    assert "проверку на робота" in (raised.value.detail or "")


async def test_the_embedding_step_reports_both_halves_and_what_is_left(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Descriptions and titles are two passes and two numbers.

    Reported separately because they fail separately: the live run of 18 Sep
    2026 had titles drained and descriptions starved, and one combined «0» said
    nothing about either.
    """
    descriptions = iter(
        [
            EmbeddingOutcome(considered=10, unchanged=0, embedded=10, stopped="budget"),
            EmbeddingOutcome(considered=4, unchanged=0, embedded=4, stopped="drained"),
        ]
    )

    async def embed(_: Any) -> EmbeddingOutcome:
        return next(descriptions)

    async def titles(_: Any) -> EmbeddingOutcome:
        return EmbeddingOutcome(considered=2, unchanged=0, embedded=2, stopped="drained")

    monkeypatch.setattr(autopilot, "embed_pending", embed)
    monkeypatch.setattr(autopilot, "embed_pending_titles", titles)
    _one_session(monkeypatch, db_session)
    step = ChainStep(key="embed", title="Эмбеддинги")

    lines = await autopilot._step_embed(step)

    assert "описаний 14, названий 2" in lines[0]
    assert "всё посчитано" in lines[1]


async def test_the_embedding_step_stops_the_chain_when_the_model_is_not_installed(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scoring after this would write similarity-less matches over good ones."""

    async def unavailable(_: Any) -> EmbeddingOutcome:
        return EmbeddingOutcome(considered=0, unchanged=0, embedded=0, stopped="unavailable")

    monkeypatch.setattr(autopilot, "embed_pending", unavailable)
    _one_session(monkeypatch, db_session)

    with pytest.raises(autopilot.ChainStepFailedError) as raised:
        await autopilot._step_embed(ChainStep(key="embed", title="Эмбеддинги"))

    assert "uv sync --extra embeddings" in (raised.value.detail or "")


async def test_the_queue_step_counts_what_passed_and_groups_what_did_not(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chain's last step is a measurement, and this is the measurement.

    Against the real selection on a real database, with only the page re-read
    replaced — that one is hh over the network and has its own file. What it
    prints is what a scheduled run leaves behind at 03:50, so the numbers have
    to be the ones the batch screen will show at breakfast.
    """
    await _ready(db_session, vacancies, profiles, matches, external_id="137000070")
    await _ready(
        db_session,
        vacancies,
        profiles,
        matches,
        external_id="137000071",
        experience_gap_years=Decimal(str(settings.agent_max_experience_gap_years)) + 1,
    )
    await _ready(db_session, vacancies, profiles, matches, external_id="137000072", letter=None)

    async def nothing_to_re_read(*args: Any, **kwargs: Any) -> freshness.RefreshOutcome:
        return freshness.RefreshOutcome()

    monkeypatch.setattr(autopilot.freshness, "refresh", nothing_to_re_read)
    _one_session(monkeypatch, db_session)

    lines = await autopilot._step_queue(ChainStep(key="queue", title="Очередь"))

    assert "Готово к отправке: 1." in lines[0]
    assert "В «посмотреть руками»: 2." in lines[0]
    assert any("просят больше опыта: 1" in line for line in lines)
    assert any("письма ещё нет: 1" in line for line in lines)
    # The line that says the chain did not do the one thing it must not do.
    assert lines[-1].startswith("Ничего не отправлено")


async def test_the_letters_step_re_reads_first_and_writes_only_for_what_passed(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The order inside this step is the whole of its design.

    Re-reading first means no letter is written for a posting archived a week
    ago — on 17 Sep 2026 that was the entire corpus, nine days stale — and a
    vacancy the selection refuses for any other reason never reaches the model
    at all, which is the cheapest place to not spend an LLM call.
    """
    wanted = await _ready(
        db_session, vacancies, profiles, matches, external_id="137000073", letter=None
    )
    refused = await _ready(
        db_session,
        vacancies,
        profiles,
        matches,
        external_id="137000074",
        letter=None,
        experience_gap_years=Decimal(str(settings.agent_max_experience_gap_years)) + 1,
    )
    order: list[str] = []
    written: list[UUID] = []

    async def refresh(*args: Any, **kwargs: Any) -> freshness.RefreshOutcome:
        order.append("refresh")
        return freshness.RefreshOutcome(checked=2, open_for_applications=2)

    async def write_letter(_: Any, vacancy_id: UUID, *args: Any, **kwargs: Any) -> Any:
        order.append("write")
        written.append(vacancy_id)
        return LetterOutcome(vacancy_id=vacancy_id, title="t", company=None, saved=True)

    monkeypatch.setattr(autopilot.freshness, "refresh", refresh)
    monkeypatch.setattr(autopilot.letters_service, "write_letter", write_letter)
    _one_session(monkeypatch, db_session)
    step = ChainStep(key="letters", title="Письма")

    lines = await autopilot._step_letters(step)

    assert order[0] == "refresh"
    assert written == [wanted]
    assert refused not in written
    assert any("Написано писем: 1 из 1" in line for line in lines)


async def test_the_letters_step_says_so_when_everything_that_passed_already_has_one(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«Написано писем: 0» and «писать не для чего» are different facts."""
    await _ready(db_session, vacancies, profiles, matches, external_id="137000075")

    async def refresh(*args: Any, **kwargs: Any) -> freshness.RefreshOutcome:
        return freshness.RefreshOutcome(checked=1, open_for_applications=1)

    async def must_not_write(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("письмо уже есть — модель звать не за чем")

    monkeypatch.setattr(autopilot.freshness, "refresh", refresh)
    monkeypatch.setattr(autopilot.letters_service, "write_letter", must_not_write)
    _one_session(monkeypatch, db_session)

    lines = await autopilot._step_letters(ChainStep(key="letters", title="Письма"))

    assert any("у всех прошедших отбор письма уже есть" in line for line in lines)


async def test_the_scoring_step_says_what_is_still_missing_a_vector(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """666 vacancies with no description vector, reported as a number rather than
    discovered later as a queue that is mysteriously short."""

    async def score(*args: Any, **kwargs: Any) -> ScoringOutcome:
        return ScoringOutcome(considered=700, written=34, without_embedding=666)

    async def ensure(*args: Any, **kwargs: Any) -> str | None:
        return None

    monkeypatch.setattr(autopilot, "score_corpus", score)
    monkeypatch.setattr(autopilot.profile_vectors, "ensure_profile_embedding", ensure)
    _one_session(monkeypatch, db_session)

    lines = await autopilot._step_match(ChainStep(key="match", title="Подбор"))

    assert lines[0] == "Рассмотрено вакансий: 700, записано оценок: 34."
    assert "Без вектора описания осталось: 666." in lines[1]


async def test_scoring_without_a_parsed_resume_stops_the_chain_and_says_where_to_go(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse(*args: Any, **kwargs: Any) -> ScoringOutcome:
        raise ProfileNotReadyError("Активного резюме нет.")

    async def ensure(*args: Any, **kwargs: Any) -> str | None:
        return None

    monkeypatch.setattr(autopilot, "score_corpus", refuse)
    monkeypatch.setattr(autopilot.profile_vectors, "ensure_profile_embedding", ensure)
    _one_session(monkeypatch, db_session)

    with pytest.raises(autopilot.ChainStepFailedError) as raised:
        await autopilot._step_match(ChainStep(key="match", title="Подбор"))

    assert "резюме" in (raised.value.detail or "")


# ── the schedule ──────────────────────────────────────────────────────


def test_the_next_run_is_tomorrow_when_today_is_already_past() -> None:
    at = time(3, 30)

    assert autopilot.next_run(datetime(2026, 9, 17, 3, 0), at) == datetime(2026, 9, 17, 3, 30)
    assert autopilot.next_run(datetime(2026, 9, 17, 4, 0), at) == datetime(2026, 9, 18, 3, 30)
    # Exactly on the hour waits for tomorrow rather than firing at boot.
    assert autopilot.next_run(datetime(2026, 9, 17, 3, 30), at) == datetime(2026, 9, 18, 3, 30)


async def test_the_schedule_starts_the_chain_and_only_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clock cannot read a letter, so a clock may not cause an application."""
    started: list[str] = []
    waited: list[float] = []

    async def start() -> None:
        started.append("chain")

    async def sleep(seconds: float) -> None:
        waited.append(seconds)

    monkeypatch.setattr(settings, "autopilot_daily_at", "03:30")
    await autopilot.schedule_daily(
        start, sleep=sleep, now=lambda: datetime(2026, 9, 17, 3, 0), rounds=2
    )

    assert started == ["chain", "chain"]
    assert waited == [1800.0, 1800.0]


async def test_no_schedule_means_no_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []

    async def start() -> None:  # pragma: no cover - the point is that it is not called
        started.append("chain")

    monkeypatch.setattr(settings, "autopilot_daily_at", None)
    await autopilot.schedule_daily(start, rounds=1)

    assert started == []


async def test_a_chain_that_fails_does_not_end_the_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Today's hh challenge must not cancel tomorrow's collection."""
    tries = 0

    async def start() -> None:
        nonlocal tries
        tries += 1
        raise RuntimeError("hh показал проверку на робота")

    async def sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(settings, "autopilot_daily_at", "03:30")
    await autopilot.schedule_daily(
        start, sleep=sleep, now=lambda: datetime(2026, 9, 17, 3, 0), rounds=3
    )

    assert tries == 3


def test_nothing_in_the_autopilot_offers_a_way_to_skip_the_confirmation() -> None:
    """The brief's hard line, read out of the source rather than asserted about it.

    No ``--yes``, no ``--unattended``, no setting. It reads the module's code —
    every string it could pass to anything, and every name it defines — because
    the thing being prevented is somebody adding exactly such a switch in a
    hurry. Docstrings are skipped on purpose: this file's own prose says the
    words in order to forbid them, and a test that could not survive being
    explained would be a test nobody may write about.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path(autopilot.__file__).read_text(encoding="utf-8"))
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    }
    written = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    ]
    written += [node.id for node in ast.walk(tree) if isinstance(node, ast.Name)]
    written += [node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)]
    for forbidden in ("--yes", "--unattended", "auto_send", "skip_confirmation", "unattended"):
        assert not any(forbidden in text for text in written), forbidden
    assert not any(name.endswith("auto_send") for name in dir(settings))


def test_the_batch_ceiling_and_the_quiet_period_are_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both numbers move from ``.env``; neither is spelled into the code."""
    monkeypatch.setattr(settings, "agent_batch_limit", 7)
    monkeypatch.setattr(settings, "agent_first_batch_limit", 2)
    monkeypatch.setattr(settings, "agent_quiet_period_days", 3)

    recent, ordinary_reason, first = autopilot._ceiling(datetime.now(UTC) - timedelta(days=1))
    quiet, quiet_reason, quiet_first = autopilot._ceiling(datetime.now(UTC) - timedelta(days=10))

    assert (recent, first) == (7, False)
    assert "7" in ordinary_reason
    assert (quiet, quiet_first) == (2, True)
    assert "2" in quiet_reason


def test_an_unconfirmed_vacancy_id_cannot_be_smuggled_into_a_batch() -> None:
    """The request is a list of (vacancy, digest) pairs and cannot be anything else."""
    with pytest.raises(ValueError):
        BatchConfirmRequest(items=[BatchConfirmItem(vacancy_id=uuid4(), card_digest="short")])


async def test_confirmations_already_standing_count_towards_the_ceiling(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """A ceiling on one press is not a ceiling: three presses of three make nine.

    So the limit is counted over everything that would be outstanding after the
    press, not over the press alone.
    """
    limit = min(settings.agent_first_batch_limit, settings.agent_batch_limit)
    ids = [
        await _ready(db_session, vacancies, profiles, matches, external_id=f"13700002{index}")
        for index in range(limit + 1)
    ]
    plan = await autopilot.plan(db_session)
    rows = {row.vacancy_id: row for row in plan.items}

    first = await autopilot.confirm(
        db_session,
        BatchConfirmRequest(
            items=[
                BatchConfirmItem(vacancy_id=one, card_digest=rows[one].card_digest)
                for one in ids[:limit]
            ]
        ),
    )
    assert first.confirmed == limit

    with pytest.raises(autopilot.BatchRefusedError) as refused:
        await autopilot.confirm(
            db_session,
            BatchConfirmRequest(
                items=[
                    BatchConfirmItem(
                        vacancy_id=ids[limit], card_digest=rows[ids[limit]].card_digest
                    )
                ]
            ),
        )

    assert "Уже подтверждено" in (refused.value.detail or "")
    served = await agent_queue.build_queue(db_session, limit=10)
    assert sum(1 for item in served.items if item.confirmation is not None) == limit


@pytest.mark.parametrize(
    ("stopped", "expected"),
    [
        ("drained", "всё посчитано"),
        ("budget", "следующий запуск продолжит"),
        ("starved", "выборка не отдаёт"),
        ("unavailable", "недоступна"),
    ],
)
def test_the_embedding_step_says_why_it_wrote_nothing(stopped: str, expected: str) -> None:
    """Zero written means four different things, and one of them is a defect.

    Measured on the live run of 18 Sep 2026: the step reported «посчитано 0» and
    the scoring step right after it reported 666 vacancies with no description
    vector. The number alone read as "nothing to do".
    """
    from app.pipeline.embedding import EmbeddingOutcome

    outcome = EmbeddingOutcome(considered=0, unchanged=0, embedded=0, stopped=stopped)

    assert expected in autopilot._left(outcome)
