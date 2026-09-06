"""One crawl: plan, fan out across sources, write, then embed once.

The shape follows from three constraints that are easy to state and easy to get
wrong.

**A source that breaks must not break the run.** Every connector is awaited
inside its own guard, its failure lands in ``pipeline_run.errors``, and the run
carries on. That is what the JSONB column is for, and why a run with one broken
source finishes ``partial`` rather than ``failed``: ``last_successful`` counts
partial runs, so marking the whole crawl failed would reset the incremental
watermark and force a full re-crawl next time.

**Each source is bounded by its own rate limit, not by a shared gate.** The
limits are per vendor, so one shared semaphore would let a source with a
generous allowance starve a careful one — and with remotive permitted four
requests a day, that is not a theoretical loss.

**Embedding happens once, at the end, over deduplicated rows.** Not per posting
and not per source: a job cross-posted to four boards is one row and one vector.

Scheduling reads the *latest* run of a source whatever its status, never the
last successful one. A failed run still spent its requests, and a source
permitted four calls a day would otherwise be hammered precisely while it was
unhappy.
"""

import asyncio
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.db.enums import PipelineRunStatus, VacancyCompleteness
from app.db.repositories.pipeline_run import PipelineRunRepository
from app.db.repositories.profile import ProfileRepository
from app.db.repositories.source_quota import SourceQuotaRepository
from app.db.repositories.source_state import SourceStateRepository
from app.db.repositories.vacancy import UpsertItem, VacancyRepository
from app.db.session import session_factory
from app.normalize.fingerprint import VERSION as FINGERPRINT_VERSION
from app.normalize.fingerprint import fingerprint
from app.pipeline.embedding import EmbeddingOutcome, embed_pending
from app.schemas.pipeline import PipelineRunCreate, PipelineRunFinish
from app.schemas.profile import CandidateProfileRead
from app.schemas.vacancy import VacancyCreate
from app.sources.base import BaseSource, RawPosting, SourceUnavailable, Unavailable
from app.sources.http import get_client
from app.sources.query_planner import QueryPlan, plan_queries
from app.sources.registry import disabled_reason, get_enabled_sources, get_source

logger = get_logger(__name__)

#: How the crawl gets a session. Injectable for the same reason the token
#: bucket's clock is: the orchestration opens several short-lived sessions of
#: its own rather than borrowing the request's, and a test that cannot supply
#: them can only test the parts that do no work.
type Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]

#: Postings held in memory before a write. One page of a busy source, so the
#: batch amortises the round trip without letting a long crawl grow unbounded.
UPSERT_BATCH = 100


@dataclass(slots=True)
class SourceOutcome:
    """What one source did, whether or not it worked."""

    slug: str
    found: int = 0
    new: int = 0
    updated: int = 0
    duplicates: int = 0
    requests: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    skipped: Unavailable | None = None
    duration_seconds: float = 0.0
    run_id: UUID | None = None

    @property
    def status(self) -> PipelineRunStatus:
        """Partial when something broke but something also arrived."""
        if not self.errors:
            return PipelineRunStatus.SUCCESS
        if self.found:
            return PipelineRunStatus.PARTIAL
        return PipelineRunStatus.FAILED


@dataclass(slots=True)
class RunReport:
    """Everything one crawl did, in the shape the API and the CLI both want."""

    plan: QueryPlan
    sources: list[SourceOutcome] = field(default_factory=list)
    embedding: EmbeddingOutcome | None = None
    dry_run: bool = False
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    duration_seconds: float = 0.0

    @property
    def found(self) -> int:
        """Postings yielded, which is not the number of rows written."""
        return sum(outcome.found for outcome in self.sources)

    @property
    def new(self) -> int:
        """Vacancies that did not exist before this run."""
        return sum(outcome.new for outcome in self.sources)

    @property
    def duplicates(self) -> int:
        """Postings collapsed into a vacancy another posting already created."""
        return sum(outcome.duplicates for outcome in self.sources)


def to_vacancy(posting: RawPosting) -> VacancyCreate:
    """Minimal normalisation: enough to store the posting, no more.

    Phase 4 owns the real thing — salaries, skills, work authorisation, spam.
    What cannot wait is the fingerprint, because the column is NOT NULL and
    UNIQUE and nothing can be written without one.

    ``city`` is deliberately left empty here rather than guessed from a
    free-text location. A wrong city changes the fingerprint, and a fingerprint
    computed from a guess merges two different jobs or splits one — the first of
    which loses data irreversibly.
    """
    company = (posting.company or "").strip() or None
    description = posting.description or None
    return VacancyCreate(
        fingerprint=fingerprint(company=company, title=posting.title, city=None),
        fingerprint_version=FINGERPRINT_VERSION,
        title=posting.title,
        company=company,
        description_raw=description,
        completeness=(VacancyCompleteness.FULL if description else VacancyCompleteness.STUB),
    )


async def run_pipeline(
    *,
    source_slugs: list[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    sessions: Sessions = session_factory,
) -> RunReport:
    """Crawl every eligible source once and report what happened.

    Opens its own session on purpose, the way the resume background task does:
    a request's session is closed when the response is sent, and a long crawl
    must not hold one write transaction open across the whole thing.
    """
    started = asyncio.get_running_loop().time()
    async with sessions() as session:
        profile = await ProfileRepository(session).get_active()
        if profile is None:
            raise AppError("Нет активного профиля: сначала загрузите резюме")
        plan = plan_queries(CandidateProfileRead.model_validate(profile))
        sources = await _eligible(session, source_slugs, force=force)

    logger.info(
        "pipeline.planned",
        queries=len(plan.queries),
        dropped=plan.dropped,
        groups=list(plan.groups),
        sources=[source.slug for source, reason in sources if reason is None],
    )
    report = RunReport(plan=plan, dry_run=dry_run)

    if dry_run:
        # Everything decided, nothing fetched: the point is to see the plan and
        # which sources would run before spending a metered request on it.
        report.sources = [
            SourceOutcome(slug=source.slug, skipped=reason) for source, reason in sources
        ]
        report.duration_seconds = asyncio.get_running_loop().time() - started
        return report

    runnable = [source for source, reason in sources if reason is None]
    report.sources = [
        SourceOutcome(slug=source.slug, skipped=reason)
        for source, reason in sources
        if reason is not None
    ]
    outcomes = await asyncio.gather(
        *(_run_source(source, plan, sessions) for source in runnable), return_exceptions=False
    )
    report.sources.extend(outcomes)

    async with sessions() as session:
        report.embedding = await embed_pending(session)
        await session.commit()

    report.duration_seconds = asyncio.get_running_loop().time() - started
    logger.info(
        "pipeline.finished",
        found=report.found,
        new=report.new,
        duplicates=report.duplicates,
        seconds=round(report.duration_seconds, 1),
    )
    return report


async def _eligible(
    session: AsyncSession, slugs: list[str] | None, *, force: bool
) -> list[tuple[BaseSource, Unavailable | None]]:
    """Every source we were asked for, each with its reason for sitting out."""
    chosen = [get_source(slug) for slug in slugs] if slugs else get_enabled_sources()
    runs = PipelineRunRepository(session)
    quotas = SourceQuotaRepository(session)
    latest = {run.source_slug: run for run in await runs.latest_per_source()}

    decided: list[tuple[BaseSource, Unavailable | None]] = []
    for source in chosen:
        reason = disabled_reason(source)
        if reason is None:
            reason = await _waiting_reason(source, latest.get(source.slug), quotas, force=force)
        decided.append((source, reason))
    return decided


async def _waiting_reason(
    source: BaseSource,
    last_run: Any,
    quotas: SourceQuotaRepository,
    *,
    force: bool,
    now: datetime | None = None,
) -> Unavailable | None:
    """Cooling down, out of credits, or ready.

    Takes the clock as an argument for the same reason ``BaseSource.is_due``
    does: a scheduling rule that can only be tested against the wall clock can
    only be tested on the day the test was written.
    """
    remaining = await quotas.remaining(source.slug, source.daily_quota)
    if remaining is not None and remaining <= 0:
        return Unavailable(
            code=SourceUnavailable.QUOTA_EXHAUSTED,
            detail=(
                f"Дневной лимит источника «{source.name}» исчерпан "
                f"({source.daily_quota} запросов). Сбрасывается в полночь UTC."
            ),
        )

    started_at = getattr(last_run, "started_at", None)
    if source.is_due(started_at, now=now):
        return None
    # force skips a short cooldown but never a long one: "не отключать rate
    # limiting «чтобы быстрее»" applies to a person in a hurry too, and the
    # long intervals are the ones a vendor's terms actually impose.
    if force and source.min_interval.total_seconds() <= 3600:
        return None
    until = source.cooldown_until(started_at)
    return Unavailable(
        code=SourceUnavailable.COOLING_DOWN,
        detail=(
            f"Источник «{source.name}» опрашивается не чаще чем раз в "
            f"{source.min_interval}. Следующий запуск после {until:%H:%M %d.%m}."
        ),
        retry_after=until,
    )


async def _run_source(source: BaseSource, plan: QueryPlan, sessions: Sessions) -> SourceOutcome:
    """One source, start to finish, with its failure contained."""
    outcome = SourceOutcome(slug=source.slug)
    started = asyncio.get_running_loop().time()

    async with sessions() as session:
        run = await PipelineRunRepository(session).start(PipelineRunCreate(source_slug=source.slug))
        outcome.run_id = run.id
        await session.commit()

    try:
        await _crawl(source, plan, outcome, sessions)
    except Exception as exc:  # a broken source, not a broken run
        logger.exception("pipeline.source_failed", slug=source.slug)
        outcome.errors.append({"stage": "crawl", "error": type(exc).__name__, "detail": str(exc)})

    outcome.duration_seconds = asyncio.get_running_loop().time() - started
    async with sessions() as session:
        await PipelineRunRepository(session).finish(
            outcome.run_id,
            PipelineRunFinish(
                status=outcome.status,
                found=outcome.found,
                new=outcome.new,
                updated=outcome.updated,
                errors=outcome.errors,
            ),
        )
        await session.commit()
    logger.info(
        "pipeline.source_done",
        slug=source.slug,
        status=outcome.status.value,
        found=outcome.found,
        new=outcome.new,
        updated=outcome.updated,
        requests=outcome.requests,
        seconds=round(outcome.duration_seconds, 1),
    )
    return outcome


async def _crawl(
    source: BaseSource, plan: QueryPlan, outcome: SourceOutcome, sessions: Sessions
) -> None:
    """Fetch and write, one batch at a time."""
    bound = _bind(source, outcome, sessions)
    batch: list[RawPosting] = []

    # No semaphore inside a source: its concurrency is already bounded by its
    # own token bucket, which is the limit the vendor actually published. A
    # second gate here would look like a safeguard while enforcing nothing.
    async for posting in bound.search_batch(plan.queries):
        outcome.found += 1
        if source.needs_detail_fetch:
            posting = await bound.fetch_detail(posting)
        batch.append(posting)
        if len(batch) >= UPSERT_BATCH:
            await _write(batch, outcome, sessions)
            batch = []
    if batch:
        await _write(batch, outcome, sessions)


def _bind(source: BaseSource, outcome: SourceOutcome, sessions: Sessions) -> BaseSource:
    """Give the source its client, its credit hook, its "seen this?" lookup and its position.

    Each hook opens its own short session rather than sharing one. A crawl runs
    for minutes and the write it does at the end must not sit behind a
    transaction opened at the start of it.
    """

    async def spend(slug: str) -> None:
        outcome.requests += 1
        if source.daily_quota is None:
            return
        async with sessions() as session:
            await SourceQuotaRepository(session).spend(slug)
            await session.commit()

    async def known(external_ids: Sequence[str]) -> set[str]:
        async with sessions() as session:
            return await VacancyRepository(session).known_external_ids(source.slug, external_ids)

    async def load_state(key: str) -> dict[str, Any] | None:
        async with sessions() as session:
            return await SourceStateRepository(session).get(source.slug, key)

    async def save_state(key: str, value: dict[str, Any]) -> None:
        # Committed as soon as the connector asks, not at the end of the crawl:
        # the point of the record is to survive the run dying, and a value
        # written inside a transaction that never commits records nothing.
        async with sessions() as session:
            await SourceStateRepository(session).set(source.slug, key, value)
            await session.commit()

    client = get_client()
    return (
        source.bind(client.bind(source, on_request=spend))
        .with_known_ids(known)
        .with_state(load_state, save_state)
    )


async def _write(postings: list[RawPosting], outcome: SourceOutcome, sessions: Sessions) -> None:
    """Upsert one batch and record what it did."""
    items: list[UpsertItem] = [
        (to_vacancy(posting), posting.source_slug, posting.external_id, posting.url, posting.raw)
        for posting in postings
    ]
    async with sessions() as session:
        result = await VacancyRepository(session).bulk_upsert(items)
        await session.commit()
    outcome.new += result.created
    outcome.updated += result.updated
    # bulk_upsert deduplicates by fingerprint, so a cross-posted job contributes
    # one vacancy and several source rows. The gap is the cross-publisher
    # duplicate rate, which is worth reporting rather than hiding.
    outcome.duplicates += len(postings) - len(result.vacancy_ids)
