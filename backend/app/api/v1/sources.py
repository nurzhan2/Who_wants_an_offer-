"""The sources page: what is configured, what is idle, and why."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.schemas.source import SourcesResponse
from app.services import sources as sources_service

router = APIRouter(prefix="/sources", tags=["sources"])


@router.get(
    "",
    response_model=SourcesResponse,
    summary="Registered sources, their limits and why any are idle",
)
async def list_sources(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SourcesResponse:
    """Every connector the registry found.

    Includes the ones that will not run, with the reason: a source that is
    silently absent from this list is indistinguishable from a source that
    found nothing, and those need different actions from the person reading it.

    Credentials are reported by NAME and by presence only. Never a value, a
    prefix or a length — each of those narrows a key for anyone who can read the
    response or a screenshot of it.
    """
    return await sources_service.list_sources(session)
