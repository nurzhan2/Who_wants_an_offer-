"""The first screen, in one request."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.schemas.dashboard import Overview
from app.services import overview as overview_service

router = APIRouter(prefix="/overview", tags=["dashboard"])


@router.get(
    "",
    response_model=Overview,
    summary="Corpus, crawl position, last run and applications, together",
)
async def read_overview(
    session: Annotated[AsyncSession, Depends(get_session)],
    harvest: Annotated[int, Query(ge=0, le=200)] = overview_service.HARVEST_LIMIT,
) -> Overview:
    """Everything the overview screen shows.

    One endpoint rather than six, because these numbers are read against each
    other — "1174 vacancies, 582 of them scored" is one fact — and six requests
    would let a screen show two halves of it taken at different moments.

    ``harvest`` caps the list of postings the last crawl bought *by name*. That
    list is the only way to see whether a run spent its budget on postings worth
    reading: hh's sitemap carries a URL and a date, so a page has to be paid for
    before anyone can tell what it advertises, and the counters alone cannot say
    whether the pages were python ones or somebody else's. Set it to 0 to skip
    the titles and keep the counts.
    """
    return await overview_service.build(session, harvest_limit=harvest)
