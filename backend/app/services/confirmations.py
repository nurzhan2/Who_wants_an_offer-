"""Confirming an application in the dashboard, without weakening what that means.

Until 2026-09-16 an application could be confirmed only in a terminal: ``wwao
apply --send`` printed a card and waited for a word typed in full. The task
moved the confirmation into the browser, and the properties it had to keep are
the reason this module is shaped the way it is.

**The card is the queue item.** What the modal shows is built by
``app.services.agent_queue.build_queue`` for this one vacancy — the same letter,
the same score and explanation, the same ATS summary, the same hh lines the
agent will receive — so there is no second rendering that could drift from the
payload. The owner is confirming the thing the agent will send, not a picture
of it.

**Consent binds content, not a vacancy.** The card has a digest
(:func:`app.services.agent_queue.card_digest`). Opening the modal returns it;
confirming sends it back; this module recomputes the card and refuses when the
two differ — the letter was regenerated, the vacancy was rescored, hh said
something new — so a "yes" given to one text cannot confirm another. The queue
checks the digest again before serving the confirmation, and the agent binds
its mandate to it and compares the letter's own digest before typing.

**One confirmation, one attempt, limited in time.** The first result the agent
reports for the vacancy clears it, whatever the result was, and it stops being
served after ``AGENT_CONFIRMATION_TTL_HOURS``. A third of the first real queue
was archived by the time the agent reached it; a "yes" from last week was given
about a page that may be gone.

**Nothing here sends.** The API cannot: it has no browser and no hh session,
and must not have them. The agent sends, on the owner's machine, and it still
re-reads the vacancy page and refuses when hh already counts an application.

**Only hh.** The agent can drive one site. For any other source the card says
so and links to the original posting instead of offering a confirmation.

Requests that change something take a JSON body. A page on another origin
cannot send one without a CORS preflight, which this API answers only for the
dashboard, so a stray tab cannot confirm on the owner's behalf.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from uuid import UUID

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.db.enums import ApplicationStatus, MatchBucket
from app.db.models import Application, Match, Vacancy, VacancySource
from app.schemas.agent import QueueItem
from app.schemas.confirmations import (
    ConfirmationCard,
    ConfirmationState,
    ConfirmedList,
    ConfirmedVacancy,
)
from app.schemas.dashboard import send_confirmed
from app.services import agent_queue, notices

logger = get_logger(__name__)


class ConfirmationRefusedError(AppError):
    """The card cannot be confirmed as it stands, and the detail says why."""

    status_code = HTTPStatus.CONFLICT
    title = "Confirmation refused"
    problem_type = "confirmation-refused"


class VacancyNotFoundError(AppError):
    """No such vacancy."""

    status_code = HTTPStatus.NOT_FOUND
    title = "Vacancy not found"
    problem_type = "vacancy-not-found"


CHANGED: str = (
    "Карточка изменилась, пока была открыта: письмо, оценка или предупреждения hh "
    "уже другие. Перечитайте её и подтвердите заново."
)


async def card(session: AsyncSession, vacancy_id: UUID) -> ConfirmationCard:
    """What the confirmation modal shows, with the digest to confirm it by."""
    vacancy = await session.get(Vacancy, vacancy_id)
    if vacancy is None:
        raise VacancyNotFoundError("Вакансия не найдена.")
    sources = (
        await session.execute(
            select(VacancySource.source_slug, VacancySource.url, VacancySource.external_id)
            .where(VacancySource.vacancy_id == vacancy_id)
            .order_by(VacancySource.created_at, VacancySource.id)
        )
    ).all()
    agent_source = next(
        (row for row in sources if row.source_slug == settings.agent_source_slug), None
    )
    shown = agent_source if agent_source is not None else (sources[0] if sources else None)
    visibility = await notices.build(session)
    resume_notice = next(
        (item.quote for item in visibility.items if not item.resolved and item.quote), None
    )
    base = ConfirmationCard(
        vacancy_id=vacancy_id,
        title=vacancy.title,
        company=vacancy.company,
        source=shown.source_slug if shown is not None else None,
        url=shown.url if shown is not None else None,
        external_id=agent_source.external_id if agent_source else None,
        published_at=vacancy.published_at,
        last_seen_at=vacancy.last_seen_at,
        is_active=vacancy.is_active,
        resume_notice=resume_notice,
        ttl_hours=settings.agent_confirmation_ttl_hours,
    )
    if agent_source is None:
        return base.model_copy(
            update={
                "blockers": [
                    f"Агент умеет откликаться только на {settings.agent_source_slug}. "
                    "Откликнитесь на странице вакансии — ссылка ниже."
                ]
            }
        )

    blockers = await _blockers(session, vacancy)
    item = None
    if not blockers:
        served = await agent_queue.build_queue(
            session,
            limit=1,
            min_score=_floor(),
            require_letter=True,
            vacancy_id=vacancy_id,
        )
        item = served.items[0] if served.items else None
        if item is None:
            blockers = ["Агент не получит эту вакансию: её ссылка на hh не совпадает с её номером."]
    state = await _state(session, vacancy_id, item)
    return base.model_copy(
        update={
            "blockers": blockers,
            "item": item,
            "card_digest": agent_queue.card_digest(item) if item is not None else None,
            "state": state,
        }
    )


async def confirm(session: AsyncSession, vacancy_id: UUID, digest: str) -> ConfirmationCard:
    """Record the owner's "yes" to exactly the card they read, or refuse."""
    current = await card(session, vacancy_id)
    if current.blockers or current.item is None:
        raise ConfirmationRefusedError(
            " ".join(current.blockers) or "Эту вакансию сейчас нельзя подтвердить."
        )
    if current.card_digest != digest:
        raise ConfirmationRefusedError(CHANGED)
    row = await _letter_row(session, vacancy_id)
    if row is None:  # pragma: no cover - the card required a letter a moment ago
        raise ConfirmationRefusedError("Письма для этой вакансии больше нет.")
    row.confirmed_at = datetime.now(UTC)
    row.confirmed_letter_digest = agent_queue.letter_digest(current.item.letter)
    row.confirmed_card_digest = digest
    await session.commit()
    logger.info("confirmations.given", vacancy_id=str(vacancy_id))
    return await card(session, vacancy_id)


async def withdraw(session: AsyncSession, vacancy_id: UUID) -> ConfirmationCard:
    """Take a confirmation back before the agent uses it."""
    await session.execute(
        update(Application)
        .where(Application.vacancy_id == vacancy_id)
        .values(confirmed_at=None, confirmed_letter_digest=None, confirmed_card_digest=None)
    )
    await session.commit()
    logger.info("confirmations.withdrawn", vacancy_id=str(vacancy_id))
    return await card(session, vacancy_id)


async def confirmed(session: AsyncSession) -> ConfirmedList:
    """Every vacancy with a confirmation the agent would still honour.

    Read through the queue itself, so the count on the "send" button is the
    number the agent will actually be handed, not the number of rows with a
    timestamp.
    """
    served = await agent_queue.build_queue(
        session, limit=50, min_score=_floor(), require_letter=True
    )
    items = [item for item in served.items if item.confirmation is not None]
    stale = await session.scalar(
        select(func.count()).select_from(Application).where(Application.confirmed_at.is_not(None))
    )
    return ConfirmedList(
        items=[
            ConfirmedVacancy(
                external_id=item.vacancy_id,
                title=item.title,
                company=item.company,
                confirmed_at=item.confirmation.confirmed_at,
            )
            for item in items
            if item.confirmation is not None
        ],
        no_longer_valid=max(0, int(stale or 0) - len(items)),
    )


# ── why a vacancy cannot be confirmed ─────────────────────────────────


def _floor() -> Decimal:
    """The queue's own score floor, as the queue compares it."""
    return Decimal(settings.agent_queue_min_score)


async def _blockers(session: AsyncSession, vacancy: Vacancy) -> list[str]:
    """Every reason the agent would not be handed this vacancy, in words.

    Mirrors the conditions of ``agent_queue._queue_statement`` one by one, so the
    owner sees which one applies instead of a disabled button.
    """
    reasons: list[str] = []
    if not vacancy.is_active:
        reasons.append("Вакансия в архиве: при последнем обходе её на сайте уже не было.")
    if vacancy.is_spam:
        reasons.append("Вакансия помечена как спам.")
    applications = (
        await session.execute(
            select(
                Application.status,
                Application.applied_at,
                Application.sent_at,
                Application.agent_status,
                Application.agent_reason,
                Application.cover_letter,
                Application.hh_negotiations_total,
                Application.hh_last_state,
            )
            .where(Application.vacancy_id == vacancy.id)
            .order_by(Application.created_at, Application.id)
        )
    ).all()
    for row in applications:
        if row.sent_at is not None:
            confirmed_by_hh = send_confirmed(
                sent_at=row.sent_at,
                negotiations_total=row.hh_negotiations_total,
                last_state=row.hh_last_state,
            )
            reasons.append(
                "Отклик на эту вакансию уже отправлен"
                + ("." if confirmed_by_hh else " (hh пока не подтвердил — обновите исходы).")
            )
            break
        if row.agent_status == "skipped":
            reasons.append(
                "Агент уже открывал эту вакансию, и hh сказал, что откликаться нельзя"
                + (f": {row.agent_reason}" if row.agent_reason else ".")
            )
            break
        if row.status is not ApplicationStatus.SAVED or row.applied_at is not None:
            reasons.append("По этой вакансии в трекере уже отмечен отклик.")
            break
    if not any(row.cover_letter for row in applications):
        reasons.append("Письма ещё нет — сначала напишите его в разделе документов ниже.")

    seed = await session.scalar(
        select(func.count())
        .select_from(VacancySource)
        .where(
            and_(
                VacancySource.vacancy_id == vacancy.id,
                VacancySource.external_id.like(f"%{agent_queue.SEED_ID_MARKER}%"),
            )
        )
    )
    if seed:
        reasons.append("Это тестовая запись, а не настоящая вакансия.")

    profile_id = await agent_queue.active_profile_id(session)
    if profile_id is None:
        reasons.append("Активного резюме нет — загрузите его на «Мои данные».")
        return reasons
    match = (
        await session.execute(
            select(Match.score, Match.bucket)
            .where(Match.vacancy_id == vacancy.id)
            .where(Match.profile_id == profile_id)
        )
    ).first()
    if match is None:
        reasons.append("Вакансия не оценена против текущего резюме — пересчитайте подбор.")
    elif match.bucket == MatchBucket.FILTERED:
        reasons.append("Подбор отсеял эту вакансию фильтром — откликаться на неё агент не будет.")
    elif match.score < _floor():
        reasons.append(
            f"Оценка {float(match.score):.0f} ниже порога очереди агента "
            f"({_floor()}, настройка AGENT_QUEUE_MIN_SCORE)."
        )
    return reasons


async def _letter_row(session: AsyncSession, vacancy_id: UUID) -> Application | None:
    """The row whose letter the queue serves: the oldest one holding a letter."""
    row: Application | None = await session.scalar(
        select(Application)
        .where(Application.vacancy_id == vacancy_id)
        .where(Application.cover_letter.is_not(None))
        .order_by(Application.created_at, Application.id)
        .limit(1)
    )
    return row


async def _state(
    session: AsyncSession, vacancy_id: UUID, item: QueueItem | None
) -> ConfirmationState | None:
    """Whether a confirmation is on record and whether it still applies."""
    row = await _letter_row(session, vacancy_id)
    if row is None or row.confirmed_at is None:
        return None
    if item is not None and item.confirmation is not None:
        return ConfirmationState(confirmed_at=row.confirmed_at, valid=True)
    expired = datetime.now(UTC) - row.confirmed_at > timedelta(
        hours=settings.agent_confirmation_ttl_hours
    )
    return ConfirmationState(
        confirmed_at=row.confirmed_at,
        valid=False,
        reason=(
            f"Подтверждение старше {settings.agent_confirmation_ttl_hours} ч — подтвердите заново."
            if expired
            else "После подтверждения карточка изменилась — агент его не использует. "
            "Перечитайте и подтвердите заново."
        ),
    )
