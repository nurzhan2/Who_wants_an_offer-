"""Reading the state of the source layer, and starting a run.

The status of a source is assembled rather than stored. There is no ``source``
table and deliberately none was added: enablement in a row is a change no code
review ever sees, and every fact the page needs already exists somewhere —
the class declares its limits, ``pipeline_run`` records what happened,
``source_quota`` records what was spent.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.repositories.pipeline_run import PipelineRunRepository
from app.db.repositories.source_quota import SourceQuotaRepository
from app.pipeline.runner import RunReport
from app.schemas.pipeline import PipelineRunRead
from app.schemas.source import (
    EmbeddingSummary,
    PlanSummary,
    RunResponse,
    SourceRunSummary,
    SourcesResponse,
    SourceStatus,
)
from app.sources.base import BaseSource, SourceUnavailable, Unavailable
from app.sources.registry import all_sources, disabled_reason, import_errors

logger = get_logger(__name__)


async def list_sources(session: AsyncSession) -> SourcesResponse:
    """Every registered source with its limits, its last run and why it is idle."""
    sources = all_sources()
    runs = PipelineRunRepository(session)
    quotas = SourceQuotaRepository(session)
    latest = {run.source_slug: run for run in await runs.latest_per_source()}

    statuses: list[SourceStatus] = []
    for source in sources:
        reason = await _reason(source, quotas)
        last = latest.get(source.slug)
        statuses.append(
            SourceStatus(
                slug=source.slug,
                name=source.name,
                regions=source.regions,
                access_mode=source.access_mode,
                terms_url=source.terms_url,
                attribution=source.attribution,
                requests_per_second=source.rate_limit.requests_per_second,
                daily_quota=source.daily_quota,
                daily_used=await quotas.used_today(source.slug),
                min_interval=source.min_interval,
                enabled=reason is None,
                inactive=reason,
                last_run=PipelineRunRead.model_validate(last) if last is not None else None,
            )
        )
    return SourcesResponse(sources=statuses, import_errors=import_errors())


async def _reason(source: BaseSource, quotas: SourceQuotaRepository) -> Unavailable | None:
    """Config, credentials, then the daily allowance.

    The credential branch returns the source's own answer unchanged, which
    carries the missing key NAMES and nothing else.
    """
    reason = disabled_reason(source)
    if reason is not None:
        return reason
    remaining = await quotas.remaining(source.slug, source.daily_quota)
    if remaining is not None and remaining <= 0:
        return Unavailable(
            code=SourceUnavailable.QUOTA_EXHAUSTED,
            detail=(
                f"Дневной лимит источника «{source.name}» исчерпан "
                f"({source.daily_quota} запросов). Сбрасывается в полночь UTC."
            ),
        )
    return None


def to_response(report: RunReport) -> RunResponse:
    """Turn a run report into the API's view of it."""
    return RunResponse(
        dry_run=report.dry_run,
        started_at=report.started_at,
        duration_seconds=round(report.duration_seconds, 3),
        plan=PlanSummary(
            queries=len(report.plan.queries),
            groups=report.plan.groups,
            placements=report.plan.placements,
            collapsed=report.plan.collapsed,
            dropped=report.plan.dropped,
            limit=report.plan.limit,
        ),
        sources=[
            SourceRunSummary(
                slug=outcome.slug,
                found=outcome.found,
                new=outcome.new,
                updated=outcome.updated,
                duplicates=outcome.duplicates,
                requests=outcome.requests,
                duration_seconds=round(outcome.duration_seconds, 3),
                errors=list(outcome.errors),
                skipped=outcome.skipped,
            )
            for outcome in report.sources
        ],
        embedding=(
            EmbeddingSummary(
                considered=report.embedding.considered,
                unchanged=report.embedding.unchanged,
                embedded=report.embedding.embedded,
                skipped_reason=report.embedding.skipped_reason,
            )
            if report.embedding is not None
            else None
        ),
        found=report.found,
        new=report.new,
        duplicates=report.duplicates,
    )


async def recent_runs(
    session: AsyncSession, *, source_slug: str | None = None, limit: int = 50
) -> list[PipelineRunRead]:
    """Run history, newest first."""
    runs = await PipelineRunRepository(session).recent(source_slug=source_slug, limit=limit)
    return [PipelineRunRead.model_validate(run) for run in runs]
