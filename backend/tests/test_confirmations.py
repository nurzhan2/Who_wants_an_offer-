"""Confirming an application in the dashboard.

The invariant the whole feature hangs on: **without the owner's confirmation of
exactly this card, the agent is never handed a confirmed application.** Each
test below attacks one way it could be lost:

* nothing is confirmed until the owner posts the digest of the card they read;
* a digest of any other card is refused, and so is a card that changed after
  it was read — a regenerated letter, a rescore;
* a letter regenerated after confirming voids the confirmation in the queue;
* a confirmation expires, and the first result the agent reports spends it;
* a vacancy the agent must not touch — archived, filtered, below the floor,
  already sent, from another source — cannot be confirmed at all, and the card
  says why in words.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import uuid7
from app.db.enums import MatchBucket
from app.db.models import Application, CandidateProfile, Vacancy
from app.db.repositories.match import MatchRepository
from app.db.repositories.profile import ProfileRepository
from app.db.repositories.vacancy import VacancyRepository
from app.schemas.agent import AgentStatus, ApplicationResult
from app.services import agent_queue, confirmations
from factories import make_match, make_profile, make_vacancy

pytestmark = pytest.mark.db

HH_ID = "136555567"
HH_URL = f"https://almaty.hh.kz/vacancy/{HH_ID}"
LETTER = "Здравствуйте! Пишу по вакансии Python-разработчика."
CARDS = f"{settings.api_v1_prefix}/tracker/confirmations"
QUEUE = f"{settings.api_v1_prefix}/applications/queue"
TOKEN = "confirmations-test-token"


async def _ready(
    session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
    *,
    score: str = "91",
    bucket: MatchBucket | None = None,
    slug: str = "hh",
    external_id: str = HH_ID,
    url: str = HH_URL,
    letter: str | None = LETTER,
    **application: Any,
) -> UUID:
    """A vacancy the agent would be handed: hh, scored above the floor, a letter."""
    profile = await profiles.create(make_profile())
    await session.execute(
        update(CandidateProfile).where(CandidateProfile.id == profile.id).values(is_active=True)
    )
    result = await vacancies.upsert_by_external_id(
        make_vacancy(f"confirm-{external_id}-{slug}"),
        source_slug=slug,
        external_id=external_id,
        url=url,
        raw={"_derived": {"external_id": external_id, "url": url, "key_skills": ["Python"]}},
    )
    match = make_match(profile.id, result.vacancy_id, Decimal(score))
    if bucket is not None:
        match = match.model_copy(update={"bucket": bucket})
    await matches.bulk_upsert([match])
    await session.flush()
    if letter is not None or application:
        session.add(
            Application(
                id=uuid7(), vacancy_id=result.vacancy_id, cover_letter=letter, **application
            )
        )
    await session.flush()
    return result.vacancy_id


async def _served(session: AsyncSession) -> list[Any]:
    return list((await agent_queue.build_queue(session, limit=10)).items)


async def test_nothing_is_confirmed_until_the_owner_posts_the_card_they_read(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
    async_client: AsyncClient,
) -> None:
    vacancy_id = await _ready(db_session, vacancies, profiles, matches)

    [item] = await _served(db_session)
    assert item.confirmation is None

    card = (await async_client.get(f"{CARDS}/{vacancy_id}")).json()
    assert card["blockers"] == []
    assert card["item"]["letter"] == LETTER
    assert card["item"]["score_explanation"] is None or isinstance(
        card["item"]["score_explanation"], str
    )
    assert card["url"] == HH_URL
    assert card["state"] is None
    # Reading the card is not consent.
    [item] = await _served(db_session)
    assert item.confirmation is None

    confirmed = await async_client.post(
        f"{CARDS}/{vacancy_id}", json={"card_digest": card["card_digest"]}
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["state"]["valid"] is True

    [item] = await _served(db_session)
    assert item.confirmation is not None
    assert item.confirmation.card_digest == card["card_digest"]
    assert item.confirmation.letter_digest == agent_queue.letter_digest(LETTER)
    assert agent_queue.card_digest(item) == card["card_digest"]

    listed = (await async_client.get(CARDS)).json()
    assert [row["external_id"] for row in listed["items"]] == [HH_ID]


async def test_a_digest_of_another_card_is_refused(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
    async_client: AsyncClient,
) -> None:
    vacancy_id = await _ready(db_session, vacancies, profiles, matches)

    refused = await async_client.post(f"{CARDS}/{vacancy_id}", json={"card_digest": "0" * 64})

    assert refused.status_code == 409
    assert "Карточка изменилась" in refused.json()["detail"]
    [item] = await _served(db_session)
    assert item.confirmation is None


async def test_a_letter_rewritten_while_the_card_was_open_is_not_confirmed(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    vacancy_id = await _ready(db_session, vacancies, profiles, matches)
    read = await confirmations.card(db_session, vacancy_id)
    assert read.card_digest is not None

    await db_session.execute(
        update(Application)
        .where(Application.vacancy_id == vacancy_id)
        .values(cover_letter="Совсем другое письмо.")
    )

    with pytest.raises(confirmations.ConfirmationRefusedError, match="изменилась"):
        await confirmations.confirm(db_session, vacancy_id, read.card_digest)


async def test_a_letter_rewritten_after_confirming_voids_the_confirmation(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    vacancy_id = await _ready(db_session, vacancies, profiles, matches)
    read = await confirmations.card(db_session, vacancy_id)
    assert read.card_digest is not None
    await confirmations.confirm(db_session, vacancy_id, read.card_digest)

    await db_session.execute(
        update(Application)
        .where(Application.vacancy_id == vacancy_id)
        .values(cover_letter="Перегенерированное письмо.")
    )

    [item] = await _served(db_session)
    assert item.confirmation is None
    state = (await confirmations.card(db_session, vacancy_id)).state
    assert state is not None
    assert state.valid is False
    assert "подтвердите заново" in (state.reason or "")


async def test_a_posting_that_changed_after_confirming_voids_it_too(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """The title, like the score and its reasons, is on the card that was agreed to."""
    vacancy_id = await _ready(db_session, vacancies, profiles, matches)
    read = await confirmations.card(db_session, vacancy_id)
    assert read.card_digest is not None
    await confirmations.confirm(db_session, vacancy_id, read.card_digest)

    await db_session.execute(
        update(Vacancy).where(Vacancy.id == vacancy_id).values(title="Другая должность")
    )

    [item] = await _served(db_session)
    assert item.confirmation is None


async def test_a_rescore_after_confirming_voids_it(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """The score is on the card: a different one is a different card."""
    from app.db.models import Match

    vacancy_id = await _ready(db_session, vacancies, profiles, matches)
    read = await confirmations.card(db_session, vacancy_id)
    assert read.card_digest is not None
    await confirmations.confirm(db_session, vacancy_id, read.card_digest)

    await db_session.execute(
        update(Match).where(Match.vacancy_id == vacancy_id).values(score=Decimal("95"))
    )

    [item] = await _served(db_session)
    assert item.confirmation is None


async def test_a_confirmation_expires(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    vacancy_id = await _ready(db_session, vacancies, profiles, matches)
    read = await confirmations.card(db_session, vacancy_id)
    assert read.card_digest is not None
    await confirmations.confirm(db_session, vacancy_id, read.card_digest)

    old = datetime.now(UTC) - timedelta(hours=settings.agent_confirmation_ttl_hours + 1)
    await db_session.execute(
        update(Application).where(Application.vacancy_id == vacancy_id).values(confirmed_at=old)
    )

    [item] = await _served(db_session)
    assert item.confirmation is None
    state = (await confirmations.card(db_session, vacancy_id)).state
    assert state is not None
    assert f"старше {settings.agent_confirmation_ttl_hours} ч" in (state.reason or "")
    assert (await confirmations.confirmed(db_session)).no_longer_valid == 1


@pytest.mark.parametrize(
    "status",
    [AgentStatus.SENT, AgentStatus.NEEDS_MANUAL, AgentStatus.FAILED, AgentStatus.SKIPPED],
)
async def test_the_first_result_spends_the_confirmation(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
    status: AgentStatus,
) -> None:
    vacancy_id = await _ready(db_session, vacancies, profiles, matches)
    read = await confirmations.card(db_session, vacancy_id)
    assert read.card_digest is not None
    await confirmations.confirm(db_session, vacancy_id, read.card_digest)

    await agent_queue.record_results(
        db_session, [ApplicationResult(vacancy_id=HH_ID, status=status, negotiations_total=1)]
    )

    rows = (
        await db_session.scalars(select(Application).where(Application.vacancy_id == vacancy_id))
    ).all()
    assert all(row.confirmed_at is None for row in rows)
    assert all(row.confirmed_card_digest is None for row in rows)


async def test_a_confirmation_can_be_withdrawn(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
    async_client: AsyncClient,
) -> None:
    vacancy_id = await _ready(db_session, vacancies, profiles, matches)
    card = (await async_client.get(f"{CARDS}/{vacancy_id}")).json()
    await async_client.post(f"{CARDS}/{vacancy_id}", json={"card_digest": card["card_digest"]})

    withdrawn = await async_client.delete(f"{CARDS}/{vacancy_id}")

    assert withdrawn.json()["state"] is None
    [item] = await _served(db_session)
    assert item.confirmation is None


async def test_the_agent_receives_the_confirmation_over_its_own_seam(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
    async_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "agent_api_token", SecretStr(TOKEN))
    vacancy_id = await _ready(db_session, vacancies, profiles, matches)
    card = (await async_client.get(f"{CARDS}/{vacancy_id}")).json()
    await async_client.post(f"{CARDS}/{vacancy_id}", json={"card_digest": card["card_digest"]})

    queue = await async_client.get(QUEUE, headers={"Authorization": f"Bearer {TOKEN}"})

    [item] = queue.json()["items"]
    assert item["confirmation"]["card_digest"] == card["card_digest"]
    assert item["letter"] == LETTER


@pytest.mark.parametrize(
    ("setup", "said"),
    [
        ({"score": "40"}, "ниже порога"),
        ({"bucket": MatchBucket.FILTERED}, "отсеял"),
        ({"letter": None, "agent_status": "queued"}, "Письма ещё нет"),
        ({"sent_at": datetime(2026, 9, 16, tzinfo=UTC)}, "уже отправлен"),
        (
            {"agent_status": "skipped", "agent_reason": "вакансия в архиве"},
            "вакансия в архиве",
        ),
        ({"slug": "remotive", "url": "https://remotive.com/jobs/1"}, "только на hh"),
        ({"external_id": "hh-dev-001", "url": "https://example.test/hh/dev-001"}, "тестовая"),
    ],
)
async def test_a_vacancy_the_agent_must_not_touch_cannot_be_confirmed(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
    setup: dict[str, Any],
    said: str,
) -> None:
    vacancy_id = await _ready(db_session, vacancies, profiles, matches, **setup)

    card = await confirmations.card(db_session, vacancy_id)

    assert card.card_digest is None
    assert any(said in blocker for blocker in card.blockers), card.blockers
    with pytest.raises(confirmations.ConfirmationRefusedError):
        await confirmations.confirm(db_session, vacancy_id, "0" * 64)


async def test_an_archived_vacancy_cannot_be_confirmed(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    vacancy_id = await _ready(db_session, vacancies, profiles, matches)
    await db_session.execute(
        update(Vacancy).where(Vacancy.id == vacancy_id).values(is_active=False)
    )

    card = await confirmations.card(db_session, vacancy_id)

    assert card.is_active is False
    assert any("в архиве" in blocker for blocker in card.blockers)


async def test_an_unknown_vacancy_is_a_404(async_client: AsyncClient) -> None:
    response = await async_client.get(f"{CARDS}/{UUID(int=7)}")
    assert response.status_code == 404


async def test_the_card_carries_the_resume_notice_while_it_stands(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    notice = "Чтобы откликнуться, поменяйте видимость резюме на «Видно всем работодателям»"
    vacancy_id = await _ready(db_session, vacancies, profiles, matches)
    other = await vacancies.upsert_by_external_id(
        make_vacancy("confirm-other"),
        source_slug="hh",
        external_id="136000001",
        url="https://almaty.hh.kz/vacancy/136000001",
        raw={},
    )
    db_session.add(
        Application(
            id=uuid7(),
            vacancy_id=other.vacancy_id,
            sent_at=datetime(2026, 9, 16, tzinfo=UTC),
            hh_warning=notice,
        )
    )
    await db_session.flush()

    card = await confirmations.card(db_session, vacancy_id)

    assert card.resume_notice == notice


async def test_starting_a_confirmation_takes_json_only(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
    async_client: AsyncClient,
) -> None:
    """A form post from another origin carries no JSON and gets no preflight."""
    vacancy_id = await _ready(db_session, vacancies, profiles, matches)
    card = (await async_client.get(f"{CARDS}/{vacancy_id}")).json()

    form = await async_client.post(
        f"{CARDS}/{vacancy_id}",
        content=f"card_digest={card['card_digest']}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert form.status_code == 422
    [item] = await _served(db_session)
    assert item.confirmation is None


@pytest.mark.unit
def test_every_confirmation_field_the_agent_reads_is_one_this_backend_serves() -> None:
    """A renamed key would read as "not confirmed" on the agent's side, silently."""
    from test_agent_queue import AGENT_QUEUE, _keys_read_from_payload

    from app.schemas.agent import DashboardConfirmation

    read = _keys_read_from_payload(
        AGENT_QUEUE.read_text(encoding="utf-8"), "DashboardConfirmation", "from_json"
    )

    assert read == set(DashboardConfirmation.model_fields)
