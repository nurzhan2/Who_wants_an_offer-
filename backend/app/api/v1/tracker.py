"""The applications board. Reads, and only reads.

Under ``/tracker`` rather than ``/applications``, and the separation is
deliberate rather than cosmetic. ``/applications`` is the seam to the local
apply agent: two endpoints behind a shared local token, one of which hands out
the letters that are about to be sent under the owner's name. This is a screen.
Hanging a third, unauthenticated path off that prefix would put a reader inside
a namespace whose whole documented promise is that nothing reaches it without
the token, and the next person to read either file would have to check which
half of the prefix they were in.

There is no send here, and there will not be one. An application goes out
through ``wwao apply --send``, which prints the letter and waits for a person at
the keyboard; a browser cannot make that promise — a tab can be left open, a
page reloaded, a button clicked by the wrong window — and the confirmation
exists precisely because the thing being confirmed is irreversible and public.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.schemas.dashboard import Board
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
