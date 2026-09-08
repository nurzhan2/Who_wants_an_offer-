"""The workshop's HTTP surface: reference documents, rules, and a trial letter.

Validation, a call into ``app/workshop/service.py``, a response. The one design
decision that lives here rather than in the service is the URL space: every
mutation takes a ``UUID`` path parameter, and a built-in rule's id is
``builtin:no_links``, which is not a UUID. So a built-in cannot be addressed by
``PATCH`` or ``DELETE`` at all — FastAPI rejects the path before any handler
runs, and "undeletable" is a property of the routing table rather than a check
somebody has to remember to write.

Uploading a reference is the one multipart endpoint. It takes either a file or
pasted text, exactly one of them, because those are two ways of saying the same
thing and having two endpoints for it would mean two places to keep the size
floor and the extraction warnings in step.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ParsingError
from app.db.enums import ReferenceKind
from app.db.models import ReferenceDocument
from app.db.session import get_session
from app.schemas.workshop import (
    MAX_NOTE_CHARS,
    MAX_TITLE_CHARS,
    PreviewRequest,
    PreviewResponse,
    ReferenceCreated,
    ReferenceDetail,
    ReferenceRead,
    ReferenceUpdate,
    RuleCreate,
    RuleRead,
    RuleUpdate,
    VacancyChoice,
)
from app.workshop import prompt as workshop_prompt
from app.workshop import references as reference_documents
from app.workshop import service, store
from app.workshop.rules import RuleSpec

router = APIRouter(prefix="/workshop", tags=["workshop"])

#: How much of a reference is shown in a list row. Enough to recognise which
#: document it is, far short of shipping five CVs to render five rows.
PREVIEW_CHARS = 240


def _reference_read(row: ReferenceDocument) -> ReferenceRead:
    """One reference as a list row."""
    return ReferenceRead(
        id=row.id,
        kind=row.kind,
        title=row.title,
        note=row.note,
        is_active=row.is_active,
        characters=len(row.text),
        preview=row.text[:PREVIEW_CHARS],
        source_filename=row.source_filename,
        source_format=row.source_format,
        size_bytes=row.size_bytes,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _reference_detail(row: ReferenceDocument) -> ReferenceDetail:
    """One reference with its text."""
    return ReferenceDetail(**_reference_read(row).model_dump(), text=row.text)


def _rule_read(rule: RuleSpec) -> RuleRead:
    """One rule as the dashboard shows it, with the sentence the model is given.

    ``asked_as`` is rendered here rather than stored: it is a function of the
    parameters, and a stored copy would be the version that was true when the
    rule was saved. Showing it beside the owner's own message is what makes a
    rule that does not measure what its author meant visible now rather than in
    a letter three weeks later.
    """
    return RuleRead(
        id=rule.id,
        kind=rule.kind,
        scope=rule.scope,
        severity=rule.severity,
        params=rule.params,
        message=rule.message,
        is_active=rule.is_active,
        is_builtin=rule.is_builtin,
        asked_as=workshop_prompt.describe(rule.params),
    )


# ── reference documents ───────────────────────────────────────────────


@router.get(
    "/references",
    response_model=list[ReferenceRead],
    summary="Documents kept as examples of shape",
)
async def list_references(
    session: Annotated[AsyncSession, Depends(get_session)],
    kind: ReferenceKind | None = None,
) -> list[ReferenceRead]:
    """Every reference, newest first. Inactive ones included, and marked."""
    rows = await store.references(session, kind=kind)
    return [_reference_read(row) for row in rows]


@router.get(
    "/references/{reference_id}",
    response_model=ReferenceDetail,
    summary="One reference, with its text",
)
async def read_reference(
    reference_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ReferenceDetail:
    """The whole document, for reading and for deciding whether to keep it."""
    row = await store.get_reference(session, reference_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference not found")
    return _reference_detail(row)


@router.post(
    "/references",
    status_code=status.HTTP_201_CREATED,
    response_model=ReferenceCreated,
    summary="Keep a document as an example of shape",
)
async def create_reference(
    session: Annotated[AsyncSession, Depends(get_session)],
    kind: Annotated[ReferenceKind, Form(description="cv or cover_letter")],
    title: Annotated[str, Form(min_length=1, max_length=MAX_TITLE_CHARS)],
    note: Annotated[str | None, Form(max_length=MAX_NOTE_CHARS)] = None,
    text: Annotated[str | None, Form(description="The document, pasted.")] = None,
    file: Annotated[UploadFile | None, File(description="PDF, DOCX, TXT or Markdown.")] = None,
) -> ReferenceCreated:
    """Store a reference, from a file or from pasted text — exactly one of them.

    201 rather than 202: unlike a resume there is no background work. The text is
    extracted in the request, and the extraction is the whole of the processing,
    so a caller that gets a 201 has a reference that is already in use.

    The response carries the extractor's warnings. That is the point of the
    envelope: "this PDF is laid out in two columns and came out interleaved" has
    to reach the person while they still have the file open and can paste the
    text instead.
    """
    if (file is None) == (text is None or not text.strip()):
        raise ParsingError(
            "send either a file or the text, and not both: they are two ways of "
            "giving the same document and only one of them can be the one stored"
        )

    warnings: tuple[str, ...] = ()
    source_filename: str | None = None
    source_format: str | None = None
    size_bytes: int | None = None

    if file is not None:
        # Bounded read, like the resume endpoint: an UploadFile is a stream, and
        # reading it whole before checking the size would let the upload decide
        # this process's memory use.
        limit = settings.resume_max_file_size_mb * 1024 * 1024
        content = await file.read(limit + 1)
        if len(content) > limit:
            raise ParsingError(
                f"file is larger than the {settings.resume_max_file_size_mb} MB limit"
            )
        extracted = reference_documents.extract(content, file.filename or "reference")
        body = extracted.text
        warnings = extracted.warnings
        source_filename = file.filename
        source_format = extracted.source_format
        size_bytes = extracted.size_bytes
    else:
        # Not None: the exclusive-or above rejects the case where both are absent.
        body = reference_documents.accept_text(text or "")

    row = await service.store_reference(
        session,
        kind=kind,
        title=title,
        note=note,
        text=body,
        source_filename=source_filename,
        source_format=source_format,
        size_bytes=size_bytes,
    )
    await session.commit()
    return ReferenceCreated(reference=_reference_detail(row), warnings=warnings)


@router.patch(
    "/references/{reference_id}",
    response_model=ReferenceDetail,
    summary="Rename a reference, re-note it, or switch it off",
)
async def patch_reference(
    reference_id: UUID,
    changes: ReferenceUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ReferenceDetail:
    """Correct the fields around the document. Never the document."""
    row = await store.get_reference(session, reference_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference not found")
    updated = await service.update_reference(session, row, changes)
    await session.commit()
    return _reference_detail(updated)


@router.delete(
    "/references/{reference_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Forget a reference",
)
async def remove_reference(
    reference_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Delete it. Nothing points at it, so nothing else changes."""
    row = await store.get_reference(session, reference_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference not found")
    await service.delete_reference(session, row)
    await session.commit()


# ── rules ─────────────────────────────────────────────────────────────


@router.get("/rules", response_model=list[RuleRead], summary="Every rule, built-in first")
async def list_rules(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[RuleRead]:
    """The editing view: built-ins, then the owner's, active and inactive alike.

    A rule that is switched off is exactly the one somebody is looking for when
    they wonder why nothing is being checked, so it is in the list and marked
    rather than filtered out of it.
    """
    return [_rule_read(rule) for rule in await service.rules_for_display(session)]


@router.post(
    "/rules",
    status_code=status.HTTP_201_CREATED,
    response_model=RuleRead,
    summary="Add a rule",
)
async def add_rule(
    payload: RuleCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RuleRead:
    """Save a rule, or refuse it.

    A rule that would have a document claim experience the profile does not list
    is refused here with 422 and the names that caused it — see
    ``app/workshop/truth.py``. Rules describe form; facts come from the resume.
    """
    row = await service.create_rule(session, payload)
    await session.commit()
    return _rule_read(store.spec_of(row))


@router.patch("/rules/{rule_id}", response_model=RuleRead, summary="Change a rule")
async def patch_rule(
    rule_id: UUID,
    changes: RuleUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RuleRead:
    """Edit a stored rule, through the same refusal a new one passes."""
    row = await store.get_rule(session, rule_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")
    updated = await service.update_rule(session, row, changes)
    await session.commit()
    return _rule_read(store.spec_of(updated))


@router.delete(
    "/rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a rule",
)
async def remove_rule(
    rule_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Delete a rule of the owner's.

    A built-in has no UUID, so it never reaches this handler: the path parameter
    rejects ``builtin:no_links`` before routing gets here.
    """
    row = await store.get_rule(session, rule_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")
    await service.delete_rule(session, row)
    await session.commit()


# ── the trial letter ──────────────────────────────────────────────────


@router.get(
    "/vacancies",
    response_model=list[VacancyChoice],
    summary="Vacancies a trial letter can be written for",
)
async def choosable_vacancies(
    session: Annotated[AsyncSession, Depends(get_session)],
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[VacancyChoice]:
    """The picker behind the preview button, and nothing more.

    Deliberately minimal and deliberately here: the dashboard's vacancy list is
    phase 7's, and this exists so the workshop can say «на любой вакансии из
    базы» today. When that list lands this endpoint goes, rather than sitting
    beside it disagreeing about what "active" means.
    """
    rows = await store.choosable_vacancies(session, query=q, limit=limit)
    return [VacancyChoice.model_validate(row) for row in rows]


@router.post(
    "/preview",
    response_model=PreviewResponse,
    summary="Write a trial letter for one vacancy under the current rules",
)
async def preview(
    payload: PreviewRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PreviewResponse:
    """Generate and return a letter. Nothing is saved.

    A refusal — every hard rule could not be met — comes back as a 200 whose
    ``written`` is false and whose ``broken_rules`` name what stopped it. That is
    an answer to the question the owner asked, not a failure of their request,
    and rendering it as an error would throw away the list that makes it useful.
    """
    try:
        return await service.preview_letter(
            session, vacancy_id=payload.vacancy_id, profile_id=payload.profile_id
        )
    except service.VacancyNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Vacancy not found"
        ) from None
    except service.ProfileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            # Deliberately distinct from the vacancy's 404: "there is no resume
            # to write from" is fixed by uploading one, and a client that cannot
            # tell the two apart tells its user to look for the wrong thing.
            detail="No resume to write from: upload one first",
        ) from None
