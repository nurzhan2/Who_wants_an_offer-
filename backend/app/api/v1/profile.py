"""Profile read and manual correction endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.profile import ProfileRepository
from app.db.session import get_session
from app.schemas.profile import CandidateProfileRead, CandidateProfileUpdate
from app.services import resume as resume_service

router = APIRouter(prefix="/profile", tags=["profile"])


@router.get(
    "/{profile_id}",
    response_model=CandidateProfileRead,
    summary="Profile, skills and parse status",
)
async def read_profile(
    profile_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CandidateProfileRead:
    """Return a profile. This is also the endpoint a client polls after upload."""
    profile = await resume_service.get_profile(session, profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
    await session.commit()
    return CandidateProfileRead.model_validate(profile)


@router.patch(
    "/{profile_id}",
    response_model=CandidateProfileRead,
    summary="Correct an extracted profile by hand",
)
async def update_profile(
    profile_id: UUID,
    changes: CandidateProfileUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CandidateProfileRead:
    """Apply manual corrections.

    Not a convenience endpoint. Extraction gets things wrong — a missed skill,
    an inflated level, the wrong city — and every one of those errors becomes a
    wrong match score. Being able to fix them is what keeps the scores honest.
    """
    profiles = ProfileRepository(session)
    updated = await profiles.update(profile_id, changes)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
    await session.commit()

    refreshed = await profiles.get(profile_id)
    if refreshed is None:  # pragma: no cover - deleted between the two statements
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
    return CandidateProfileRead.model_validate(refreshed)
