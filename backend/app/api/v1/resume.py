"""Resume upload endpoint."""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ParsingError
from app.db.session import get_session
from app.schemas.profile import ResumeUploadResponse
from app.services import resume as resume_service

router = APIRouter(prefix="/resume", tags=["resume"])


@router.post(
    "/upload",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ResumeUploadResponse,
    summary="Upload a resume and start parsing it",
)
async def upload_resume(
    background: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    file: Annotated[UploadFile, File(description="PDF, DOCX, TXT or Markdown, up to 10 MB.")],
) -> ResumeUploadResponse:
    """Accept a resume and hand back an id to poll.

    202, not 201: the profile exists but is not finished. Parsing needs an LLM
    round trip and an embedding, which is far longer than a request should hold
    a connection open for.
    """
    # Bounded read: an UploadFile is a stream, and reading it whole before
    # checking the size would let a large upload decide our memory use.
    limit = settings.resume_max_file_size_mb * 1024 * 1024
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise ParsingError(f"file is larger than the {settings.resume_max_file_size_mb} MB limit")

    accepted, path = await resume_service.accept_upload(
        session, content=content, filename=file.filename or "resume"
    )
    await session.commit()

    background.add_task(resume_service.parse_in_background, accepted.profile_id, path)
    return ResumeUploadResponse(profile_id=accepted.profile_id, parse_status=accepted.parse_status)
