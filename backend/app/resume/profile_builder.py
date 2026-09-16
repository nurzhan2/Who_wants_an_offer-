"""Orchestration: an uploaded file becomes a stored, embedded profile.

The whole pipeline in one place, so the order and the failure handling are
readable together:

    extract → LLM (the PDF itself, or text) → enrich → persist → embed

Two things this module is careful about.

**PDFs are handed to the model as documents.** The text pdfplumber produces is
stored for full-text search and never sent to the model, because resumes are
usually two-column and line-oriented extraction reads straight across them.

**Nothing here logs resume content.** Every log line carries the profile id, the
format, sizes and durations. The same goes for ``parse_error``, which reaches
both the API and the logs.
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.db.enums import ParseStatus
from app.db.repositories.profile import ProfileRepository
from app.llm import usage as usage_ledger
from app.llm.base import Document, LLMTask, LLMUsage
from app.llm.router import LLMRouter, get_router
from app.matching import embeddings
from app.resume import ats_audit, enricher
from app.resume.extractor import ExtractedDocument
from app.schemas.llm import ProfileExtraction
from app.schemas.profile import CandidateProfileCreate, ExperienceCreate, SkillCreate

logger = get_logger(__name__)

#: Which task this is. Everything else — provider, model, effort, tool policy —
#: follows from it through configuration, so moving resume extraction between
#: providers never touches this module.
TASK = LLMTask.RESUME_EXTRACTION


@dataclass(frozen=True, slots=True)
class BuildResult:
    """What building a profile produced, for logs and for the report."""

    profile_id: UUID
    status: ParseStatus
    skill_count: int
    total_years: float
    usage: LLMUsage | None
    cost_usd: float | None
    duration_seconds: float
    warnings: tuple[str, ...]


def _documents_for(document: ExtractedDocument) -> tuple[Document, ...]:
    """The file itself, when the model can read it better than we can.

    PDF only. DOCX and plain text have no column problem worth solving, and
    sending them as text keeps the request small. How the file reaches the
    model is the provider's business: the API inlines it as base64, the CLI
    writes it into a directory of its own and reads it.
    """
    if document.source_format == "pdf":
        return (
            Document(content=document.file_bytes, media_type="application/pdf", path="resume.pdf"),
        )
    return ()


def _text_for(document: ExtractedDocument) -> str:
    """The prompt variable carrying the resume, for non-PDF formats.

    For a PDF the text is deliberately absent: pdfplumber reads two-column
    layouts straight across, so its output would be worse than no text at all.
    The file goes to the model instead.
    """
    if document.source_format == "pdf":
        return "(the resume is attached as a file — read it rather than looking for it here)"
    return document.raw_text


async def extract_profile(
    document: ExtractedDocument,
    *,
    router: LLMRouter,
    today: date,
) -> tuple[ProfileExtraction, LLMUsage]:
    """Ask whichever provider serves this task to read the resume."""
    result = await router.complete_json(
        "extract_profile",
        ProfileExtraction,
        task=TASK,
        variables={"today": today.isoformat(), "resume_text": _text_for(document)},
        documents=_documents_for(document),
    )
    return result.value, usage_ledger.record(result.usage)


def to_profile_create(
    extraction: ProfileExtraction,
    enriched: enricher.EnrichedProfile,
    document: ExtractedDocument,
) -> CandidateProfileCreate:
    """Assemble what gets written, taking experience from the computation.

    ``total_years`` comes from ``enriched``, never from
    ``extraction.stated_total_years``. That field records the resume's own
    claim so the two can be compared; using it would reintroduce the very bug
    the dates module exists to prevent.
    """
    return CandidateProfileCreate(
        name=extraction.full_name,
        headline=extraction.headline,
        seniority=enriched.seniority,
        total_years=enriched.total_years,
        summary=extraction.summary,
        locations=[extraction.city] if extraction.city else [],
        relocation=bool(extraction.relocation),
        remote_pref=extraction.remote_pref,
        salary_min=(
            None
            if extraction.salary_expectation is None
            else round(extraction.salary_expectation, 2)
        ),
        salary_currency=extraction.salary_currency,
        languages=[language.model_dump() for language in extraction.languages],
        education=[degree.model_dump() for degree in extraction.education],
        raw_text=document.raw_text,
        skills=[
            SkillCreate(
                canonical_name=skill.canonical_name,
                raw_names=list(skill.raw_names),
                years=skill.years,
                level=skill.level,
                evidence=skill.evidence,
                last_used_year=skill.last_used_year,
            )
            for skill in enriched.skills
        ],
        experience=to_experience(extraction),
    )


def to_experience(extraction: ProfileExtraction) -> list[ExperienceCreate]:
    """The extraction's work periods, as rows, in the order the resume gave them.

    ``position`` is the index in that order and it is the handle a generated CV
    refers to a job by, so it has to be assigned here rather than derived later:
    a stored arrangement names ``ref 2``, and ``ref 2`` has to keep meaning the
    same job for as long as that version is readable.

    A period with neither an employer nor a title is dropped. There is nothing
    to print on the line, and a blank entry in a CV reads to an employer as
    something hidden rather than as something missing.

    Dates are passed through exactly as the extraction normalised them —
    "YYYY-MM", or None where the resume gave none — because this is the last
    place they could be quietly changed and the whole point of storing them is
    that a generated document cannot.
    """
    rows: list[ExperienceCreate] = []
    for period in extraction.work_periods:
        company = period.company.strip()
        title = period.title.strip()
        if not company and not title:
            continue
        rows.append(
            ExperienceCreate(
                position=len(rows),
                company=company[:300],
                title=title[:300],
                start=period.start,
                end=period.end,
                is_current=period.is_current,
                stack=[name.strip() for name in period.stack if name.strip()],
                domains=[name.strip() for name in period.domains if name.strip()],
            )
        )
    return rows


async def _record_ats_report(
    profiles: ProfileRepository,
    profile_id: UUID,
    document: ExtractedDocument,
    extraction: ProfileExtraction,
) -> None:
    """Redo the readability audit now that there is something to compare against.

    Upload could only judge the file itself. This run adds the comparison the
    report exists for: what the model read off the page, against what survives
    into the text layer an employer's parser sees.

    Failure here is logged and dropped. The audit is a diagnostic; losing it
    must not fail a parse that otherwise succeeded, and the profile keeps the
    structural report written at upload rather than nothing.
    """
    try:
        report = await asyncio.to_thread(
            ats_audit.audit,
            document.file_bytes,
            source_format=document.source_format,
            raw_text=document.raw_text,
            extraction=extraction,
        )
    except Exception as exc:
        logger.exception("resume.ats_coverage_failed", error=type(exc).__name__)
        return
    await profiles.set_ats_report(profile_id, report)


async def build_profile(
    document: ExtractedDocument,
    *,
    session: AsyncSession,
    profile_id: UUID,
    router: LLMRouter | None = None,
    today: date | None = None,
) -> BuildResult:
    """Fill in a profile row that already exists in ``pending``.

    The row is created by the upload endpoint so the client has an id to poll
    immediately. Everything here happens in the caller's transaction: a partial
    profile is worse than none, so either the whole thing lands or the row is
    marked failed with a reason.
    """
    started = time.perf_counter()
    today = today or datetime.now(UTC).date()
    profiles = ProfileRepository(session)
    usage: LLMUsage | None = None
    cost: float | None = None

    try:
        extraction, usage = await extract_profile(
            document, router=router or get_router(), today=today
        )
        cost = usage.cost_usd
        enriched = enricher.enrich(extraction, today=today)
        payload = to_profile_create(extraction, enriched, document)

        await profiles.update_from_extraction(profile_id, payload)
        await profiles.replace_skills(profile_id, payload.skills)
        await profiles.replace_experience(profile_id, payload.experience)
        await _record_ats_report(profiles, profile_id, document, extraction)

        vector = await embeddings.encode_profile(
            headline=payload.headline,
            skills=[skill.canonical_name for skill in enriched.skills],
            titles=list(enriched.titles),
            domains=list(enriched.domains),
        )
        await profiles.set_embedding(profile_id, vector)
        # Order matters: activate this one, then retire the rest. The row was
        # created inactive so a half-parsed profile could never be scored
        # against, and this is the only place that undoes that.
        await profiles.activate(profile_id)
        await profiles.inherit_target_titles(profile_id)
        await profiles.deactivate_others(profile_id)
        await profiles.set_parse_status(profile_id, ParseStatus.READY)

    except AppError as exc:
        # Every expected failure in this pipeline is an AppError, and the
        # handler catches the base class rather than a list of subclasses on
        # purpose. Listing ParsingError and LLMError individually silently
        # excluded EmbeddingError — a sibling, not a subclass — so a missing
        # [embeddings] extra left the profile pending for ever with no reason
        # recorded. A new failure mode must not be able to reintroduce that.
        # Anything that is not an AppError is a bug and is re-raised for the
        # caller to log with its traceback.
        await profiles.set_parse_status(profile_id, ParseStatus.FAILED, error=str(exc))
        duration = time.perf_counter() - started
        logger.warning(
            "resume.parse_failed",
            profile_id=str(profile_id),
            reason=type(exc).__name__,
            source_format=document.source_format,
            duration_seconds=round(duration, 2),
            cost_usd=cost,
        )
        return BuildResult(
            profile_id=profile_id,
            status=ParseStatus.FAILED,
            skill_count=0,
            total_years=0.0,
            usage=usage,
            cost_usd=cost,
            duration_seconds=duration,
            warnings=(str(exc),),
        )

    duration = time.perf_counter() - started
    logger.info(
        "resume.parsed",
        profile_id=str(profile_id),
        source_format=document.source_format,
        page_count=document.page_count,
        size_bytes=document.size_bytes,
        skills=len(enriched.skills),
        jobs=len(payload.experience),
        total_years=float(enriched.total_years),
        stated_years_delta=(
            None if enriched.stated_years_delta is None else float(enriched.stated_years_delta)
        ),
        provider=usage.provider if usage else None,
        model=usage.model if usage else None,
        accounting=usage.accounting if usage else None,
        input_tokens=usage.input_tokens if usage else 0,
        output_tokens=usage.output_tokens if usage else 0,
        cost_usd=cost,
        duration_seconds=round(duration, 2),
    )
    return BuildResult(
        profile_id=profile_id,
        status=ParseStatus.READY,
        skill_count=len(enriched.skills),
        total_years=float(enriched.total_years),
        usage=usage,
        cost_usd=cost,
        duration_seconds=duration,
        warnings=enriched.warnings,
    )
