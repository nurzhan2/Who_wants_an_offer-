"""hh's resume-visibility sentence reaches every screen, and leaves on evidence.

The first real ``--send`` (2026-09-16) sent four applications and hh showed the
same sentence on all four. It is about the resume, not about any of those
vacancies, so the dashboard has to carry it before the next send rather than
during it — and has to say when the sends stopped carrying it.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ApplicationStatus
from app.db.models import Application
from app.db.repositories.vacancy import VacancyRepository
from app.services import notices
from factories import make_upsert_item

pytestmark = pytest.mark.db

MEASURED = (
    "Чтобы откликнуться на эту вакансию, поменяйте видимость резюме на «Видно всем "
    "работодателям, зарегистрированным на headhunter.com.kz»"
)
REJECTION = "Такой отклик может получить отказ\nАнглийский язык — B2"
MORNING = datetime(2026, 9, 16, 12, 49, tzinfo=UTC)


async def _vacancy(vacancies: VacancyRepository, seed: str) -> UUID:
    return (await vacancies.bulk_upsert([make_upsert_item(seed)])).vacancy_ids[0]


async def _sent(db_session: AsyncSession, vacancy_id: UUID, **columns: Any) -> None:
    db_session.add(Application(vacancy_id=vacancy_id, status=ApplicationStatus.APPLIED, **columns))
    await db_session.flush()


def test_the_line_is_found_inside_everything_hh_said() -> None:
    """The agent stores both of hh's lines in one column, one per line."""
    assert notices.quoted_visibility(f"{REJECTION}\n{MEASURED}") == MEASURED
    assert notices.quoted_visibility(REJECTION) is None
    assert notices.quoted_visibility(None) is None
    assert notices.quoted_visibility("Поменяйте Видимость Резюме") is not None


async def test_nothing_is_shown_when_no_send_carried_it(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    vacancy_id = await _vacancy(vacancies, "notice-none")
    await _sent(db_session, vacancy_id, sent_at=MORNING, hh_warning=REJECTION)
    # A row that was never sent is not evidence about the send form.
    db_session.add(Application(vacancy_id=vacancy_id, hh_warning=MEASURED))
    await db_session.flush()

    assert (await notices.build(db_session)).items == []


async def test_the_measured_run_raises_the_notice_with_hh_words(
    db_session: AsyncSession, vacancies: VacancyRepository, async_client: AsyncClient
) -> None:
    for index in range(4):
        vacancy_id = await _vacancy(vacancies, f"notice-{index}")
        await _sent(
            db_session,
            vacancy_id,
            sent_at=MORNING + timedelta(minutes=index),
            hh_warning=f"{MEASURED}\n{REJECTION}" if index == 2 else MEASURED,
        )

    body = (await async_client.get("/api/v1/notices")).json()

    [notice] = body["items"]
    assert notice["kind"] == "resume_visibility"
    assert notice["quote"] == MEASURED
    assert notice["resolved"] is False
    assert notice["applications"] == 4
    assert "все отклики" in notice["body"]
    assert notice["first_seen_at"].startswith("2026-09-16T12:49")


async def test_a_newer_send_without_the_line_says_it_may_be_fixed(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    older = await _vacancy(vacancies, "notice-old")
    newer = await _vacancy(vacancies, "notice-new")
    await _sent(db_session, older, sent_at=MORNING, hh_blocking_warning=MEASURED)
    await _sent(db_session, newer, sent_at=MORNING + timedelta(days=1), hh_warning=None)

    [notice] = (await notices.build(db_session)).items

    assert notice.resolved is True
    assert notice.quote == MEASURED
    assert "уже исправлена" in notice.title
