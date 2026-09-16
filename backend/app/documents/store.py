"""Reading the rows a document is built from, and writing the versions back.

Every query is explicit about its columns, for the reason the letter store gives:
``Vacancy.matches``, ``Vacancy.applications``, ``Vacancy.documents`` and
``Match.vacancy`` are all ``lazy="raise"``, so the models turn an accidental N+1
into an error rather than into a slow afternoon.

The queries live in this package rather than under ``db/repositories/`` on the
same principle the letters package applies: they answer one feature's questions
and nothing else asks them.

**Version numbers are assigned under the unique constraint, not around it.**
``uq_generated_document_profile_id_vacancy_id_kind_version`` is what actually
makes "a regeneration adds a version" true; :func:`next_version` reads the
current maximum and the constraint rejects the loser if two generations race.
Reading the maximum and trusting it would be a check-then-act that quietly
overwrites under concurrency, which is precisely the thing the whole versioning
requirement exists to prevent.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.base import uuid7
from app.db.enums import DocumentKind, DocumentSource, MatchBucket
from app.db.models import (
    CandidateProfile,
    GeneratedDocument,
    Match,
    ProfileExperience,
    Vacancy,
    VacancySource,
)
from app.db.seed_rows import seed_only
from app.documents.context import (
    EducationEntry,
    ExperienceEntry,
    education_from_rows,
    experience_from_rows,
)
from app.documents.employer import EmployerSignals, from_raw
from app.letters import channel
from app.schemas.ats import ATSReport

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DocumentRow:
    """One stored version, as everything outside this module reads it."""

    id: UUID
    profile_id: UUID
    vacancy_id: UUID
    kind: DocumentKind
    version: int
    payload: dict[str, Any]
    text: str
    file_format: str
    ats_report: ATSReport
    rules_version: str
    source: DocumentSource
    problems: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DocumentSummary:
    """One stored version without its body, for a list.

    A documents screen shows twenty rows and needs the score and the date on
    each; it does not need twenty CVs. Keeping the text out of the list query is
    the difference between a page and a megabyte.
    """

    id: UUID
    vacancy_id: UUID
    vacancy_title: str
    company: str | None
    kind: DocumentKind
    version: int
    rules_version: str
    source: DocumentSource
    ats_score: int
    ats_overall: str
    created_at: datetime


async def load_experience(session: AsyncSession, profile_id: UUID) -> tuple[ExperienceEntry, ...]:
    """The profile's jobs, in the order the resume gave them.

    Empty is a real answer and not an error: a profile parsed before migration
    ``0010`` has no rows, because the extraction that would have filled them ran
    before the table existed. The service reports that as a reason it cannot
    write a CV rather than writing one with no experience section, which would
    be a document that misrepresents a career by omission.
    """
    rows = (
        await session.scalars(
            select(ProfileExperience)
            .where(ProfileExperience.profile_id == profile_id)
            .order_by(ProfileExperience.position)
        )
    ).all()
    return experience_from_rows(
        [
            {
                "position": row.position,
                "company": row.company,
                "title": row.title,
                "start": row.start,
                "end": row.end,
                "is_current": row.is_current,
                "stack": row.stack,
                "domains": row.domains,
            }
            for row in rows
        ]
    )


async def load_education(session: AsyncSession, profile_id: UUID) -> tuple[EducationEntry, ...]:
    """The profile's degrees, from the JSONB column that holds them."""
    stored = await session.scalar(
        select(CandidateProfile.education).where(CandidateProfile.id == profile_id)
    )
    return education_from_rows(stored)


async def load_resume_text(session: AsyncSession, profile_id: UUID) -> str | None:
    """The resume as the extractor read it.

    Loaded on its own rather than with the profile because of what it is: the
    document the owner's phone number and email address live in, read by
    :mod:`app.documents.contacts` and by nothing else. A separate query is one
    fewer place the string can be picked up by accident — and it is a column
    nothing else in this feature wants.
    """
    return await session.scalar(
        select(CandidateProfile.raw_text).where(CandidateProfile.id == profile_id)
    )


async def next_version(
    session: AsyncSession, *, profile_id: UUID, vacancy_id: UUID, kind: DocumentKind
) -> int:
    """The number the next document of this kind for this pair gets.

    One past the current maximum, or 1 when there is none. Correctness under
    concurrency comes from the unique constraint rather than from here — see the
    module docstring.
    """
    highest = await session.scalar(
        select(func.max(GeneratedDocument.version))
        .where(GeneratedDocument.profile_id == profile_id)
        .where(GeneratedDocument.vacancy_id == vacancy_id)
        .where(GeneratedDocument.kind == kind)
    )
    return int(highest or 0) + 1


async def save(
    session: AsyncSession,
    *,
    profile_id: UUID,
    vacancy_id: UUID,
    kind: DocumentKind,
    payload: dict[str, Any],
    text: str,
    file_format: str,
    ats_report: ATSReport,
    rules_version: str,
    source: DocumentSource,
    problems: tuple[str, ...] = (),
) -> DocumentRow:
    """Store a new version. Nothing here can update an existing one.

    There is no ``force`` and no upsert, and the omission is the feature: the
    brief requires that regenerating shows the owner what changed, which is only
    possible while the previous answer is still there to compare against.
    """
    version = await next_version(session, profile_id=profile_id, vacancy_id=vacancy_id, kind=kind)
    row = GeneratedDocument(
        id=uuid7(),
        profile_id=profile_id,
        vacancy_id=vacancy_id,
        kind=kind,
        version=version,
        payload=payload,
        text=text,
        file_format=file_format,
        ats_report=ats_report.model_dump(mode="json"),
        rules_version=rules_version,
        source=source,
        problems=list(problems),
    )
    session.add(row)
    await session.flush()
    logger.info(
        "documents.stored",
        document_id=str(row.id),
        profile_id=str(profile_id),
        vacancy_id=str(vacancy_id),
        kind=kind.value,
        version=version,
        source=source.value,
        rules_version=rules_version,
        ats_score=ats_report.score,
        characters=len(text),
    )
    return _to_row(row)


async def latest(
    session: AsyncSession, *, profile_id: UUID, vacancy_id: UUID, kind: DocumentKind
) -> DocumentRow | None:
    """The most recent version of this kind for this pair, or None."""
    row = await session.scalar(
        select(GeneratedDocument)
        .where(GeneratedDocument.profile_id == profile_id)
        .where(GeneratedDocument.vacancy_id == vacancy_id)
        .where(GeneratedDocument.kind == kind)
        .order_by(GeneratedDocument.version.desc())
        .limit(1)
    )
    return _to_row(row) if row is not None else None


async def by_id(session: AsyncSession, document_id: UUID) -> DocumentRow | None:
    """One stored version by its own id, for a download or a comparison."""
    row = await session.get(GeneratedDocument, document_id)
    return _to_row(row) if row is not None else None


async def versions(
    session: AsyncSession, *, profile_id: UUID, vacancy_id: UUID, kind: DocumentKind
) -> list[DocumentRow]:
    """Every version of this kind for this pair, oldest first.

    Oldest first because that is the order they were written in, and the thing
    the owner is reading this list to see is what changed between one and the
    next.
    """
    rows = (
        await session.scalars(
            select(GeneratedDocument)
            .where(GeneratedDocument.profile_id == profile_id)
            .where(GeneratedDocument.vacancy_id == vacancy_id)
            .where(GeneratedDocument.kind == kind)
            .order_by(GeneratedDocument.version)
        )
    ).all()
    return [_to_row(row) for row in rows]


async def recent(
    session: AsyncSession, *, profile_id: UUID, limit: int = 50
) -> list[DocumentSummary]:
    """Everything generated for this profile, newest first, without the bodies."""
    rows = (
        await session.execute(
            select(
                GeneratedDocument.id,
                GeneratedDocument.vacancy_id,
                Vacancy.title,
                Vacancy.company,
                GeneratedDocument.kind,
                GeneratedDocument.version,
                GeneratedDocument.rules_version,
                GeneratedDocument.source,
                GeneratedDocument.ats_report,
                GeneratedDocument.created_at,
            )
            .join(Vacancy, Vacancy.id == GeneratedDocument.vacancy_id)
            .where(GeneratedDocument.profile_id == profile_id)
            .order_by(GeneratedDocument.created_at.desc(), GeneratedDocument.id.desc())
            .limit(limit)
        )
    ).all()
    summaries: list[DocumentSummary] = []
    for row in rows:
        report = ATSReport.model_validate(row.ats_report)
        summaries.append(
            DocumentSummary(
                id=row.id,
                vacancy_id=row.vacancy_id,
                vacancy_title=row.title,
                company=row.company,
                kind=row.kind,
                version=row.version,
                rules_version=row.rules_version,
                source=row.source,
                ats_score=report.score,
                ats_overall=report.overall.value,
                created_at=row.created_at,
            )
        )
    return summaries


@dataclass(frozen=True, slots=True)
class Candidate:
    """One vacancy a document could be generated for, with what it already has."""

    vacancy_id: UUID
    title: str
    company: str | None
    score: Decimal
    cv_versions: int
    letter_versions: int
    #: What the employer published about themselves on this posting. Never a
    #: lookup: see :mod:`app.documents.employer`.
    employer: EmployerSignals = field(default_factory=EmployerSignals)
    #: How it is applied to — see :mod:`app.letters.channel`.
    via_agent: bool = False
    source_slug: str = ""
    url: str = ""


async def candidates(
    session: AsyncSession,
    *,
    profile_id: UUID,
    limit: int = 25,
    min_score: Decimal = Decimal("0"),
) -> list[Candidate]:
    """The best-scoring vacancies, each with how many documents it already has.

    The list the two buttons live on. Counting the existing versions here rather
    than per row is what keeps the screen one query: a list of twenty vacancies
    that each ask "and how many CVs does this one have" is the N+1 the models'
    ``lazy="raise"`` exists to make impossible to write by accident.
    """
    cv_count = (
        select(func.count())
        .select_from(GeneratedDocument)
        .where(GeneratedDocument.vacancy_id == Match.vacancy_id)
        .where(GeneratedDocument.profile_id == profile_id)
        .where(GeneratedDocument.kind == DocumentKind.CV)
        .scalar_subquery()
    )
    letter_count = (
        select(func.count())
        .select_from(GeneratedDocument)
        .where(GeneratedDocument.vacancy_id == Match.vacancy_id)
        .where(GeneratedDocument.profile_id == profile_id)
        .where(GeneratedDocument.kind == DocumentKind.COVER_LETTER)
        .scalar_subquery()
    )
    rows = (
        await session.execute(
            select(
                Match.vacancy_id,
                Vacancy.title,
                Vacancy.company,
                Match.score,
                cv_count.label("cv_versions"),
                letter_count.label("letter_versions"),
                channel.on_agent_source(Match.vacancy_id).label("via_agent"),
                channel.primary_slug(Match.vacancy_id).label("source_slug"),
                channel.primary_url(Match.vacancy_id).label("url"),
            )
            .join(Vacancy, Vacancy.id == Match.vacancy_id)
            .where(Match.profile_id == profile_id)
            .where(Match.score >= min_score)
            # A filtered vacancy is "do not apply"; a CV or a letter for it is
            # a model call spent on an application that must not go out.
            .where(Match.bucket != MatchBucket.FILTERED)
            # Same for a seeded vacancy: its address is example.test.
            .where(~seed_only(Match.vacancy_id))
            .where(Vacancy.is_active.is_(True))
            .where(Vacancy.is_spam.is_(False))
            .order_by(Match.score.desc(), Match.vacancy_id)
            .limit(limit)
        )
    ).all()
    signals = await _employer_signals(session, [row.vacancy_id for row in rows])
    return [
        Candidate(
            vacancy_id=row.vacancy_id,
            title=row.title,
            company=row.company,
            score=row.score,
            cv_versions=int(row.cv_versions),
            letter_versions=int(row.letter_versions),
            employer=signals.get(row.vacancy_id, EmployerSignals()),
            via_agent=bool(row.via_agent),
            source_slug=row.source_slug or "",
            url=row.url or "",
        )
        for row in rows
    ]


async def _employer_signals(
    session: AsyncSession, vacancy_ids: list[UUID]
) -> dict[UUID, EmployerSignals]:
    """What each of these vacancies' employers published, in one more query.

    One query for the whole page rather than one per row. A vacancy can be held
    under several postings after deduplication, so the payloads are grouped here
    and read together — and ``Vacancy.sources`` is ``lazy="selectin"``, which
    would have made the obvious loop a hidden query per row.
    """
    if not vacancy_ids:
        return {}
    rows = (
        await session.execute(
            select(VacancySource.vacancy_id, VacancySource.raw).where(
                VacancySource.vacancy_id.in_(vacancy_ids)
            )
        )
    ).all()
    grouped: dict[UUID, list[dict[str, Any]]] = {}
    for row in rows:
        if isinstance(row.raw, dict):
            grouped.setdefault(row.vacancy_id, []).append(row.raw)
    return {vacancy_id: from_raw(raws) for vacancy_id, raws in grouped.items()}


def _to_row(row: GeneratedDocument) -> DocumentRow:
    """One ORM row as the dataclass the rest of the feature passes around."""
    return DocumentRow(
        id=row.id,
        profile_id=row.profile_id,
        vacancy_id=row.vacancy_id,
        kind=row.kind,
        version=row.version,
        payload=dict(row.payload),
        text=row.text,
        file_format=row.file_format,
        ats_report=ATSReport.model_validate(row.ats_report),
        rules_version=row.rules_version,
        source=row.source,
        problems=tuple(str(item) for item in row.problems),
        created_at=row.created_at,
    )
