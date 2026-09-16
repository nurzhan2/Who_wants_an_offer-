"""The applications board, and the owner's confirmation of an application.

Under ``/tracker`` rather than ``/applications``, and the separation is
deliberate rather than cosmetic. ``/applications`` is the seam to the local
apply agent: two endpoints behind a shared local token, one of which hands out
the letters that are about to be sent under the owner's name. This is a screen.
Hanging a third, unauthenticated path off that prefix would put a reader inside
a namespace whose whole documented promise is that nothing reaches it without
the token, and the next person to read either file would have to check which
half of the prefix they were in.

There is no send here, and there will not be one: the API has no browser and
no hh session. Since 2026-09-16 there is a *confirmation* here — the card the
terminal used to print is shown in a modal, and the owner's "yes" is recorded
bound to that card's content. The local agent sends it later, on the owner's
machine, after re-reading the vacancy page. What keeps that as strong as the
typed word is argued in ``app/services/confirmations.py``: the confirmation
binds a digest of the card, expires, is spent by the first attempt, and every
request that records or withdraws one needs a JSON body or a non-simple method,
so another origin cannot make it without a CORS preflight this API refuses.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.schemas.confirmations import ConfirmationCard, ConfirmedList, ConfirmRequest
from app.schemas.dashboard import Board
from app.services import confirmations as confirmations_service
from app.services import tracker as tracker_service

router = APIRouter(prefix="/tracker", tags=["dashboard"])


@router.get(
    "/board",
    response_model=Board,
    summary="Applications by stage, and by what hh said about them",
)
async def read_board(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Board:
    """Every tracked application, on two axes.

    Stages are where an application is in this project's pipeline — queued, set
    aside for a person with the reason the agent wrote, sent. Outcomes are what
    hh has since reported, grouped into the four answers worth acting on and
    passed through verbatim when hh says something the grouping does not know.

    Each card carries the letter that was actually typed into the form, whole.
    That is the point of the screen: the question a feedback loop exists to
    answer is what the employer actually read, and a truncated letter answers a
    different one.
    """
    return await tracker_service.board(session)


@router.get(
    "/confirmations",
    response_model=ConfirmedList,
    summary="Applications confirmed in the dashboard that the agent would send",
)
async def list_confirmations(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConfirmedList:
    """What «Отправить подтверждённые» would hand the agent right now."""
    return await confirmations_service.confirmed(session)


@router.get(
    "/confirmations/{vacancy_id}",
    response_model=ConfirmationCard,
    summary="The card to confirm one application by",
)
async def read_confirmation(
    vacancy_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConfirmationCard:
    """The full card — letter, score with its reasons, hh's lines — and its digest."""
    return await confirmations_service.card(session, vacancy_id)


@router.post(
    "/confirmations/{vacancy_id}",
    response_model=ConfirmationCard,
    summary="Confirm exactly the card that was read",
)
async def confirm(
    vacancy_id: UUID,
    payload: ConfirmRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConfirmationCard:
    """409 when the card cannot be confirmed or has changed since it was read."""
    return await confirmations_service.confirm(session, vacancy_id, payload.card_digest)


@router.delete(
    "/confirmations/{vacancy_id}",
    response_model=ConfirmationCard,
    summary="Withdraw a confirmation the agent has not used yet",
)
async def withdraw(
    vacancy_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConfirmationCard:
    """Forget the confirmation; the agent's dashboard run then leaves this vacancy alone."""
    return await confirmations_service.withdraw(session, vacancy_id)
