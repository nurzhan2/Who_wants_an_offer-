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
from app.db.models import Application, CandidateProfile, Vacancy
from app.db.repositories.match import MatchRepository
from app.db.repositories.profile import ProfileRepository
from app.db.repositories.vacancy import VacancyRepository
from app.schemas.autopilot import BatchConfirmItem, BatchConfirmRequest, SetAsideKind
from app.schemas.operations import ChainStep, ChainStepStatus, OperationKind
from app.services import agent_queue, autopilot
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
    await matches.bulk_upsert(
        [make_match(profile_id, result.vacancy_id, Decimal("91"), **match_fields)]
    )
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
    assert str(settings.agent_max_experience_gap_years) in aside.reason.replace(".0", "")
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
