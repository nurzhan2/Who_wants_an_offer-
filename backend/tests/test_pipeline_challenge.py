"""What the crawl does when a site answers a permitted request with a captcha.

Kept apart from ``test_pipeline_runner.py`` because it defends one claim, and
the claim is about a distinction rather than a feature: a source that refused us
is not a source that broke, and the run report has to be able to tell a person
which of the two happened. Reading the wrong one costs an afternoon of looking
for a bug in a connector that is working perfectly.

Three consequences follow and each has a test here.

**The status stays honest.** A challenged run is ``partial``, never ``failed``.
That is not a euphemism: ``last_successful`` counts partial runs as a watermark,
so recording a challenge as a failure would reset the incremental position of a
source that was fine one request earlier and force a full re-crawl -- several
times the requests, against a host that has just said we are asking for too
many.

**Everything already bought is kept.** The runner batches, so an interruption
finds up to ``UPSERT_BATCH`` postings in memory that have been fetched and paid
for. Dropping them means buying them again next run for nothing.

**One source is one source.** The connectors run as separate tasks under a
``gather`` with ``return_exceptions=False``, so a ``_run_source`` that let an
exception out would cancel every other source in the crawl.

No database and no network. The repositories are replaced with stubs that record
what they were handed, which is what these assertions are about -- the rows are
``test_pipeline_end_to_end.py``'s subject and need PostgreSQL to mean anything.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.db.enums import PipelineRunStatus
from app.db.repositories.vacancy import BulkUpsertResult, UpsertItem
from app.normalize.sync import SyncOutcome
from app.pipeline import runner
from app.pipeline import runner as runner_module
from app.pipeline.runner import (
    BATCH_MAX_AGE_SECONDS,
    UPSERT_BATCH,
    RunReport,
    SourceOutcome,
    _run_source,
)
from app.schemas.pipeline import PipelineRunCreate, PipelineRunFinish
from app.sources.base import BaseSource, RateLimit, RawPosting, SearchQuery
from app.sources.http import HHChallengedError, reset_client
from app.sources.query_planner import QueryPlan

pytestmark = pytest.mark.unit

PLAN = QueryPlan(queries=(SearchQuery(),))


def challenge(slug: str) -> HHChallengedError:
    """The transport's refusal to follow a redirect into a captcha."""
    return HHChallengedError(
        "stopped by a check for robots",
        host="almaty.hh.kz",
        path="/account/captcha",
        source_slug=slug,
    )


def posting(source_slug: str, external_id: str) -> RawPosting:
    """One posting from a fake source."""
    return RawPosting(
        source_slug=source_slug,
        external_id=external_id,
        url=f"https://example.test/{source_slug}/{external_id}",
        title="Backend Engineer",
        company="Acme",
        description="Python, FastAPI.",
    )


# -- fakes -------------------------------------------------------------


class Session:
    """Stands in for the AsyncSession each hook opens for itself."""

    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        """Count it, so a test can tell a write was meant to be durable."""
        self.commits += 1


class Derivation:
    """``sync_requirements``, minus the database.

    Records the ids it was handed. The runner derives skills inside the same
    transaction as the batch write so a stored vacancy is never left unscoreable,
    and «over the ids that were just written» is the part of that worth holding
    here — the derivation itself has its own tests.
    """

    calls: list[list[Any]] = []  # noqa: RUF012 - a test's shared ledger

    @staticmethod
    async def record(session: object, *, vacancy_ids: Sequence[Any] | None = None) -> Any:
        """Note the call and hand back an outcome shaped like the real one."""
        Derivation.calls.append(list(vacancy_ids or []))
        return SyncOutcome()


class Runs:
    """PipelineRunRepository, minus the database.

    Records what every run was closed with, because the status a challenge
    writes to that row is what a scheduler and a report both read.
    """

    finished: list[PipelineRunFinish] = []  # noqa: RUF012 - a test's shared ledger

    def __init__(self, session: object) -> None:
        self._session = session

    async def start(self, run: PipelineRunCreate) -> Any:
        """Open a run and hand back something with an id on it.

        ``Any`` because the runner reads one attribute off the row and the ORM
        model it really gets needs a database to exist.
        """
        return type("Row", (), {"id": uuid4()})()

    async def finish(self, run_id: UUID | None, outcome: PipelineRunFinish) -> None:
        """Remember how the run was closed."""
        self.finished.append(outcome)


class Vacancies:
    """VacancyRepository, minus the database. Remembers every batch it was given."""

    batches: list[list[UpsertItem]] = []  # noqa: RUF012 - a test's shared ledger

    def __init__(self, session: object) -> None:
        self._session = session

    async def bulk_upsert(self, items: list[UpsertItem]) -> BulkUpsertResult:
        """Accept the batch and report every posting as new."""
        self.batches.append(list(items))
        return BulkUpsertResult(
            created=len(items), updated=0, vacancy_ids=tuple(uuid4() for _ in items)
        )


class Yielding(BaseSource):
    """A source that yields what it was handed, then optionally is refused."""

    name = "Fake"
    rate_limit = RateLimit(requests_per_second=50.0, burst=100)

    def __init__(self, postings: list[RawPosting], *, challenged: bool) -> None:
        super().__init__()
        self._postings = postings
        self._challenged = challenged
        #: Every count the pipeline confirmed, in order. A connector that walks
        #: a corpus records its position from these and from nothing else.
        self.confirmed: list[int] = []

    async def record_progress(self, durable: int) -> None:
        """What ``app/sources/hh.py`` uses to record where the crawl got to."""
        self.confirmed.append(durable)

    async def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]:
        """Hand over the canned postings, then hit the captcha if asked to."""
        for item in self._postings:
            yield item
        if self._challenged:
            raise challenge(self.slug)


class Challenged(Yielding):
    """The source hh's captcha stops."""

    slug = "challenged"


class Healthy(Yielding):
    """The source that must keep going while the other one is stopped."""

    slug = "healthy"


class Ticking(Yielding):
    """A source whose postings advance a clock, so "slow" is testable without waiting."""

    slug = "healthy"

    def __init__(self, postings: list[RawPosting], *, challenged: bool, clock: list[int]) -> None:
        super().__init__(postings, challenged=challenged)
        self._clock = clock

    async def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]:
        """One posting, one tick."""
        async for item in super().search(query):
            self._clock[0] += 1
            yield item


class Broken(BaseSource):
    """A source that is genuinely broken, so the two cannot be conflated."""

    slug = "broken"
    name = "Broken"
    rate_limit = RateLimit(requests_per_second=50.0, burst=100)

    async def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]:
        """Fail the way a bug fails."""
        raise RuntimeError("connector is broken")
        yield  # pragma: no cover - unreachable, keeps this an async generator


# -- fixtures ----------------------------------------------------------


@pytest.fixture
def sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[], AbstractAsyncContextManager[Session]]]:
    """The crawl's session factory and its repositories, all without a database."""
    Runs.finished = []
    Vacancies.batches = []
    Derivation.calls = []
    monkeypatch.setattr(runner_module, "PipelineRunRepository", Runs)
    monkeypatch.setattr(runner_module, "VacancyRepository", Vacancies)
    # The batch write also derives vacancy_skill in the same transaction, which
    # is real SQL against a session these tests deliberately do not have. Its
    # own behaviour is covered in test_normalize_requirements.py; what matters
    # here is that it runs over the ids the write just returned, so it is
    # recorded rather than removed.
    monkeypatch.setattr(runner_module, "sync_requirements", Derivation.record)
    reset_client()

    @asynccontextmanager
    async def factory() -> AsyncIterator[Session]:
        yield Session()

    yield factory
    reset_client()


# -- the status stays honest -------------------------------------------


def test_a_challenged_source_is_partial_and_never_failed() -> None:
    """failed means "the connector is broken, go and look at it".

    A challenge is the one failure with nothing in the connector to look at:
    the requests were inside the rules and the site said no anyway. And the
    status is not only prose -- last_successful counts partial runs as a
    watermark, so failed would reset the incremental position of a source that
    was working one request earlier.
    """
    stopped = SourceOutcome(
        slug="hh",
        found=172,
        challenged=True,
        errors=[{"stage": "challenge", "error": "HHChallengedError", "detail": "stopped"}],
    )

    assert stopped.status is PipelineRunStatus.PARTIAL


def test_a_challenge_before_anything_arrived_is_still_not_a_failure() -> None:
    """The case the ordinary rule would get wrong.

    With no postings and an error recorded, the plain reading is failed -- and
    that is exactly the run whose watermark must survive, because it holds a
    whole city's crawl position and reached nothing this time only because it
    was refused on its first page.
    """
    stopped = SourceOutcome(
        slug="hh",
        found=0,
        challenged=True,
        errors=[{"stage": "challenge", "error": "HHChallengedError", "detail": "stopped"}],
    )

    assert stopped.status is PipelineRunStatus.PARTIAL


def test_a_source_that_really_broke_still_reads_as_failed() -> None:
    """The other half, so the exemption above cannot swallow real breakage."""
    broken = SourceOutcome(
        slug="fake",
        found=0,
        errors=[{"stage": "crawl", "error": "RuntimeError", "detail": "boom"}],
    )
    partial = SourceOutcome(
        slug="fake",
        found=3,
        errors=[{"stage": "crawl", "error": "RuntimeError", "detail": "boom"}],
    )

    assert broken.status is PipelineRunStatus.FAILED
    assert partial.status is PipelineRunStatus.PARTIAL
    assert SourceOutcome(slug="fake", found=3).status is PipelineRunStatus.SUCCESS


def test_a_report_can_name_the_sources_that_were_stopped_rather_than_broken() -> None:
    """The signal a report reads, kept apart from the error list.

    Everything in errors wants somebody to read a traceback. This wants somebody
    to decide whether to crawl that host more slowly, less often, or not today.
    """
    report = RunReport(
        plan=PLAN,
        sources=[
            SourceOutcome(slug="hh", found=172, challenged=True),
            SourceOutcome(slug="jsearch", found=40),
            SourceOutcome(
                slug="remotive",
                errors=[{"stage": "crawl", "error": "RuntimeError", "detail": "boom"}],
            ),
        ],
    )

    assert report.challenged_sources == ["hh"]


# -- what the runner does with one -------------------------------------


async def test_a_challenge_is_recorded_as_a_challenge_and_not_as_a_crash(
    sessions: Callable[[], AbstractAsyncContextManager[Session]],
) -> None:
    """The whole point, at the level a report reads.

    The outcome carries the distinct signal, the run row is closed partial, and
    the recorded error names the stage -- so nothing downstream has to guess
    from an exception name whether to reschedule or to debug.
    """
    source = Challenged([posting("challenged", "1")], challenged=True)

    outcome = await _run_source(source, PLAN, sessions)

    assert outcome.challenged is True
    assert outcome.status is PipelineRunStatus.PARTIAL
    assert [error["stage"] for error in outcome.errors] == ["challenge"]
    assert outcome.errors[0]["error"] == "HHChallengedError"
    assert Runs.finished[-1].status is PipelineRunStatus.PARTIAL


async def test_everything_already_fetched_is_written_before_the_run_stops(
    sessions: Callable[[], AbstractAsyncContextManager[Session]],
) -> None:
    """A batch in memory when the captcha arrives is not thrown away.

    On the run this rule comes from, 172 pages had been read and the last of
    them were still in the runner's list. They are fetched, they are paid for,
    and the connector's crawl position is behind them by design -- so writing
    them is both safe and the only way not to buy them twice.
    """
    postings = [posting("challenged", str(index)) for index in range(3)]
    assert len(postings) < UPSERT_BATCH, "otherwise the batch would have been written anyway"
    source = Challenged(postings, challenged=True)

    outcome = await _run_source(source, PLAN, sessions)

    assert outcome.found == 3
    assert outcome.new == 3
    assert [len(batch) for batch in Vacancies.batches] == [3]


async def test_the_rescued_batch_is_confirmed_before_the_run_stops(
    sessions: Callable[[], AbstractAsyncContextManager[Session]],
) -> None:
    """Writing the rescued batch is half of it; telling the connector is the other half.

    A corpus connector cannot record its crawl position on its own — it must not
    name an entry whose posting is still in this function's unwritten list — so
    it waits to be told what has been written. If the confirmation is skipped on
    the way out, a run stopped by hh's check for robots records nothing, and the
    next run starts at the top of the corpus again.

    That is not hypothetical: it is what both live runs did. hh answered at the
    172nd posting and then at the 50th, ``source_state`` stayed empty for the
    source's entire life, and the corpus sat at 466 rows out of some 13 557.
    """
    postings = [posting("challenged", str(index)) for index in range(3)]
    assert len(postings) < UPSERT_BATCH, "so only the rescue can confirm anything"
    source = Challenged(postings, challenged=True)

    await _run_source(source, PLAN, sessions)

    assert source.confirmed, "the crawl position was never told what had been written"
    assert source.confirmed[-1] == 3, f"confirmed {source.confirmed}, wrote 3"


async def test_the_confirmation_never_runs_ahead_of_the_write(
    sessions: Callable[[], AbstractAsyncContextManager[Session]],
) -> None:
    """Confirming more than was written is the one direction that loses postings.

    A position naming an unwritten posting is never revisited by any future run,
    so the page is lost rather than merely re-bought. Asserted against the
    batches the repository actually received.
    """
    postings = [posting("challenged", str(index)) for index in range(UPSERT_BATCH + 5)]
    source = Challenged(postings, challenged=True)

    await _run_source(source, PLAN, sessions)

    written = 0
    for size, confirmed in zip(
        [len(batch) for batch in Vacancies.batches], source.confirmed, strict=True
    ):
        written += size
        assert confirmed <= written, f"confirmed {confirmed} with only {written} written"
    assert source.confirmed[-1] == len(postings)


async def test_a_run_that_finishes_confirms_its_last_batch_too(
    sessions: Callable[[], AbstractAsyncContextManager[Session]],
) -> None:
    """The ordinary ending needs the same confirmation as the interrupted one.

    A crawl that drains its budget leaves a partial batch, writes it, and must
    say so — otherwise the tail of every successful run is re-bought forever,
    which is the same defect as the interrupted case wearing a friendlier face.
    """
    postings = [posting("healthy", str(index)) for index in range(UPSERT_BATCH + 4)]
    source = Healthy(postings, challenged=False)

    outcome = await _run_source(source, PLAN, sessions)

    assert outcome.errors == []
    assert source.confirmed[-1] == len(postings), (
        f"finished having written {len(postings)}, confirmed {source.confirmed}"
    )


async def test_every_batch_written_is_also_derived_before_it_is_committed(
    sessions: Callable[[], AbstractAsyncContextManager[Session]],
) -> None:
    """A stored vacancy is never left in a state where it cannot be scored.

    ``vacancy_skill`` was empty for all 643 rows precisely because deriving it
    was a separate later pass that nobody ran. Doing it in the same transaction
    as the write is what makes that impossible to repeat, and this is the
    assertion that says so: one derivation per batch, over the ids that batch
    just returned, and never a batch that got written without one.
    """
    postings = [posting("healthy", str(index)) for index in range(UPSERT_BATCH + 4)]
    source = Healthy(postings, challenged=False)

    await _run_source(source, PLAN, sessions)

    assert len(Derivation.calls) == len(Vacancies.batches), "a batch was written underived"
    assert [len(call) for call in Derivation.calls] == [len(batch) for batch in Vacancies.batches]


async def test_a_challenge_in_one_source_does_not_stop_another(
    sessions: Callable[[], AbstractAsyncContextManager[Session]],
) -> None:
    """The containment the crawl depends on, asserted where it actually lives.

    run_pipeline gathers the sources with return_exceptions=False, so a
    _run_source that let this exception out would cancel every other connector
    in the run. Both are driven concurrently here for that reason.
    """
    stopped = Challenged([posting("challenged", "1")], challenged=True)
    healthy = Healthy([posting("healthy", str(index)) for index in range(4)], challenged=False)

    outcomes = await asyncio.gather(
        _run_source(stopped, PLAN, sessions),
        _run_source(healthy, PLAN, sessions),
        return_exceptions=False,
    )

    by_slug = {outcome.slug: outcome for outcome in outcomes}
    assert by_slug["challenged"].challenged is True
    assert by_slug["healthy"].challenged is False
    assert by_slug["healthy"].status is PipelineRunStatus.SUCCESS
    assert by_slug["healthy"].found == 4
    assert by_slug["healthy"].new == 4


async def test_a_broken_source_is_still_a_broken_source(
    sessions: Callable[[], AbstractAsyncContextManager[Session]],
) -> None:
    """The distinction has to cut both ways or it is not a distinction.

    A connector with a bug in it must not start reading as "the site refused
    us", which would send a real failure to be rescheduled forever instead of
    fixed.
    """
    outcome = await _run_source(Broken(), PLAN, sessions)

    assert outcome.challenged is False
    assert outcome.status is PipelineRunStatus.FAILED
    assert [error["stage"] for error in outcome.errors] == ["crawl"]


async def test_a_failed_partial_write_does_not_hide_why_the_run_stopped(
    sessions: Callable[[], AbstractAsyncContextManager[Session]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write of what was already bought happens while unwinding.

    If it fails there and is allowed to propagate, it replaces the exception
    that stopped the crawl -- and the run is then filed as a database problem
    when what actually happened is that the site refused us. The classification
    is the whole point of this module, so the second failure is logged and the
    first one still wins.
    """

    class Failing(Vacancies):
        """A repository that cannot write, to fail during the unwind."""

        async def bulk_upsert(self, items: list[UpsertItem]) -> BulkUpsertResult:
            """Break the way a connection pool breaks."""
            raise RuntimeError("the connection went away")

    monkeypatch.setattr(runner_module, "VacancyRepository", Failing)
    source = Challenged([posting("challenged", str(index)) for index in range(3)], challenged=True)

    outcome = await _run_source(source, PLAN, sessions)

    assert outcome.challenged is True
    assert [error["error"] for error in outcome.errors] == ["HHChallengedError"]


async def test_a_slow_source_writes_before_its_batch_is_full(
    sessions: Callable[[], AbstractAsyncContextManager[Session]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A count alone is the wrong measure of when to write.

    hh is crawled at about one page every four to five seconds, so a batch of a
    hundred is seven and a half minutes of work held in memory, and every hh crawl
    so far has ended before then. The challenge path rescues its batch, so that
    ending is covered; a hard signal is not, and neither is a run the operator
    stops. Measured with this rule in place: a crawl cut off after 200 seconds
    stored 31 postings and recorded positions for two sitemap files, none of
    which a full batch would have reached.

    The clock is monkeypatched rather than waited on, because a test that sleeps
    for a minute is a test nobody runs.
    """
    # A clock that advances with the CRAWL rather than with calls to it: forty
    # seconds a page, which is the shape of a slow source and is deterministic
    # however many other things read the clock. Two pages then exceed the limit.
    # Anything that leaves the batch to the end of the run writes four at once.
    handed_over = [0]
    step = BATCH_MAX_AGE_SECONDS / 1.5
    monkeypatch.setattr(runner.time, "monotonic", lambda: handed_over[0] * step)
    postings = [posting("healthy", str(index)) for index in range(4)]
    assert len(postings) < UPSERT_BATCH, "so only the age rule can trigger a write"
    source = Ticking(postings, challenged=False, clock=handed_over)

    await _run_source(source, PLAN, sessions)

    assert [len(batch) for batch in Vacancies.batches] == [2, 2], (
        "the aged batch must be written during the walk, not left to the end"
    )
    assert source.confirmed == [2, 4], "and the crawl position told about each"


async def test_a_fast_source_still_writes_by_count(
    sessions: Callable[[], AbstractAsyncContextManager[Session]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The age rule must not turn a busy feed into a write per posting."""
    monkeypatch.setattr(runner.time, "monotonic", lambda: 0.0)
    postings = [posting("healthy", str(index)) for index in range(UPSERT_BATCH + 7)]
    source = Healthy(postings, challenged=False)

    await _run_source(source, PLAN, sessions)

    assert [len(batch) for batch in Vacancies.batches] == [UPSERT_BATCH, 7]
