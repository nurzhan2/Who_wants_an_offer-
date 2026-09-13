"""One crawl: plan, fan out across sources, write, then embed once.

The shape follows from three constraints that are easy to state and easy to get
wrong.

**A source that breaks must not break the run.** Every connector is awaited
inside its own guard, its failure lands in ``pipeline_run.errors``, and the run
carries on. That is what the JSONB column is for, and why a run with one broken
source finishes ``partial`` rather than ``failed``: ``last_successful`` counts
partial runs, so marking the whole crawl failed would reset the incremental
watermark and force a full re-crawl next time.

**A source that refuses us is not a source that broke.** A site answering a
permitted request with a check for robots has decided something about us, and
the run for it is over — immediately, with no retry, because retrying a request
a host has just refused is useless and rude. It is recorded as
``SourceOutcome.challenged`` and stays ``partial``: the connector is fine, the
watermark must not reset, and a report needs to be able to say «остановлены
проверкой» rather than «источник сломан».

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
import time
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
from app.normalize.sync import sync_requirements
from app.pipeline.embedding import EmbeddingOutcome, embed_pending, embed_pending_titles
from app.schemas.pipeline import PipelineRunCreate, PipelineRunFinish
from app.schemas.profile import CandidateProfileRead
from app.schemas.vacancy import VacancyCreate
from app.sources.base import BaseSource, RawPosting, SourceUnavailable, Unavailable
from app.sources.http import HHChallengedError, get_client
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

#: How long a partial batch may stay unwritten. A count alone is the wrong
#: measure for a slow source: hh is crawled at about one page every four to five
#: seconds, so a batch of a hundred is seven and a half minutes of work held in
#: memory, and a crawl that ends before then — which every hh crawl so far has,
#: whether by a check for robots or by the operator — writes nothing and records
#: no position unless it happens to end through the one path that rescues the
#: batch. Measured with the rule in place: a run cut off after 200 seconds stored
#: 31 postings — a third of a batch — and recorded positions for two sitemap
#: files. Killed at the same point without it, a hard signal takes all 31 with it.
#:
#: So whichever comes first. A busy feed still writes by count and pays nothing
#: for this; a slow corpus writes every minute or so and its work survives.
BATCH_MAX_AGE_SECONDS = 60.0


@dataclass(slots=True)
class SourceOutcome:
    """What one source did, whether or not it worked."""

    slug: str
    found: int = 0
    new: int = 0
    updated: int = 0
    duplicates: int = 0
    #: ``vacancy_skill`` rows derived from what this source's postings carried.
    #: Worth reporting next to ``new``: a source can store hundreds of vacancies
    #: and contribute nothing scoreable, and that is a fact about the source
    #: rather than about the crawl.
    skills: int = 0
    requests: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    skipped: Unavailable | None = None
    duration_seconds: float = 0.0
    run_id: UUID | None = None
    #: The source answered with a check for robots and this run was stopped for
    #: it. A field of its own rather than a shade of ``status``, because the two
    #: answer different questions: ``status`` says how much of the work got
    #: done, and this says why it stopped. A report that has both can write
    #: «остановлены проверкой» where it would otherwise write «источник сломан»,
    #: which is the difference between rescheduling and debugging.
    challenged: bool = False

    @property
    def status(self) -> PipelineRunStatus:
        """Partial when something broke but something also arrived.

        A challenge is ``PARTIAL`` whatever it managed to reach, and never
        ``FAILED``. Two reasons, and the second is the one with teeth.

        ``FAILED`` reads as "the connector is broken, go and look at it", and a
        challenge is the one failure where there is nothing in the connector to
        look at: the requests were within the rules and the site said no anyway.

        And the status is not only prose. ``last_successful`` counts partial
        runs as a watermark, so recording a challenge as a failure would reset
        the incremental position of a source that was working perfectly one
        request earlier and force a full re-crawl — spending several times the
        requests, against a host that has just told us we are asking for too
        many.
        """
        if self.challenged:
            return PipelineRunStatus.PARTIAL
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
    #: Title vectors, matching's main signal since ``0015_title_embedding``.
    title_embedding: EmbeddingOutcome | None = None
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

    @property
    def skills(self) -> int:
        """``vacancy_skill`` rows this run derived."""
        return sum(outcome.skills for outcome in self.sources)

    @property
    def challenged_sources(self) -> list[str]:
        """Sources a check for robots stopped, for a report to name as such.

        Separate from the error list on purpose. Everything in ``errors`` wants
        somebody to read a traceback; this wants somebody to decide whether to
        crawl that host more slowly, or less often, or not today.
        """
        return [outcome.slug for outcome in self.sources if outcome.challenged]


#: Width of ``vacancy.city``. A longer value fails ``VacancyCreate`` and takes
#: the whole batch down instead of the one posting that carried it.
MAX_CITY = 120


def stated_city(posting: RawPosting) -> str | None:
    """The city a connector read off the posting, or None when it read none.

    ``RawPosting.raw["_derived"]`` is the seam connectors already use for what
    they worked out rather than for what the payload literally said, and a
    ``city`` key in it means a place name lifted from a structured field. Read
    by key rather than by source slug, so a new connector opts in from inside
    ``sources/`` and this module never learns its name (CLAUDE.md rule 5). No
    city is named here; the value comes from the posting.

    **The trust this places in a connector has one known hole.** hh fills the
    key from the page's ``address.city``, falls back to its ``area``, and — for
    a page that gives neither — falls back a third time to the city of the host
    it was crawled from: ``city=(city or site.city)`` in ``hh.py``'s
    ``_derive``. That third value is not something the posting stated. It is
    this deployment's own ``hh_sites.yaml`` reaching the deduplication key, so
    such a posting is keyed by where we were standing when we read it, and one
    walked from two hosts becomes two rows with one ``vacancy_source`` row
    flipping between them.

    Nothing here can undo that: the three cases arrive flattened into one
    string, and a guard in this module cannot tell which one it is holding. The
    fix belongs in ``_derive`` — leave the city ``None`` when the page states
    none, which is what every other connector already does — and until that
    lands this docstring names the exception rather than repeating a rule the
    data does not keep. It is thought to fire rarely: the connector's own probe
    of 200 vacancy pages found ``address`` on 184 of them and ``area`` on all
    200, so no page in that sample would have reached the third fallback.

    Deliberately *not* read: JSearch's ``_derived["location"]``, which is one
    free-text line holding a city, a region and a country together («Алматы,
    Казахстан»). Cutting a city out of that line is normalisation and phase 4
    owns it, and hashing the whole line instead would key one job differently
    on every source that spells its location its own way — a split, so nothing
    is lost, but nothing is gained either. arbeitnow and remotive state no
    place at all in a structured field, so their postings keep hashing without
    a city, exactly as they did under version 1.
    """
    derived = posting.raw.get("_derived")
    if not isinstance(derived, dict):
        return None
    city = derived.get("city")
    if not isinstance(city, str):
        return None
    return " ".join(city.split())[:MAX_CITY] or None


def to_vacancy(posting: RawPosting) -> VacancyCreate:
    """Minimal normalisation: enough to store the posting, no more.

    Phase 4 owns the real thing — salaries, skills, work authorisation, spam.
    What cannot wait is the fingerprint, because the column is NOT NULL and
    UNIQUE and nothing can be written without one.

    ``city`` is taken from the posting when its source stated one and left
    ``None`` when it did not; it is never guessed out of free text. Both the
    column and the fingerprint get the same value, because a row whose key
    distinguishes it by city while its own ``city`` column is empty is a row
    nobody can explain. Leaving the city out of the key merges an employer's
    four cities into one row and overwrites three of the four postings, which
    is the loss :mod:`app.normalize.fingerprint` calls unrecoverable.
    """
    company = (posting.company or "").strip() or None
    description = posting.description or None
    city = stated_city(posting)
    return VacancyCreate(
        fingerprint=fingerprint(company=company, title=posting.title, city=city),
        fingerprint_version=FINGERPRINT_VERSION,
        title=posting.title,
        company=company,
        city=city,
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
        report.title_embedding = await embed_pending_titles(session)
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
    except HHChallengedError as exc:
        # Caught apart from every other failure, and one line above it, because
        # it is not one: the source answered a permitted request with a check
        # for robots. Recorded under its own stage and logged as a warning
        # rather than an exception, since a traceback here points at the hop
        # that was refused and there is no bug in it to find.
        #
        # This ends the run for this source and nothing else. The others are
        # separate tasks under the gather above and keep going. What this source
        # already fetched has been written by the time we get here, and its
        # crawl position was left exactly where it was — so the next run resumes
        # at the page this one was refused, and re-fetches nothing it stored.
        outcome.challenged = True
        outcome.errors.append(
            {"stage": "challenge", "error": type(exc).__name__, "detail": exc.detail}
        )
        logger.warning(
            "pipeline.source_challenged",
            slug=source.slug,
            found=outcome.found,
            requests=outcome.requests,
            detail=exc.detail,
        )
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
    """Fetch and write, one batch at a time.

    An interrupted crawl still writes the batch it was holding. Those postings
    are already fetched and already paid for — on the run that produced this
    rule, 172 pages had been read and the last 72 of them were in this list —
    and dropping them means buying them again next run for nothing. The
    connector's crawl position is behind them by design, so writing them is
    safe: the position never names a posting this function has not handed to
    the database.
    """
    bound = _bind(source, outcome, sessions)
    batch: list[RawPosting] = []
    opened = time.monotonic()
    #: Postings this function has handed to the database and seen committed.
    #: The connector cannot know this and must not guess it; see
    #: ``BaseSource.record_progress``.
    durable = 0

    # No semaphore inside a source: its concurrency is already bounded by its
    # own token bucket, which is the limit the vendor actually published. A
    # second gate here would look like a safeguard while enforcing nothing.
    try:
        async for posting in bound.search_batch(plan.queries):
            outcome.found += 1
            if source.needs_detail_fetch:
                posting = await bound.fetch_detail(posting)
            batch.append(posting)
            full = len(batch) >= UPSERT_BATCH
            stale = time.monotonic() - opened >= BATCH_MAX_AGE_SECONDS
            if full or stale:
                await _write(batch, outcome, sessions)
                durable += len(batch)
                await bound.record_progress(durable)
                batch = []
                opened = time.monotonic()
    except Exception:
        if batch:
            try:
                await _write(batch, outcome, sessions)
                durable += len(batch)
                # After the write and before the re-raise, which is the whole
                # point: these are the postings a crawl stopped by a check for
                # robots has just rescued, and telling the connector about them
                # is what lets the next run start after them instead of at the
                # top of the corpus again.
                await bound.record_progress(durable)
            except Exception:
                # Logged, not raised: a write that fails while unwinding must
                # not replace the failure that stopped the crawl. That one says
                # why the run ended — a check for robots reads very differently
                # from a broken connector — and the caller classifies on it.
                logger.exception("pipeline.partial_write_failed", slug=source.slug)
        raise
    if batch:
        await _write(batch, outcome, sessions)
        durable += len(batch)
        # One confirmation per write, and only after one. A run that ends with
        # nothing outstanding has already told the connector everything it knows.
        await bound.record_progress(durable)


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
        # In the same transaction as the write, over the ids it just returned.
        # A vacancy that is stored but has no ``vacancy_skill`` rows is not
        # scoreable, so a crash between the two would leave the corpus in the
        # state this whole change exists to end. Deriving is pure and local —
        # no requests, no model — so it costs the batch a few statements.
        sync = await sync_requirements(session, vacancy_ids=result.vacancy_ids)
        await session.commit()
    outcome.new += result.created
    outcome.updated += result.updated
    outcome.skills += sync.skills_written
    # bulk_upsert deduplicates by fingerprint, so a cross-posted job contributes
    # one vacancy and several source rows. The gap is the cross-publisher
    # duplicate rate, which is worth reporting rather than hiding.
    outcome.duplicates += len(postings) - len(result.vacancy_ids)
