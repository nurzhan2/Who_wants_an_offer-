"""A whole crawl, from plan to stored vector, with no network and no model.

The pieces are tested elsewhere; this is the wiring, and the wiring is where a
phase like this actually fails. A connector can be perfect while the runner
writes nothing, counts the wrong thing, or lets one broken source take the run
down with it.

Two seams make it possible to run the real orchestration here. The session
factory is injected, so the crawl writes into the test's own rolled-back
transaction instead of opening connections nobody can see; and
``EMBEDDING_PROVIDER="fake"`` gives deterministic vectors without the 2.3 GB of
model weights CI does not install. Neither seam is a test-only shortcut — the
first is what lets a scheduler own its sessions in phase 9, and the second is
already how the rest of the suite runs.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from typing import Any, ClassVar

import pytest
from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.enums import PipelineRunStatus
from app.db.models import PipelineRun, SourceQuota, Vacancy, VacancySkill, VacancySource
from app.db.repositories.profile import ProfileRepository
from app.pipeline.runner import run_pipeline
from app.sources import registry
from app.sources.base import BaseSource, RateLimit, RawPosting, SearchQuery
from factories import make_profile

pytestmark = pytest.mark.db


def posting(
    external_id: str, *, title: str, company: str, description: str | None = "Body."
) -> RawPosting:
    """One posting from a fake source."""
    return RawPosting(
        source_slug="fixture_source",
        external_id=external_id,
        url=f"https://example.test/{external_id}",
        title=title,
        company=company,
        description=description,
    )


class FixtureSource(BaseSource):
    """A source that yields canned postings and never touches the network."""

    slug = "fixture_source"
    name = "Fixture"
    rate_limit = RateLimit(requests_per_second=50.0, burst=50)

    #: Rewritten per test. ClassVar because the runner builds the instance.
    postings: ClassVar[list[RawPosting]] = []
    fail: ClassVar[bool] = False

    async def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]:
        """Yield the canned postings, or break on purpose."""
        if self.fail:
            raise RuntimeError("upstream fell over")
        for item in self.postings:
            yield item


class BrokenSource(BaseSource):
    """A source that always fails, to prove it cannot take the run with it."""

    slug = "broken_source"
    name = "Broken"

    async def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]:
        """Fail before yielding anything."""
        raise RuntimeError("connector is broken")
        yield  # pragma: no cover - unreachable, keeps this an async generator


@pytest.fixture
def sessions(db_session: AsyncSession) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """Hand the crawl the test's own session so its writes are visible and undone.

    Serialised with a lock. In production each source is handed its own session
    by ``session_factory``, so two connectors running under ``asyncio.gather``
    never share one; here they must, and an AsyncSession used concurrently
    interleaves a flush with an add. The lock reproduces the isolation the real
    factory gives without giving up the single rolled-back transaction that
    makes the writes visible to the test.
    """
    lock = asyncio.Lock()

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with lock:
            yield db_session

    return factory


@pytest.fixture
def only_fixture_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the fakes and let nothing else run.

    The real connectors stay registered — reset_registry cannot rebuild them —
    so the allow-list is what keeps this test off the network.
    """
    registry.register_source(FixtureSource)
    registry.register_source(BrokenSource)
    monkeypatch.setattr(settings, "sources_enabled", frozenset({"fixture_source"}))
    monkeypatch.setattr(settings, "embedding_provider", "fake")
    yield
    registry.forget_source("fixture_source")
    registry.forget_source("broken_source")


@pytest.fixture
async def profile(db_session: AsyncSession) -> None:
    """An active profile, because a crawl is planned from one."""
    profiles = ProfileRepository(db_session)
    created = await profiles.create(make_profile(skills=("python", "fastapi", "postgresql")))
    await profiles.activate(created.id)
    await db_session.flush()


async def backdate_embedding(session: AsyncSession, *, hours: int = 1) -> None:
    """Age every stored vector so a later crawl counts as newer than it.

    Needed because of how this suite isolates tests, not because of the code
    under test: everything here runs inside one transaction, and PostgreSQL's
    ``now()`` returns that transaction's start for every statement in it. So
    ``updated_at`` and ``embedded_at`` come out exactly equal and the "has this
    row moved since we embedded it" comparison can never fire. In production the
    two runs are two transactions, minutes or hours apart.
    """
    await session.execute(
        sa_update(Vacancy.__table__).values(
            embedded_at=Vacancy.__table__.c.embedded_at - timedelta(hours=hours)
        )
    )
    await session.flush()


async def count(session: AsyncSession, model: Any) -> int:
    """Rows of one table."""
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def test_a_crawl_writes_vacancies_and_their_provenance(
    db_session: AsyncSession,
    sessions: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    only_fixture_sources: None,
    profile: None,
) -> None:
    """The whole point: postings in, rows out, with the link back to the source
    that carried each one."""
    FixtureSource.postings = [
        posting("a1", title="Backend Engineer", company="Acme"),
        posting("a2", title="Data Engineer", company="Globex"),
    ]
    FixtureSource.fail = False

    report = await run_pipeline(sessions=sessions)

    assert report.found == 2
    assert report.new == 2
    assert await count(db_session, Vacancy) == 2
    assert await count(db_session, VacancySource) == 2


async def test_a_cross_posted_job_becomes_one_vacancy_with_two_sources(
    db_session: AsyncSession,
    sessions: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    only_fixture_sources: None,
    profile: None,
) -> None:
    """Two publishers, one job. Two vacancy rows would put the same posting on
    the dashboard twice and split its match score between them."""
    FixtureSource.postings = [
        posting("indeed-1", title="Senior Engineer", company="CPI Card Group"),
        posting("monster-1", title="Senior Engineer", company="CPI Card Group"),
    ]
    FixtureSource.fail = False

    report = await run_pipeline(sessions=sessions)

    assert report.found == 2
    assert report.duplicates == 1
    assert await count(db_session, Vacancy) == 1
    assert await count(db_session, VacancySource) == 2


async def test_a_second_crawl_updates_rather_than_duplicates(
    db_session: AsyncSession,
    sessions: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    only_fixture_sources: None,
    profile: None,
) -> None:
    """Idempotence is the whole reason the upsert keys on (source, external_id):
    a nightly crawl must not grow the table by a copy of the market every time."""
    FixtureSource.postings = [posting("a1", title="Backend Engineer", company="Acme")]
    FixtureSource.fail = False

    first = await run_pipeline(sessions=sessions)
    second = await run_pipeline(sessions=sessions)

    assert first.new == 1
    assert second.new == 0
    assert await count(db_session, Vacancy) == 1


async def test_a_broken_source_does_not_take_the_run_down(
    db_session: AsyncSession,
    sessions: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    only_fixture_sources: None,
    profile: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other source's postings must still land, and the failure must be
    recorded rather than raised — a run marked failed would reset the
    incremental watermark for every source, punishing the ones that worked."""
    monkeypatch.setattr(settings, "sources_enabled", frozenset({"fixture_source", "broken_source"}))
    FixtureSource.postings = [posting("a1", title="Backend Engineer", company="Acme")]
    FixtureSource.fail = False

    report = await run_pipeline(sessions=sessions)

    assert await count(db_session, Vacancy) == 1
    broken = next(item for item in report.sources if item.slug == "broken_source")
    assert broken.errors
    assert broken.errors[0]["error"] == "RuntimeError"


async def test_every_source_gets_a_run_record(
    db_session: AsyncSession,
    sessions: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    only_fixture_sources: None,
    profile: None,
) -> None:
    """The run record is how the sources page answers "when did this last work",
    and it is opened before fetching so even a crash leaves the attempt on
    record."""
    FixtureSource.postings = [posting("a1", title="Backend Engineer", company="Acme")]
    FixtureSource.fail = False

    await run_pipeline(sessions=sessions)

    runs = (
        (
            await db_session.execute(
                select(PipelineRun).where(PipelineRun.source_slug == "fixture_source")
            )
        )
        .scalars()
        .all()
    )
    assert len(runs) == 1
    assert runs[0].status is PipelineRunStatus.SUCCESS
    assert runs[0].found == 1


async def test_a_dry_run_decides_everything_and_writes_nothing(
    db_session: AsyncSession,
    sessions: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    only_fixture_sources: None,
    profile: None,
) -> None:
    """This is how a plan is inspected before it spends a metered request, so it
    must not open a run record either."""
    FixtureSource.postings = [posting("a1", title="Backend Engineer", company="Acme")]
    FixtureSource.fail = False

    report = await run_pipeline(dry_run=True, sessions=sessions)

    assert report.dry_run is True
    assert report.plan.queries
    assert await count(db_session, Vacancy) == 0
    assert await count(db_session, PipelineRun) == 0


async def test_a_metered_source_spends_no_credits_when_it_makes_no_request(
    db_session: AsyncSession,
    sessions: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    only_fixture_sources: None,
    profile: None,
) -> None:
    """The fake source is unmetered, so the ledger must stay empty: a counter
    that ticked for a source with no quota would make every allowance read as
    spent."""
    FixtureSource.postings = [posting("a1", title="Backend Engineer", company="Acme")]
    FixtureSource.fail = False

    await run_pipeline(sessions=sessions)

    assert await count(db_session, SourceQuota) == 0


# ── the embedding step ────────────────────────────────────────────────


async def test_the_crawl_embeds_what_it_wrote(
    db_session: AsyncSession,
    sessions: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    only_fixture_sources: None,
    profile: None,
) -> None:
    """A vacancy with no vector cannot be scored semantically, so the run has to
    finish the job it started."""
    FixtureSource.postings = [posting("a1", title="Backend Engineer", company="Acme")]
    FixtureSource.fail = False

    report = await run_pipeline(sessions=sessions)

    assert report.embedding is not None
    assert report.embedding.embedded == 1
    stored = (
        await db_session.execute(select(Vacancy.embedding_text_hash, Vacancy.embedded_at))
    ).one()
    assert stored.embedding_text_hash
    assert stored.embedded_at is not None


async def test_an_unchanged_description_is_never_re_embedded(
    db_session: AsyncSession,
    sessions: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    only_fixture_sources: None,
    profile: None,
) -> None:
    """The measurement the whole hash column exists for.

    Every re-crawl rewrites updated_at whether or not a word changed, so
    "the row was touched" cannot be the trigger. Without this the nightly run
    would re-encode the entire corpus every night."""
    FixtureSource.postings = [posting("a1", title="Backend Engineer", company="Acme")]
    FixtureSource.fail = False

    await run_pipeline(sessions=sessions)
    await backdate_embedding(db_session)
    second = await run_pipeline(sessions=sessions)

    assert second.embedding is not None
    assert second.embedding.considered == 1, "the row was never even reconsidered"
    assert second.embedding.unchanged == 1
    assert second.embedding.embedded == 0


async def test_a_changed_description_is_re_embedded(
    db_session: AsyncSession,
    sessions: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    only_fixture_sources: None,
    profile: None,
) -> None:
    """The other half: a rewritten posting must not keep a vector describing
    what it used to say, or it would go on matching the old job for ever."""
    FixtureSource.postings = [posting("a1", title="Backend Engineer", company="Acme")]
    FixtureSource.fail = False
    await run_pipeline(sessions=sessions)
    await backdate_embedding(db_session)

    FixtureSource.postings = [
        posting("a1", title="Backend Engineer", company="Acme", description="Rewritten body.")
    ]
    second = await run_pipeline(sessions=sessions)

    assert second.embedding is not None
    assert second.embedding.embedded == 1


async def test_a_crawl_leaves_its_vacancies_scoreable(
    db_session: AsyncSession,
    sessions: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    only_fixture_sources: None,
    profile: None,
) -> None:
    """Skills are derived in the same step that stores the vacancy.

    The alternative — deriving them in a later pass — is how ``vacancy_skill``
    came to be empty for all 643 rows while the payloads to fill it sat in the
    database the whole time. A vacancy that is stored but carries no skill rows
    is not scoreable, so the two have to land together or not at all.
    """
    FixtureSource.postings = [
        posting("s1", title="Backend Engineer", company="Acme").model_copy(
            update={
                "raw": {
                    "_derived": {
                        "key_skills": ["Python", "PostgreSQL", "Английский язык"],
                        "work_experience": "between3And6",
                    }
                }
            }
        )
    ]
    FixtureSource.fail = False

    report = await run_pipeline(sessions=sessions)

    assert report.skills == 2, "the language must not have become a third skill"
    names = await db_session.execute(select(VacancySkill.canonical_name))
    assert sorted(row[0] for row in names.all()) == ["postgresql", "python"]
    stored = (await db_session.execute(select(Vacancy))).scalars().one()
    assert stored.min_years == Decimal("3")


async def test_a_crawl_of_postings_without_skills_still_stores_them(
    db_session: AsyncSession,
    sessions: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    only_fixture_sources: None,
    profile: None,
) -> None:
    """Deriving nothing is not a reason to store nothing.

    Most of the corpus is this shape — hh's key-skills field is optional and 449
    of 643 rows leave it blank — so a derivation that could fail the write would
    fail most of the crawl.
    """
    FixtureSource.postings = [posting("s2", title="Sales Manager", company="Acme")]
    FixtureSource.fail = False

    report = await run_pipeline(sessions=sessions)

    assert report.new == 1
    assert report.skills == 0
    assert await count(db_session, VacancySkill) == 0
