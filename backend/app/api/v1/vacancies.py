"""The ranked list, and one vacancy explained."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.schemas.common import CursorPage
from app.schemas.dashboard import VacancyCard
from app.schemas.vacancy import VacancyListItem, VacancyQuery
from app.services import vacancies as vacancies_service

router = APIRouter(prefix="/vacancies", tags=["vacancies"])


@router.get(
    "",
    response_model=CursorPage[VacancyListItem],
    summary="Scored vacancies, best first",
)
async def list_vacancies(
    session: Annotated[AsyncSession, Depends(get_session)],
    # One model and no sibling query parameters, deliberately: FastAPI expands a
    # Pydantic model into individual query fields only while it is the handler's
    # *only* query field. Put a plain ``limit`` beside it and every filter is
    # silently ignored — which is why paging lives inside VacancyQuery.
    #
    # Defaulted because every field of it is optional; without a default the
    # unfiltered list answers 422 naming a parameter nobody typed.
    query: Annotated[VacancyQuery, Query()] = VacancyQuery(),  # noqa: B008
) -> CursorPage[VacancyListItem]:
    """One page of the list, ordered by score for the active profile.

    **Keyset, not offset.** ``next_cursor`` encodes the sort value and the id of
    the last row, so a crawl writing rows underneath a reader cannot make rows
    repeat or vanish between pages — which on this corpus is not hypothetical,
    since a crawl runs for twenty minutes and somebody reads the list while it
    does.

    ``with_total`` and ``with_facets`` are opt-in because each is a second
    query. The table does not need them; the filter sidebar does.

    Every filter is a query parameter of the same name — ``?city=Алматы&
    remote=full&score_min=70`` — and one of them has a default worth knowing:
    ``salary_min`` keeps postings that advertise no salary, because five in six
    of them do not. ``include_unpriced=false`` asks the other question.
    """
    return await vacancies_service.list_page(
        session,
        query.filters(),
        cursor=query.cursor,
        limit=query.limit,
        with_total=query.with_total,
        with_facets=query.with_facets,
    )


@router.get(
    "/{vacancy_id}",
    response_model=VacancyCard,
    summary="One vacancy, its score and what it asks for",
)
async def read_vacancy(
    vacancy_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> VacancyCard:
    """The card behind a row.

    Its requirement list comes back in three parts rather than two: covered, not
    covered *but demonstrably held* — an earlier resume says so, or this one's
    text does and extraction missed it — and genuinely absent. The scorer cannot
    draw that line and does not try to; ``app/services/vacancies.py`` explains
    why the screen has to.
    """
    card = await vacancies_service.card(session, vacancy_id)
    if card is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vacancy not found")
    return card
