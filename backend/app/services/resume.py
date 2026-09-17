"""Resume upload and profile reading.

The upload path is split deliberately:

* the **request** validates the file and reserves a profile row, so a bad
  upload is a 4xx the user sees immediately rather than a profile that turns up
  "failed" thirty seconds later;
* the **background task** does the slow work — one LLM call and one embedding —
  and takes nothing but a profile id and a path. That is the same signature a
  real job queue needs in phase 9, and it keeps memory bounded when several
  resumes are uploaded at once.

The file on disk is what connects the two. It is removed in a ``finally``, and
whatever a crash leaves behind is swept at startup.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.enums import ParseStatus
from app.db.models import CandidateProfile
from app.db.repositories.profile import ProfileRepository
from app.db.session import session_factory
from app.llm.router import LLMRouter, get_router
from app.resume import ats_audit, extractor, profile_builder
from app.schemas.ats import ATSReport
from app.services import contacts as contact_service

logger = get_logger(__name__)

#: Shown on the documents screen, so Russian. A parse runs inside the API
#: process; when that process dies the parse dies with it and nothing else will
#: ever finish it, so the row is told apart from one that is still going.
STALE_PARSE_REASON = (
    "Разбор не завершился: сервер остановили, пока он шёл. Загрузите резюме заново — "
    "эта запись останется в списке, пока вы её не удалите."
)


@dataclass(frozen=True, slots=True)
class UploadAccepted:
    """What the upload endpoint answers with."""

    profile_id: UUID
    parse_status: ParseStatus


def _audit(document: extractor.ExtractedDocument) -> ATSReport | None:
    """Judge machine readability, or give up quietly if the audit itself breaks.

    Runs in the request rather than the background task on purpose. The report
    is the one useful thing available before the LLM has finished — a resume no
    parser can read is worth saying so within the second, not after forty of
    them — and the checks are arithmetic on word boxes, not a model call.

    A failure here must not fail the upload: a resume that defeats the auditor
    is still a resume. It is logged rather than swallowed, and the profile keeps
    a NULL report, which the API reports as "no report" and never as "clean".
    """
    try:
        return ats_audit.audit(
            document.file_bytes,
            source_format=document.source_format,
            raw_text=document.raw_text,
        )
    except Exception as exc:
        logger.exception("resume.ats_audit_failed", error=type(exc).__name__)
        return None


def upload_path(profile_id: UUID, source_format: str) -> Path:
    """Where an uploaded file waits while the background task runs."""
    return settings.upload_dir / f"{profile_id}.{source_format}"


async def accept_upload(
    session: AsyncSession, *, content: bytes, filename: str
) -> tuple[UploadAccepted, Path]:
    """Validate an upload, reserve a profile, and stage the file.

    Extraction runs here as well as in the task. It is cheap next to the LLM
    call, and doing it now is what turns an .exe renamed to .pdf into an
    immediate 422 instead of a background failure nobody is watching.
    """
    document = extractor.extract(content, filename)
    report = await asyncio.to_thread(_audit, document)

    profiles = ProfileRepository(session)
    profile = await profiles.create_pending(
        filename=filename,
        size_bytes=document.size_bytes,
        source_format=document.source_format,
        started_at=datetime.now(UTC),
        ats_report=report,
    )

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_path(profile.id, document.source_format)
    await asyncio.to_thread(path.write_bytes, content)

    logger.info(
        "resume.accepted",
        profile_id=str(profile.id),
        source_format=document.source_format,
        size_bytes=document.size_bytes,
        page_count=document.page_count,
        needs_ocr=document.needs_ocr,
        ats_score=report.score if report else None,
        ats_findings=[f.code.value for f in report.findings] if report else None,
    )
    return UploadAccepted(profile_id=profile.id, parse_status=ParseStatus.PENDING), path


def background_failure(exc: Exception) -> str:
    """What the documents screen says about a parse that crashed, in Russian.

    It used to say «parsing failed unexpectedly», which names neither what
    broke nor what to do (seen 2026-09-17: the embedding model could not reach
    huggingface.co). The exception's text is not shown — it can carry a URL or
    a path — only its class, which is what the server log is searched by.
    """
    return (
        f"Разбор не завершился из-за ошибки на сервере ({type(exc).__name__}). "
        "Загрузите резюме ещё раз. Если повторится — проверьте подключение к интернету "
        "и настройки моделей в .env; подробности записаны в журнал сервера."
    )


async def parse_in_background(
    profile_id: UUID, path: Path, *, router: LLMRouter | None = None
) -> None:
    """Do the slow half of the upload.

    Opens its own session on purpose: the request's session is closed the
    moment the response is sent, and reusing it here would either fail or write
    into a transaction nobody will commit.
    """
    llm = router or get_router()
    try:
        content = await asyncio.to_thread(path.read_bytes)
        document = extractor.extract(content, path.name)
        async with session_factory() as session:
            result = await profile_builder.build_profile(
                document, session=session, profile_id=profile_id, router=llm
            )
            if result.status is ParseStatus.READY:
                await fill_contacts(session, profile_id)
            await session.commit()
    except Exception as exc:  # a background task must never die silently
        logger.exception(
            "resume.background_failed", profile_id=str(profile_id), error=type(exc).__name__
        )
        async with session_factory() as session:
            await ProfileRepository(session).set_parse_status(
                profile_id, ParseStatus.FAILED, error=background_failure(exc)
            )
            await session.commit()
    finally:
        # Always: a staged file whose task has ended is dead weight, and
        # uploads/ would otherwise grow one resume at a time.
        await asyncio.to_thread(path.unlink, True)


async def fill_contacts(session: AsyncSession, profile_id: UUID) -> None:
    """Fill the profile's contact block from the resume that was just parsed.

    Here rather than inside ``build_profile`` because that module orchestrates
    the *profile*: extraction, enrichment, embedding, scoring readiness. The
    contact block is a different thing with a different writer, and this is the
    layer allowed to reach for a second service.

    It reads the values back off the stored profile instead of taking them from
    the extraction: whatever landed in the row is what the rest of the product
    believes, and a contact block that disagreed with the profile it belongs to
    would be its own kind of bug.

    Only after a successful parse. A failed one has no name, no city and no
    text to read a phone number out of, and writing an empty block would leave
    the screen looking like extraction found nothing rather than never ran.
    """
    profile = await ProfileRepository(session).get(profile_id)
    if profile is None:  # pragma: no cover - deleted while the parse ran
        return
    await contact_service.prefill_from_resume(
        session,
        profile_id=profile_id,
        raw_text=profile.raw_text,
        full_name=profile.name,
        # locations is a list because a profile can want several cities; the
        # contact block prints one, and the first is the one extraction read
        # off the contact line.
        city=profile.locations[0] if profile.locations else None,
    )


async def get_profile(session: AsyncSession, profile_id: UUID) -> CandidateProfile | None:
    """Read a profile, resolving a parse that will never finish.

    ``BackgroundTasks`` does not survive a process restart, so a profile can be
    left pending for ever. Rather than a scheduled job, the staleness is settled
    whenever someone looks: the client polling the status is exactly who needs
    the answer.
    """
    profiles = ProfileRepository(session)
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.resume_parse_timeout_seconds)
    failed = await profiles.fail_stale_pending(cutoff, STALE_PARSE_REASON)
    if failed:
        logger.warning("resume.stale_pending_failed", count=failed)
    return await profiles.get(profile_id)


async def fail_interrupted_parses(session: AsyncSession) -> int:
    """Mark every parse still pending at startup as failed. Called from the lifespan.

    The parse is a background task *of this process*, and this process has only
    just started — so a row that says ``pending`` now belongs to a process that
    is gone, whatever its age. Without this such a row sat on the documents
    screen under «разбирается» for ever (measured: one from 2026-09-08, left by a
    server killed mid-upload), because only a request for that one profile
    settled it.

    Marked, never deleted: it is the owner's row and removing it is their call.
    The one-process assumption is the same one ``app.services.pipeline`` states;
    under several workers this would fail a sibling's live parse.
    """
    failed = await ProfileRepository(session).fail_stale_pending(
        datetime.now(UTC), STALE_PARSE_REASON
    )
    await session.commit()
    if failed:
        logger.warning("resume.interrupted_parses_failed", count=failed)
    return failed


async def sweep_orphaned_uploads() -> int:
    """Delete staged files no task will ever come back for.

    Called from the application lifespan. A file older than the parse timeout
    belongs to a process that is gone.
    """
    directory = settings.upload_dir
    if not directory.is_dir():
        return 0

    cutoff = datetime.now(UTC) - timedelta(seconds=settings.resume_parse_timeout_seconds)
    removed = 0
    for path in directory.iterdir():
        if not path.is_file():
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if modified < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    if removed:
        logger.info("resume.orphaned_uploads_swept", count=removed)
    return removed
