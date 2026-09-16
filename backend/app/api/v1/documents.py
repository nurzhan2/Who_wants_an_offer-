"""Generating, listing and downloading documents written for a vacancy.

The two buttons on a vacancy card are two POSTs here. Both answer with the
document *and* its audit, because the brief requires the report to travel with
the document rather than be available near it — a client cannot render one
without having been handed the other.

**Generation is synchronous here and asynchronous to the user.** The endpoints
do the work in the request, which takes as long as the model does; the dashboard
does not block on that, because it issues the request through TanStack Query and
shows the mutation's own pending state. That is the smallest thing that is
honestly asynchronous: a background task would need a job row to poll, and this
project already has one background pipeline (``app/services/resume.py``) whose
docstring says exactly why it exists — a resume upload has to answer with an id
before parsing starts, because the client has nothing else to show. A document
request has something to show: the button it came from.

**Nothing here sends an application.** These endpoints generate, audit, store
and hand over files. Sending is ``agent/``'s, from a browser, under the user's
own account, after a human confirms it — and per ``prompts/11-dashboard.md``
there is no send button in the dashboard at all, deliberately.
"""

from decimal import Decimal
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import DocumentKind, RuleScope
from app.db.session import get_session
from app.documents import rules, service
from app.documents import store as document_store
from app.documents.render import MEDIA_TYPE
from app.documents.service import DocumentOutcome
from app.letters import store as letter_store
from app.letters.context import ProfileFacts
from app.schemas.dashboard import Documents, LetterRequest, QueuedLetter, WorkshopResult
from app.schemas.document import (
    DocumentCandidateRead,
    DocumentReviewRead,
    DocumentSummaryRead,
    DocumentVersionRead,
    GeneratedDocumentRead,
    RequirementCoverageRead,
)
from app.services import documents as documents_service
from app.services import workshop as workshop_service
from app.workshop import store as workshop_store

router = APIRouter(prefix="/documents", tags=["documents"])

#: What the browser is told to call the file. RFC 5987, because a Kazakh CV's
#: filename is Cyrillic and a bare ``filename=`` header cannot carry one.
_DISPOSITION = "attachment; filename=\"{ascii}\"; filename*=UTF-8''{quoted}"


async def _profile(session: AsyncSession, profile_id: UUID | None) -> ProfileFacts:
    """The named profile or the active one, as a 404 rather than a None.

    Every endpoint below needs the same lookup and the same refusal, and a
    missing profile is not an odd case here: the two buttons live on a vacancy,
    and a vacancy screen is reachable before a resume has ever been uploaded.
    """
    profile = await letter_store.load_profile_facts(session, profile_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active candidate profile; upload a resume first",
        )
    return profile


def _to_read(outcome: DocumentOutcome) -> GeneratedDocumentRead:
    """One outcome as the API contract, delivered or withheld.

    A withheld outcome keeps its review when the audit is what withheld it, so
    the screen that explains the refusal can show the findings that caused it
    rather than a sentence about them.
    """
    document = outcome.document
    review = document.review if document is not None else outcome.withheld_review
    return GeneratedDocumentRead(
        vacancy_id=outcome.vacancy_id,
        kind=outcome.kind,
        delivered=outcome.delivered,
        document_id=outcome.stored_id,
        version=outcome.version,
        source=outcome.source,
        filename=document.filename if document else None,
        file_format=document.file_format if document else None,
        text=document.text if document else None,
        review=(
            DocumentReviewRead(
                ats=review.ats,
                coverage=RequirementCoverageRead(
                    named=list(review.coverage.named),
                    held_but_unnamed=list(review.coverage.held_but_unnamed),
                    not_held=list(review.coverage.not_held),
                    inferred=list(review.coverage.inferred),
                    literal_coverage=review.coverage.literal_coverage,
                ),
            )
            if review is not None
            else None
        ),
        reason=outcome.reason,
        reason_ru=outcome.reason_ru,
        problems=list(outcome.problems),
        hard_rules=list(outcome.hard_rules),
    )


@router.post(
    "/cv/{vacancy_id}",
    response_model=GeneratedDocumentRead,
    summary="Generate a CV tailored to one vacancy",
)
async def generate_cv(
    vacancy_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    profile_id: Annotated[UUID | None, Query()] = None,
) -> GeneratedDocumentRead:
    """Arrange, check, render, audit and store a CV for this vacancy.

    Always a new version; nothing is ever overwritten. 200 rather than 201 even
    when a row is created, because the useful part of the answer is the audit
    and the text, and a client that only followed a Location header would have
    to fetch them separately to find out whether the document was withheld.
    """
    profile = await _profile(session, profile_id)
    outcome = await service.write_cv(session, vacancy_id, profile)
    await session.commit()
    return _to_read(outcome)


@router.post(
    "/cover-letter/{vacancy_id}",
    response_model=GeneratedDocumentRead,
    summary="Generate a cover letter for one vacancy",
)
async def generate_cover_letter(
    vacancy_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    profile_id: Annotated[UUID | None, Query()] = None,
) -> GeneratedDocumentRead:
    """Write the letter through ``app.letters``, then file, audit and version it.

    The letter also lands in ``application.cover_letter`` as it always has, which
    is where the sending agent reads it from. This endpoint adds the file, the
    audit and the version history; it does not move the letter anywhere.
    """
    profile = await _profile(session, profile_id)
    outcome = await service.write_cover_letter(session, vacancy_id, profile)
    await session.commit()
    return _to_read(outcome)


@router.get(
    "",
    response_model=list[DocumentSummaryRead],
    summary="Everything generated for this profile, newest first",
)
async def list_documents(
    session: Annotated[AsyncSession, Depends(get_session)],
    profile_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[DocumentSummaryRead]:
    """List stored versions without their bodies. See ``store.recent``."""
    profile = await _profile(session, profile_id)
    rows = await document_store.recent(session, profile_id=profile.profile_id, limit=limit)
    return [
        DocumentSummaryRead(
            id=row.id,
            vacancy_id=row.vacancy_id,
            vacancy_title=row.vacancy_title,
            company=row.company,
            kind=row.kind,
            version=row.version,
            rules_version=row.rules_version,
            source=row.source,
            ats_score=row.ats_score,
            ats_overall=row.ats_overall,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get(
    "/versions/{vacancy_id}/{kind}",
    response_model=list[DocumentVersionRead],
    summary="Every version of one document for one vacancy, oldest first",
)
async def list_versions(
    vacancy_id: UUID,
    kind: DocumentKind,
    session: Annotated[AsyncSession, Depends(get_session)],
    profile_id: Annotated[UUID | None, Query()] = None,
) -> list[DocumentVersionRead]:
    """The history the versioning exists for.

    Oldest first, because what the owner is reading it to see is what changed
    from one version to the next after they edited the rules — and
    ``rules_version`` on each row is what says whether the rules are the reason.
    """
    profile = await _profile(session, profile_id)
    rows = await document_store.versions(
        session, profile_id=profile.profile_id, vacancy_id=vacancy_id, kind=kind
    )
    return [
        DocumentVersionRead(
            id=row.id,
            vacancy_id=row.vacancy_id,
            kind=row.kind,
            version=row.version,
            rules_version=row.rules_version,
            source=row.source,
            ats_score=row.ats_report.score,
            ats_overall=row.ats_report.overall.value,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get(
    "/candidates",
    response_model=list[DocumentCandidateRead],
    summary="Vacancies the two buttons appear on, best-scoring first",
)
async def list_candidates(
    session: Annotated[AsyncSession, Depends(get_session)],
    profile_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    min_score: Annotated[Decimal, Query(ge=0, le=100)] = Decimal("0"),
) -> list[DocumentCandidateRead]:
    """Scored vacancies with a count of the documents each already has.

    ``min_score`` defaults to zero rather than to a threshold: the owner asked
    for buttons on every vacancy, and a default that hid the low-scoring ones
    would be this endpoint deciding which vacancies are worth applying to.
    """
    profile = await _profile(session, profile_id)
    rows = await document_store.candidates(
        session, profile_id=profile.profile_id, limit=limit, min_score=min_score
    )
    return [
        DocumentCandidateRead(
            vacancy_id=row.vacancy_id,
            title=row.title,
            company=row.company,
            score=row.score,
            cv_versions=row.cv_versions,
            letter_versions=row.letter_versions,
            employer=row.employer,
            via_agent=row.via_agent,
            source_slug=row.source_slug,
            url=row.url,
        )
        for row in rows
    ]


@router.get(
    "/rules",
    response_model=list[str],
    summary="The hard rules a generated document is held to",
)
async def list_rules(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[str]:
    """What a document has to satisfy before it is handed over.

    A list of sentences rather than a rule model, and not a duplicate of the
    workshop's ``/workshop/rules``: that one is the form the owner edits, this
    one is the set a refusal is explained by, and it carries what the form
    cannot — the structural guarantees, which are code and have no row to edit.
    The workshop's hard CV rules are in here too, in the owner's own words.
    """
    return list(rules.describe(await workshop_store.active_rules(session, scope=RuleScope.CV)))


@router.get(
    "/{document_id}/file",
    response_class=Response,
    summary="Download one stored version as a file",
)
async def download(
    document_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Rebuild the file from the stored arrangement and send it.

    A re-render rather than a stored blob — see
    :func:`app.documents.service.rebuild` for why a CV that keeps asserting a job
    the resume no longer claims is the worse of the two failure modes.
    """
    document = await service.rebuild(session, document_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such generated document"
        )
    return Response(
        content=document.content,
        media_type=MEDIA_TYPE,
        headers={"Content-Disposition": _disposition(document.filename)},
    )


def _disposition(filename: str) -> str:
    """A Content-Disposition that survives a Cyrillic filename.

    Both forms, as RFC 6266 recommends: an ASCII fallback for anything that
    cannot read the extended one, and the percent-encoded UTF-8 name for
    everything that can. Without the second, a CV called
    ``Нуржан-Kaspi.docx`` arrives as a row of question marks.
    """
    ascii_name = filename.encode("ascii", "ignore").decode("ascii") or "document.docx"
    return _DISPOSITION.format(ascii=ascii_name, quoted=quote(filename, safe=""))


@router.get(
    "/overview",
    response_model=Documents,
    summary="Uploaded resumes with their ATS audit, and every letter written",
)
async def read_documents(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Documents:
    """The uploaded side of the screen, and every letter, with what became of each.

    Not the generated documents: those are ``GET ""`` above, which lists what
    this project wrote per vacancy with its rules version and its audit. This
    endpoint was written when the CV generator was on another branch and its
    docstring said no such thing existed; both are true at once now, and they
    answer different questions — "what did I upload and what did I send" against
    "what did the system produce for this vacancy".

    What is shown for an uploaded resume is the file and the readability audit
    taken when it landed: what an employer's parser sees, and what it loses.

    Letters carry the three dates the feedback loop is made of (written, sent,
    answered), the rules version that judged each one, and what today's rules
    make of the same text. A letter that passed when it was written and does not
    now is the most interesting row on the screen.
    """
    return await documents_service.build(session)


@router.get(
    "/queue",
    response_model=list[QueuedLetter],
    summary="Vacancies worth a letter, best first",
)
async def read_queue(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = workshop_service.DEFAULT_QUEUE_LIMIT,
    min_score: Annotated[Decimal | None, Query(ge=0, le=100)] = workshop_service.DEFAULT_MIN_SCORE,
) -> list[QueuedLetter]:
    """What the workshop offers to write next.

    Includes vacancies whose letter is already written, flagged as such: the
    batch writer skips those so a repeated run costs nothing, but a person
    looking at a queue needs to see that one is done rather than wonder where it
    went. Never includes a filtered vacancy, whatever its score, and without
    ``min_score`` starts at ``agent_queue_min_score``.
    """
    return await workshop_service.queue(session, limit=limit, min_score=min_score)


@router.post(
    "/letters",
    response_model=WorkshopResult,
    summary="Write the letter for one vacancy",
)
async def write_letter(
    payload: LetterRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WorkshopResult:
    """Generate a letter and save it on the tracker row. Never send it.

    **The only write the dashboard makes, and it produces a document.** Sending
    is ``wwao apply --send``, where the same letter is printed on a confirmation
    card and a person at the keyboard says yes to that letter for that vacancy.
    There is no send button here and there is not meant to be: a browser cannot
    make the promise the confirmation exists to make.

    201 when a letter was written and saved, 200 when nothing was: the vacancy
    is gone, a letter already exists and ``force`` was not set, or nothing could
    be written that passes the checks. All three are answers rather than errors,
    and each names itself in ``skipped`` — a 4xx would make a screen show a
    failure where the honest report is "there was already one".

    A missing active profile is a 409 rather than a skip. Every other outcome is
    about this vacancy; that one is about the installation, and it stays true
    for every vacancy until somebody uploads a resume.
    """
    result = await workshop_service.write(session, payload.vacancy_id, force=payload.force)
    if result.skipped == workshop_service.NO_PROFILE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No active profile: upload a resume before generating letters.",
        )
    if result.saved:
        response.status_code = status.HTTP_201_CREATED
    return result
