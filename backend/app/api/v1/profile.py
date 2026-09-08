"""Profile read and manual correction endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.profile import ProfileRepository
from app.db.session import get_session
from app.schemas.ats import ATSReport
from app.schemas.contact import ProfileContactRead, ProfileContactUpdate
from app.schemas.profile import CandidateProfileRead, CandidateProfileUpdate
from app.services import ats as ats_service
from app.services import contacts as contact_service
from app.services import resume as resume_service

router = APIRouter(prefix="/profile", tags=["profile"])


@router.get(
    "/active",
    response_model=CandidateProfileRead,
    summary="The profile everything is scored against",
)
async def read_active_profile(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CandidateProfileRead:
    """The active resume, without having to know its id.

    Declared **above** ``/{profile_id}``: FastAPI matches routes in order, and
    the other way round "active" would be parsed as a UUID and answered with a
    422 that mentions neither the profile nor the word.

    It exists because every screen needs it and none of them has an id to start
    from. v1 is single-user, so "active" is a flag rather than a session; when
    that changes this endpoint is where the change lands, and no caller has to
    move.
    """
    profile = await ProfileRepository(session).get_active()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            # Distinct from the 404 below on purpose: "no resume has been
            # uploaded" and "that id is not a profile" send a person to
            # completely different places.
            detail="No active profile. Upload a resume first.",
        )
    return CandidateProfileRead.model_validate(profile)


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


@router.get(
    "/{profile_id}/contacts",
    response_model=ProfileContactRead,
    summary="Name, phone, email, city and links",
)
async def read_contacts(
    profile_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProfileContactRead:
    """Return the contact block the generated documents are stamped with.

    A profile with nothing filled in yet answers 200 with an empty block rather
    than 404: the screen behind this is a form, and "nothing here yet" is the
    state it exists to fix. 404 means the profile itself is not there.
    """
    contacts = await contact_service.get_contacts(session, profile_id)
    if contacts is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
    return contacts


@router.patch(
    "/{profile_id}/contacts",
    response_model=ProfileContactRead,
    summary="Correct the contact block by hand",
)
async def update_contacts(
    profile_id: UUID,
    changes: ProfileContactUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProfileContactRead:
    """Apply the owner's corrections and remember that they made them.

    Extraction reads contacts off a page and gets them wrong the way any parser
    does — a phone number split across two lines, a city taken from an
    employer's address. What is different from the rest of the profile is that
    nothing downstream can catch it: a wrong skill shows up as a strange match
    score, while a wrong phone number just means nobody calls.

    So every field named here is flagged as settled by a human and is never
    written by extraction again, including one set to null.
    """
    updated = await contact_service.update_contacts(session, profile_id, changes)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
    await session.commit()
    return updated


@router.get(
    "/{profile_id}/ats-report/{vacancy_id}",
    response_model=ATSReport,
    summary="The same audit, read against one vacancy's requirements",
)
async def read_ats_report_for_vacancy(
    profile_id: UUID,
    vacancy_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ATSReport:
    """Return the readability audit plus what this vacancy asks for.

    The same object as the endpoint above, with its keyword half filled in.
    One endpoint rather than one per screen: the vacancy page, the preview
    before a document is generated and the confirmation card before an
    application is sent are three views of one question, and three endpoints
    would eventually answer it three ways.

    The three buckets in ``keywords`` are not three degrees of the same thing.
    ``unstated`` is fixable by regenerating a document from the profile that is
    already stored; ``absent`` is not fixable by writing anything, and no part
    of this response suggests otherwise.
    """
    report = await ats_service.report_for_vacancy(session, profile_id, vacancy_id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No ATS report recorded for this profile",
        )
    return report
