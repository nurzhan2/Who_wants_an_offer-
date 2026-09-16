"""Account-level notices the dashboard shows on every screen."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.schemas.notices import Notices
from app.services import notices as notices_service

router = APIRouter(prefix="/notices", tags=["dashboard"])


@router.get("", response_model=Notices, summary="What the owner must see on every screen")
async def read_notices(session: Annotated[AsyncSession, Depends(get_session)]) -> Notices:
    """hh's resume-visibility sentence, when the recorded sends carried it.

    Read from what the agent recorded, so it appears before the next send
    rather than during it. See ``app.services.notices``.
    """
    return await notices_service.build(session)
