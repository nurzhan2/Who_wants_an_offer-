"""What this project has generated, and the one thing it may generate."""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.schemas.dashboard import Documents, LetterRequest, QueuedLetter, WorkshopResult
from app.services import documents as documents_service
from app.services import workshop as workshop_service

router = APIRouter(prefix="/documents", tags=["dashboard"])


@router.get(
    "",
    response_model=Documents,
    summary="Resumes with their ATS audit, and every letter written",
)
async def read_documents(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Documents:
    """Both kinds of document, with what became of each.

    Resumes are uploaded rather than generated — nothing in this repository
    writes a CV — so what is shown for one is the file and the readability audit
    taken when it landed: what an employer's parser sees, and what it loses.

    Letters carry the three dates the feedback loop is made of (written, sent,
    answered), the rules version that judged each one, and what today's rules
    make of the same text. A letter that passed when it was written and does not
    now is the most interesting row on the screen.
    """
    return await documents_service.build(session)


@router.get(
    "/queue",
    response_model=list[QueuedLetter],
    summary="Vacancies worth a letter, best first",
)
async def read_queue(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = workshop_service.DEFAULT_QUEUE_LIMIT,
    min_score: Annotated[Decimal, Query(ge=0, le=100)] = workshop_service.DEFAULT_MIN_SCORE,
) -> list[QueuedLetter]:
    """What the workshop offers to write next.

    Includes vacancies whose letter is already written, flagged as such: the
    batch writer skips those so a repeated run costs nothing, but a person
    looking at a queue needs to see that one is done rather than wonder where it
    went.
    """
    return await workshop_service.queue(session, limit=limit, min_score=min_score)


@router.post(
    "/letters",
    response_model=WorkshopResult,
    summary="Write the letter for one vacancy",
)
async def write_letter(
    payload: LetterRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WorkshopResult:
    """Generate a letter and save it on the tracker row. Never send it.

    **The only write the dashboard makes, and it produces a document.** Sending
    is ``wwao apply --send``, where the same letter is printed on a confirmation
    card and a person at the keyboard says yes to that letter for that vacancy.
    There is no send button here and there is not meant to be: a browser cannot
    make the promise the confirmation exists to make.

    201 when a letter was written and saved, 200 when nothing was: the vacancy
    is gone, a letter already exists and ``force`` was not set, or nothing could
    be written that passes the checks. All three are answers rather than errors,
    and each names itself in ``skipped`` — a 4xx would make a screen show a
    failure where the honest report is "there was already one".

    A missing active profile is a 409 rather than a skip. Every other outcome is
    about this vacancy; that one is about the installation, and it stays true
    for every vacancy until somebody uploads a resume.
    """
    result = await workshop_service.write(session, payload.vacancy_id, force=payload.force)
    if result.skipped == workshop_service.NO_PROFILE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No active profile: upload a resume before generating letters.",
        )
    if result.saved:
        response.status_code = status.HTTP_201_CREATED
    return result
