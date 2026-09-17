"""The upload endpoint and the service behind it.

Upload is the one place where an HTTP request, a database row, a file on disk
and a background task have to stay in step. The failures these tests exist to
catch are the ones where they drift apart:

* a rejected upload that still left a profile row behind, so the dashboard
  shows a resume nobody uploaded;
* a staged file that outlives its task, so ``uploads/`` grows one resume at a
  time until the disk fills;
* a profile stuck in ``pending`` for ever because the process that was going to
  parse it is gone.

**No test here may reach a model.** ``httpx``'s ``ASGITransport`` really does
run FastAPI background tasks once the response is finished, so an upload test
that left ``parse_in_background`` alone would build the real ``LLMRouter`` and
call whichever provider resume extraction is routed to — the Claude Code CLI by
default, which is a real subprocess and a real bill. Every test therefore
either replaces ``parse_in_background`` with a recorder (``scheduled_parses``)
or, where the task itself is the subject, stubs out the two things it reaches
for: ``profile_builder.build_profile`` and ``session_factory``.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import PROBLEM_JSON
from app.db.enums import ParseStatus
from app.db.models import CandidateProfile
from app.db.repositories.profile import ProfileRepository
from app.llm.router import LLMRouter
from app.resume import profile_builder
from app.schemas.ats import ATSReport, FindingCode
from app.services import resume as resume_service

RESUMES = Path(__file__).parent / "fixtures" / "resumes"
UPLOAD_URL = f"{settings.api_v1_prefix}/resume/upload"

#: Enough of a PE header for `filetype` to recognise a Windows executable.
EXECUTABLE_BYTES = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 512

#: Passed instead of ``None`` so ``parse_in_background`` never builds the real
#: router — which would resolve a provider, and the configured one for resume
#: extraction is a CLI subprocess. Nothing ever calls a method on it: the tests
#: that use it stub ``build_profile``, so an attribute access here would be a
#: test that lies about not talking to a model.
UNUSED_ROUTER = cast(LLMRouter, object())


@pytest.fixture
def staging_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``settings.upload_dir`` at a throwaway directory.

    Without this the tests would write into the repository's real ``uploads/``
    and the deletion assertions would be about the developer's machine.
    """
    directory = tmp_path / "uploads"
    monkeypatch.setattr(settings, "upload_dir", directory)
    return directory


@pytest.fixture
def scheduled_parses(monkeypatch: pytest.MonkeyPatch) -> list[tuple[UUID, Path]]:
    """Replace the background task with a recorder of its arguments.

    The router looks the function up on the service module at request time, so
    patching the module attribute is enough. The recorded arguments are the
    contract with a real job queue later: an id and a path, nothing else.
    """
    calls: list[tuple[UUID, Path]] = []

    async def record(profile_id: UUID, path: Path, **_: Any) -> None:
        calls.append((profile_id, path))

    monkeypatch.setattr(resume_service, "parse_in_background", record)
    return calls


@pytest.fixture
def stub_session(monkeypatch: pytest.MonkeyPatch) -> "_StubSession":
    """Give ``parse_in_background`` a session that touches no database.

    The task deliberately opens its own session rather than reusing the
    request's, so it bypasses the test transaction entirely. Stubbing the
    factory keeps the task's file handling testable without it writing to a
    real database outside any rollback.
    """
    session = _StubSession()

    @asynccontextmanager
    async def factory() -> AsyncIterator[_StubSession]:
        yield session

    monkeypatch.setattr(resume_service, "session_factory", factory)
    return session


class _StubSession:
    """Records statements instead of executing them."""

    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.commits = 0

    async def execute(self, statement: Any, *_: Any, **__: Any) -> None:
        self.statements.append(statement)

    async def commit(self) -> None:
        self.commits += 1


async def profile_count(session: AsyncSession) -> int:
    """How many profile rows exist right now."""
    return int(
        (await session.execute(select(func.count()).select_from(CandidateProfile))).scalar_one()
    )


def resume_upload(name: str = "resume.pdf") -> dict[str, tuple[str, bytes, str]]:
    """A multipart payload carrying a real PDF resume."""
    return {"file": (name, (RESUMES / "single_column_ru.pdf").read_bytes(), "application/pdf")}


# ── the happy path ────────────────────────────────────────────────────


async def test_upload_answers_202_with_an_id_to_poll(
    async_client: AsyncClient, staging_dir: Path, scheduled_parses: list[tuple[UUID, Path]]
) -> None:
    """202 and not 201: the profile is reserved but unparsed, and the client has
    to be handed an id it can poll rather than a resource it can read."""
    response = await async_client.post(UPLOAD_URL, files=resume_upload())

    assert response.status_code == 202
    body = response.json()
    assert UUID(body["profile_id"])
    assert body["parse_status"] == ParseStatus.PENDING.value


async def test_upload_records_what_was_uploaded(
    async_client: AsyncClient,
    db_session: AsyncSession,
    staging_dir: Path,
    scheduled_parses: list[tuple[UUID, Path]],
) -> None:
    """The reserved row has to carry the filename, size and detected format, or
    a user polling a failed parse cannot tell which of their files went wrong."""
    response = await async_client.post(UPLOAD_URL, files=resume_upload("Иванов_CV.pdf"))
    profile_id = UUID(response.json()["profile_id"])

    profile = await ProfileRepository(db_session).get(profile_id)

    assert profile is not None
    assert profile.parse_status is ParseStatus.PENDING
    assert profile.resume_filename == "Иванов_CV.pdf"
    assert profile.resume_size_bytes == len(resume_upload()["file"][1])
    # The format comes from the magic bytes, not from the ".pdf" in the name.
    assert profile.resume_format == "pdf"
    # An unparsed profile must never be the one the dashboard scores against.
    assert profile.is_active is False


async def test_upload_schedules_the_task_with_the_id_and_the_staged_path(
    async_client: AsyncClient, staging_dir: Path, scheduled_parses: list[tuple[UUID, Path]]
) -> None:
    """The task is given an id and a path and nothing else. That signature is
    what lets the slow half move to a real queue later; passing the request's
    session or the file's bytes instead would tie it to this process."""
    response = await async_client.post(UPLOAD_URL, files=resume_upload())
    profile_id = UUID(response.json()["profile_id"])

    assert scheduled_parses == [(profile_id, staging_dir / f"{profile_id}.pdf")]


async def test_upload_stages_the_file_under_the_configured_directory(
    async_client: AsyncClient, staging_dir: Path, scheduled_parses: list[tuple[UUID, Path]]
) -> None:
    """The bytes are on disk before the response is sent. The task reads the
    file rather than a buffer, so an upload that answered 202 without writing
    it would fail in the background where nobody is watching."""
    response = await async_client.post(UPLOAD_URL, files=resume_upload())
    profile_id = UUID(response.json()["profile_id"])

    staged = staging_dir / f"{profile_id}.pdf"
    assert staged.read_bytes() == resume_upload()["file"][1]


# ── rejections ────────────────────────────────────────────────────────


async def test_oversized_upload_is_rejected_before_anything_is_written(
    async_client: AsyncClient,
    db_session: AsyncSession,
    staging_dir: Path,
    scheduled_parses: list[tuple[UUID, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Size on its own has to be enough to refuse an upload, and the refusal has
    to come before the row is reserved: reserving first would leave a profile
    the user never gets to see.

    The payload is a perfectly readable text resume whose only fault is its
    length, so the size limit is the only thing that can reject it. Padding
    unparseable bytes to the same length would pass this test with every size
    check deleted, which is the trap this test exists to avoid.
    """
    # A 1 MB ceiling keeps the payload small; the endpoint reads the limit from
    # settings, so the behaviour under test is unchanged.
    monkeypatch.setattr(settings, "resume_max_file_size_mb", 1)
    before = await profile_count(db_session)
    line = b"Ivan Ivanov - Python developer, FastAPI, PostgreSQL.\n"
    payload = line * (1024 * 1024 // len(line) + 1)

    response = await async_client.post(
        UPLOAD_URL, files={"file": ("resume.txt", payload, "text/plain")}
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert await profile_count(db_session) == before
    assert scheduled_parses == []
    assert not staging_dir.exists() or not list(staging_dir.iterdir())


async def test_executable_renamed_to_pdf_is_rejected_and_writes_no_row(
    async_client: AsyncClient,
    db_session: AsyncSession,
    staging_dir: Path,
    scheduled_parses: list[tuple[UUID, Path]],
) -> None:
    """The extension is a claim by whoever uploaded the file. Trusting it would
    hand an executable to pdfplumber and leave a profile row behind for it."""
    before = await profile_count(db_session)

    response = await async_client.post(
        UPLOAD_URL, files={"file": ("resume.pdf", EXECUTABLE_BYTES, "application/pdf")}
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    # The rejection names the *detected* type, not the claimed one. Without
    # this the test would pass even if the ".pdf" were believed, because
    # pdfplumber refuses an executable too — and the point is that nothing
    # ever hands it one.
    assert "msdownload" in response.json()["detail"]
    assert await profile_count(db_session) == before
    assert scheduled_parses == []
    assert not staging_dir.exists() or not list(staging_dir.iterdir())


# ── the background task cleans up after itself ────────────────────────


@pytest.mark.unit
async def test_staged_file_is_deleted_after_a_successful_parse(
    staging_dir: Path, stub_session: _StubSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parsed resume is personal data with no reason to stay on disk, and
    uploads/ would otherwise grow by one file per upload for ever."""
    calls: list[UUID] = []

    async def fake_build(_: Any, *, profile_id: UUID, **__: Any) -> None:
        calls.append(profile_id)

    monkeypatch.setattr(profile_builder, "build_profile", fake_build)
    profile_id = uuid4()
    staged = staging_dir / f"{profile_id}.pdf"
    staging_dir.mkdir(parents=True)
    staged.write_bytes((RESUMES / "single_column_ru.pdf").read_bytes())

    await resume_service.parse_in_background(profile_id, staged, router=UNUSED_ROUTER)

    assert calls == [profile_id]
    assert not staged.exists()


@pytest.mark.unit
async def test_staged_file_is_deleted_when_the_parse_fails(
    staging_dir: Path, stub_session: _StubSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure path is the one that leaks. A file left behind by every
    failed parse is exactly the file nobody notices until the disk is full —
    and the task must swallow the error rather than crash the worker."""

    async def explode(*_: Any, **__: Any) -> None:
        raise RuntimeError("the model refused")

    monkeypatch.setattr(profile_builder, "build_profile", explode)
    profile_id = uuid4()
    staged = staging_dir / f"{profile_id}.pdf"
    staging_dir.mkdir(parents=True)
    staged.write_bytes((RESUMES / "single_column_ru.pdf").read_bytes())

    await resume_service.parse_in_background(profile_id, staged, router=UNUSED_ROUTER)

    assert not staged.exists()
    # The failure was recorded rather than lost: a profile whose task blew up
    # must stop being "pending", or the client polls it until the timeout.
    assert stub_session.commits == 1
    written = [statement.compile().params for statement in stub_session.statements]
    assert [params["parse_status"] for params in written] == [ParseStatus.FAILED]
    # A reason the user can act on, and never a fragment of the resume.
    assert written[0]["parse_error"]
    assert "the model refused" not in written[0]["parse_error"]


@pytest.mark.unit
async def test_sweep_removes_uploads_older_than_the_parse_timeout(staging_dir: Path) -> None:
    """A staged file older than the parse timeout belongs to a process that is
    gone; a fresh one belongs to a task still running. Sweeping by age is the
    only signal available at startup, and getting it wrong either leaks files
    for ever or deletes a resume out from under a live parse."""
    staging_dir.mkdir(parents=True)
    stale = staging_dir / "stale.pdf"
    fresh = staging_dir / "fresh.pdf"
    stale.write_bytes(b"old")
    fresh.write_bytes(b"new")
    long_ago = datetime.now(UTC) - timedelta(seconds=settings.resume_parse_timeout_seconds * 2)
    os.utime(stale, (long_ago.timestamp(), long_ago.timestamp()))

    removed = await resume_service.sweep_orphaned_uploads()

    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()


# ── a parse that will never finish ────────────────────────────────────


async def test_profile_pending_past_the_timeout_reads_as_failed(
    db_session: AsyncSession, profiles: ProfileRepository
) -> None:
    """Background tasks do not survive a restart, so a pending row can outlive
    the process that would have finished it. Resolving staleness on read is
    what stops the dashboard spinning on it for ever."""
    started = datetime.now(UTC) - timedelta(seconds=settings.resume_parse_timeout_seconds + 60)
    profile = await profiles.create_pending(
        filename="old.pdf", size_bytes=1024, source_format="pdf", started_at=started
    )

    resolved = await resume_service.get_profile(db_session, profile.id)

    assert resolved is not None
    assert resolved.parse_status is ParseStatus.FAILED
    # A failure the user can act on, and never a fragment of the resume.
    assert resolved.parse_error == resume_service.STALE_PARSE_REASON


async def test_profile_pending_within_the_timeout_stays_pending(
    db_session: AsyncSession, profiles: ProfileRepository
) -> None:
    """The other half of the same rule: a parse that started a moment ago is
    still running, and failing it would kill working uploads."""
    profile = await profiles.create_pending(
        filename="new.pdf", size_bytes=1024, source_format="pdf", started_at=datetime.now(UTC)
    )

    resolved = await resume_service.get_profile(db_session, profile.id)

    assert resolved is not None
    assert resolved.parse_status is ParseStatus.PENDING
    assert resolved.parse_error is None


# ── the ATS report, written at upload ─────────────────────────────────


def upload_of(name: str) -> dict[str, tuple[str, bytes, str]]:
    """A multipart payload carrying one named fixture."""
    return {"file": (name, (RESUMES / name).read_bytes(), "application/pdf")}


async def test_upload_stores_the_ats_report_on_the_reserved_row(
    async_client: AsyncClient,
    db_session: AsyncSession,
    staging_dir: Path,
    scheduled_parses: list[tuple[UUID, Path]],
) -> None:
    """The report has to exist before the background task runs.

    It is computed from the uploaded bytes, and the staged file is deleted the
    moment parsing ends — so an audit deferred to the task would be an audit
    that can never be recomputed if the task dies. Asserted on the reserved row
    while the parse is still PENDING, which is exactly that window."""
    response = await async_client.post(UPLOAD_URL, files=upload_of("two_column_ru.pdf"))
    profile_id = UUID(response.json()["profile_id"])

    profile = await ProfileRepository(db_session).get(profile_id)

    assert profile is not None
    assert profile.parse_status is ParseStatus.PENDING
    assert profile.ats_report is not None
    report = ATSReport.model_validate(profile.ats_report)
    assert FindingCode.COLUMN_INTERLEAVING in {f.code for f in report.findings}
    assert not report.is_machine_readable


async def test_upload_of_a_clean_resume_stores_a_clean_report(
    async_client: AsyncClient,
    db_session: AsyncSession,
    staging_dir: Path,
    scheduled_parses: list[tuple[UUID, Path]],
) -> None:
    """The other half of the assertion above: a well-formed PDF is not flagged."""
    response = await async_client.post(UPLOAD_URL, files=resume_upload())
    profile_id = UUID(response.json()["profile_id"])

    profile = await ProfileRepository(db_session).get(profile_id)

    assert profile is not None
    assert profile.ats_report is not None
    assert ATSReport.model_validate(profile.ats_report).score == 100


async def test_an_audit_that_breaks_does_not_break_the_upload(
    async_client: AsyncClient,
    db_session: AsyncSession,
    staging_dir: Path,
    scheduled_parses: list[tuple[UUID, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resume that defeats the auditor is still a resume.

    The audit is a diagnostic, not a gate. If it raises, the upload must still
    be accepted and the profile must still be parsed — with no report, which the
    API reports as "no report" and never as a clean bill of health."""

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("pdfplumber fell over")

    monkeypatch.setattr(resume_service.ats_audit, "audit", explode)

    response = await async_client.post(UPLOAD_URL, files=resume_upload())

    assert response.status_code == 202
    profile_id = UUID(response.json()["profile_id"])
    profile = await ProfileRepository(db_session).get(profile_id)
    assert profile is not None
    assert profile.ats_report is None
    # The upload still went through the whole path.
    assert profile.parse_status is ParseStatus.PENDING
    assert len(scheduled_parses) == 1


async def test_a_parse_left_pending_by_a_dead_process_is_failed_at_startup(
    db_session: AsyncSession, profiles: ProfileRepository
) -> None:
    """Measured: a row from 2026-09-08 said «разбирается» for a week.

    The parse runs inside the API process, so whatever is pending when the
    process starts belongs to one that is gone — however recent it is. The row
    is marked, not deleted: it is the owner's to remove.
    """
    moment_ago = datetime.now(UTC) - timedelta(seconds=5)
    profile = await profiles.create_pending(
        filename="killed.pdf", size_bytes=1024, source_format="pdf", started_at=moment_ago
    )

    failed = await resume_service.fail_interrupted_parses(db_session)

    assert failed == 1
    row = await profiles.get(profile.id)
    assert row is not None
    assert row.parse_status is ParseStatus.FAILED
    assert row.parse_error == resume_service.STALE_PARSE_REASON
    assert "Загрузите резюме заново" in row.parse_error


def test_a_crashed_parse_is_explained_in_russian_without_the_exception_text() -> None:
    """«parsing failed unexpectedly» named neither the cause nor the next step."""
    message = resume_service.background_failure(
        RuntimeError("https://huggingface.co/secret-path disconnected")
    )

    assert "RuntimeError" in message
    assert "Загрузите резюме ещё раз" in message
    assert "huggingface" not in message
