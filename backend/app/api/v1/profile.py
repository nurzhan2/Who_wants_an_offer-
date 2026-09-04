"""Profile read and manual correction endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.profile import ProfileRepository
from app.db.session import get_session
from app.schemas.ats import ATSReport
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


@router.get(
    "/{profile_id}/ats-report",
    response_model=ATSReport,
    summary="Will an employer's parser read this resume?",
)
async def read_ats_report(
    profile_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ATSReport:
    """Return the readability audit stored at upload.

    Separate from the profile because it is ready at different times. The
    report exists the moment the file lands; the profile it belongs to is still
    being extracted for another half-minute. A client can show "no parser can
    read this file" while the spinner is still turning, which is the whole
    point — and the profile poll stays small.
    """
    report = await ProfileRepository(session).get_ats_report(profile_id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            # Deliberately distinct from the profile's 404: a profile uploaded
            # before this check existed is not a missing profile, and a client
            # must not read the absence of a report as a clean one.
            detail="No ATS report recorded for this profile",
        )
    return report
