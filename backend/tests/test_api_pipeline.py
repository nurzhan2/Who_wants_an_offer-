"""POST /pipeline/run enqueues, and the job it hands back can be polled.

The endpoint used to await the crawl. It cannot: hh walks a corpus rather than a
feed and one slice of it is roughly twenty minutes, which is not a request
anybody can hold open. So what is defended here is the shape rather than the
counters — the counters were already covered next door, in ``test_sources_api``.

Four properties, one per decision the shape makes:

* **the request returns while the crawl is still running**, which is the whole
  point and the one thing a timing-free test can actually pin down (the crawl
  here parks on an event that only the test can set);
* **a second crawl is refused**, because two concurrent runs of one source share
  a rate limiter and double the request rate against a host that has already
  answered one of our runs with a captcha;
* **a job that stops running never stays ``running``** — not when the crawl
  crashes, not when the task is cancelled — because a handle stuck at "running"
  is worse than no handle;
* **a job is gone after a restart**, which is what the in-memory registry buys
  and is tested rather than hoped for: a fresh registry is exactly what the next
  process sees.

Nothing here touches PostgreSQL. The job endpoints do not read the database at
all, which is why they can be exercised on a laptop with no Docker.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.exceptions import AppError
from app.pipeline import runner as runner_module
from app.pipeline.embedding import EmbeddingOutcome
from app.pipeline.runner import RunReport, SourceOutcome
from app.services import pipeline as pipeline_service
from app.sources.base import SourceUnavailable, Unavailable
from app.sources.query_planner import QueryPlan

pytestmark = pytest.mark.unit

RUN_URL = f"{settings.api_v1_prefix}/pipeline/run"
JOBS_URL = f"{settings.api_v1_prefix}/pipeline/jobs"

#: Long enough to catch a hang, short enough that a broken test still ends.
PATIENCE = 5.0


def _report(**overrides: Any) -> RunReport:
    """A finished run, without running anything."""
    report = RunReport(plan=QueryPlan(queries=(), groups=("backend",), placements=2, limit=8))
    report.sources = [
        SourceOutcome(slug="arbeitnow", found=10, new=6, updated=4, duplicates=2, requests=3),
        SourceOutcome(
            slug="jsearch",
            skipped=Unavailable(
                code=SourceUnavailable.MISSING_CREDENTIALS,
                detail="no key",
                missing_credentials=("jsearch.rapidapi_key",),
            ),
        ),
    ]
    report.embedding = EmbeddingOutcome(considered=10, unchanged=4, embedded=6)
    for name, value in overrides.items():
        setattr(report, name, value)
    return report


class FakeCrawl:
    """The pipeline, replaced by something the test decides the timing of.

    Without this the endpoint would open its own session, reach the network and
    spend real credits — the same reason the resume tests stub the background
    parse. The event is what makes the async assertions deterministic: the crawl
    stops exactly where the test wants it and moves only when the test says so.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        self.blocking = False
        self.error: Exception | None = None

    async def __call__(self, **kwargs: Any) -> RunReport:
        """Stand in for ``runner.run_pipeline``.

        A dry run never parks on the gate, whatever the test set: it fetches
        nothing in the real pipeline either, and holding one open would make the
        tests that check a dry run *during* a crawl deadlock rather than fail.
        """
        self.calls.append(kwargs)
        self.entered.set()
        if self.blocking and not kwargs.get("dry_run"):
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return _report(dry_run=bool(kwargs.get("dry_run")))


@pytest_asyncio.fixture
async def crawl(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeCrawl]:
    """A stubbed pipeline and a job registry of this test's own.

    The registry is replaced rather than cleared, because that is also what a
    restart looks like from inside this module, and a test that shares one with
    its neighbours would see their in-flight crawls and refuse its own.
    """
    fake = FakeCrawl()
    registry = pipeline_service.JobRegistry()
    monkeypatch.setattr(runner_module, "run_pipeline", fake)
    monkeypatch.setattr(pipeline_service, "_registry", registry)
    yield fake
    # Nothing may be left running: a task destroyed while pending would surface
    # in a later, unrelated test.
    fake.gate.set()
    tasks = [job.task for job in registry.recent(50) if job.task is not None]
    if tasks:
        await asyncio.wait(tasks, timeout=PATIENCE)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """A client with no database wired in, because these routes need none."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def _await_job(client: AsyncClient, job_id: str) -> dict[str, Any]:
    """Poll one job until it stops running, the way a caller would."""

    async def poll() -> dict[str, Any]:
        while True:
            body: dict[str, Any] = (await client.get(f"{JOBS_URL}/{job_id}")).json()
            if body["status"] in {"success", "failed", "cancelled"}:
                return body
            await asyncio.sleep(0)

    return await asyncio.wait_for(poll(), timeout=PATIENCE)


# ── the request does not wait for the crawl ───────────────────────────


async def test_a_crawl_is_accepted_instead_of_awaited(
    client: AsyncClient, crawl: FakeCrawl
) -> None:
    """The answer arrives while the crawl is still going, which is the change.

    Asserted against the crawl's own state rather than against a clock: the
    stub is parked inside ``run_pipeline`` and only this test can release it, so
    a response that arrives at all proves the request did not await the run.
    """
    crawl.blocking = True

    response = await client.post(RUN_URL)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] in {"queued", "running"}
    assert body["report"] is None
    assert crawl.gate.is_set() is False


async def test_the_answer_points_at_the_thing_to_poll(
    client: AsyncClient, crawl: FakeCrawl
) -> None:
    """A 202 whose caller has to guess the poll URL is half an answer."""
    crawl.blocking = True

    response = await client.post(RUN_URL)

    assert response.headers["Location"].endswith(f"{JOBS_URL}/{response.json()['id']}")


async def test_polling_shows_the_crawl_finish_with_its_counters(
    client: AsyncClient, crawl: FakeCrawl
) -> None:
    """The counters are the product of a run; they arrive on the job."""
    crawl.blocking = True
    job_id = (await client.post(RUN_URL)).json()["id"]
    await asyncio.wait_for(crawl.entered.wait(), timeout=PATIENCE)

    running = (await client.get(f"{JOBS_URL}/{job_id}")).json()
    assert running["status"] == "running"
    assert running["duration_seconds"] is not None

    crawl.gate.set()
    finished = await _await_job(client, job_id)

    assert finished["status"] == "success"
    assert finished["report"]["found"] == 10
    assert finished["report"]["new"] == 6
    assert finished["report"]["embedding"]["unchanged"] == 4
    assert finished["finished_at"] is not None


async def test_a_bounded_feed_can_still_be_run_in_one_request(
    client: AsyncClient, crawl: FakeCrawl
) -> None:
    """``wait_seconds`` is how the old synchronous path survives for the feeds
    that finish in seconds: same request, same counters, one level down."""
    response = await client.post(f"{RUN_URL}?source=arbeitnow&wait_seconds=5")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["report"]["found"] == 10


async def test_a_wait_that_runs_out_answers_with_the_running_job(
    client: AsyncClient, crawl: FakeCrawl
) -> None:
    """Waiting is a convenience, not a promise: hh will outlast any wait, and
    the caller has to end up polling rather than holding a dead connection."""
    crawl.blocking = True

    response = await client.post(f"{RUN_URL}?wait_seconds=1")

    assert response.status_code == 202
    assert response.json()["status"] == "running"


# ── one crawl at a time ───────────────────────────────────────────────


async def test_a_second_crawl_is_refused_while_one_is_in_flight(
    client: AsyncClient, crawl: FakeCrawl
) -> None:
    """Two concurrent crawls double the request rate against hosts that have
    already answered one of our runs with a captcha. The refusal names the job
    that is running, so the caller polls instead of retrying blind."""
    crawl.blocking = True
    first = (await client.post(RUN_URL)).json()

    refused = await client.post(RUN_URL)

    assert refused.status_code == 409
    assert refused.headers["content-type"].startswith("application/problem+json")
    problem = refused.json()
    assert problem["job_id"] == first["id"]
    assert problem["type"].endswith("pipeline-busy")
    assert len(crawl.calls) == 1


async def test_a_dry_run_is_never_refused_and_answers_at_once(
    client: AsyncClient, crawl: FakeCrawl
) -> None:
    """Inspecting the plan is exactly what somebody does while a long crawl is
    running, and a dry run fetches nothing, so it cannot be the hazard the
    refusal exists for."""
    crawl.blocking = True
    await client.post(RUN_URL)

    response = await client.post(f"{RUN_URL}?dry_run=true")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["dry_run"] is True
    assert body["report"] is not None
    assert crawl.calls[-1]["dry_run"] is True


async def test_the_slot_is_free_again_once_the_crawl_ends(
    client: AsyncClient, crawl: FakeCrawl
) -> None:
    """A refusal that outlives the run it protects is an outage."""
    crawl.blocking = True
    job_id = (await client.post(RUN_URL)).json()["id"]
    await asyncio.wait_for(crawl.entered.wait(), timeout=PATIENCE)
    crawl.gate.set()
    await _await_job(client, job_id)

    assert (await client.post(RUN_URL)).status_code == 202


# ── a job that stops running never stays "running" ────────────────────


async def test_a_crash_ends_the_job_and_keeps_the_details_out_of_the_body(
    client: AsyncClient, crawl: FakeCrawl
) -> None:
    """An exception's text can carry a URL with a key in it, so the job gets a
    sentence and the log gets the traceback."""
    crawl.error = RuntimeError("postgresql://user:Zx9QvLiveKeyMustNotLeak@db/offers")
    job_id = (await client.post(RUN_URL)).json()["id"]

    finished = await _await_job(client, job_id)

    assert finished["status"] == "failed"
    assert finished["error"]
    assert "Zx9QvLiveKeyMustNotLeak" not in (await client.get(f"{JOBS_URL}/{job_id}")).text


async def test_a_domain_failure_reaches_the_job_in_the_pipelines_own_words(
    client: AsyncClient, crawl: FakeCrawl
) -> None:
    """The pipeline's own words survive. «No active profile» is written for the
    person who has to fix it; losing it leaves them «something went wrong»."""
    crawl.error = AppError("no active profile: upload a resume first")
    job_id = (await client.post(RUN_URL)).json()["id"]

    finished = await _await_job(client, job_id)

    assert finished["status"] == "failed"
    assert finished["error"] == "no active profile: upload a resume first"


async def test_a_cancelled_crawl_says_so_and_frees_the_slot(
    client: AsyncClient, crawl: FakeCrawl
) -> None:
    """What a shutdown looks like from here. The job must not be left running:
    a handle stuck at "running" for ever is worse than no handle."""
    crawl.blocking = True
    job_id = (await client.post(RUN_URL)).json()["id"]
    await asyncio.wait_for(crawl.entered.wait(), timeout=PATIENCE)

    # Reaching into the registry on purpose: cancellation is what a shutdown
    # does to the task, and there is no HTTP verb for "the process is going
    # down". The property under test is what the job looks like afterwards.
    job = pipeline_service._registry.get(UUID(job_id))
    assert job is not None and job.task is not None
    job.task.cancel()
    await asyncio.wait([job.task], timeout=PATIENCE)

    body = (await client.get(f"{JOBS_URL}/{job_id}")).json()
    assert body["status"] == "cancelled"
    assert body["error"]
    assert (await client.post(RUN_URL)).status_code == 202


# ── the job is a handle on this process, and says so ──────────────────


async def test_an_unknown_job_is_a_404_that_explains_itself(
    client: AsyncClient, crawl: FakeCrawl
) -> None:
    """Not a 500 and not an empty object that reads as a job with no counters."""
    response = await client.get(f"{JOBS_URL}/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["type"].endswith("pipeline-job-unknown")


async def test_a_job_does_not_survive_a_restart(
    client: AsyncClient, crawl: FakeCrawl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deliberate consequence of keeping jobs in memory, pinned so it stays
    deliberate. A fresh registry is what the next process sees, and the answer
    is 404 rather than a row that claims to be running with nobody behind it."""
    job_id = (await client.post(f"{RUN_URL}?wait_seconds=5")).json()["id"]
    assert (await client.get(f"{JOBS_URL}/{job_id}")).status_code == 200

    monkeypatch.setattr(pipeline_service, "_registry", pipeline_service.JobRegistry())

    assert (await client.get(f"{JOBS_URL}/{job_id}")).status_code == 404


# ── the request reaches the runner unchanged ──────────────────────────


async def test_source_selection_and_force_reach_the_runner(
    client: AsyncClient, crawl: FakeCrawl
) -> None:
    """Naming sources is how a person crawls the fast feeds without waiting for
    hh, so the selection has to actually arrive."""
    await client.post(f"{RUN_URL}?source=arbeitnow&source=remotive&force=true&wait_seconds=5")

    assert crawl.calls[-1]["source_slugs"] == ["arbeitnow", "remotive"]
    assert crawl.calls[-1]["force"] is True


async def test_an_unknown_source_is_refused_before_a_job_is_opened(
    client: AsyncClient, crawl: FakeCrawl
) -> None:
    """A typo must not take the single in-flight slot, and must not be
    discovered by polling a job that was doomed when it was accepted."""
    response = await client.post(f"{RUN_URL}?source=definitely_not_a_source")

    assert response.status_code >= 400
    assert crawl.calls == []
    assert (await client.get(JOBS_URL)).json()["jobs"] == []


# ── the list of jobs ──────────────────────────────────────────────────


async def test_the_job_list_is_newest_first_and_says_whether_one_is_running(
    client: AsyncClient, crawl: FakeCrawl
) -> None:
    """The dashboard disables its own button from ``busy``; deriving it from the
    list would make the client repeat the rule for what "in flight" means."""
    await client.post(f"{RUN_URL}?dry_run=true")
    crawl.blocking = True
    latest = (await client.post(RUN_URL)).json()["id"]

    body = (await client.get(JOBS_URL)).json()

    assert body["busy"] is True
    assert body["jobs"][0]["id"] == latest
    assert len(body["jobs"]) == 2


async def test_a_running_job_is_never_forgotten_to_make_room(
    client: AsyncClient, crawl: FakeCrawl
) -> None:
    """Evicting the in-flight job would lose the handle on a crawl that is still
    making requests, and its caller would never learn how it ended."""
    crawl.blocking = True
    running = (await client.post(RUN_URL)).json()["id"]
    for _ in range(pipeline_service.JOB_HISTORY + 3):
        assert (await client.post(f"{RUN_URL}?dry_run=true")).status_code == 200

    body = (await client.get(JOBS_URL)).json()

    assert len(body["jobs"]) == pipeline_service.JOB_HISTORY
    assert running in {job["id"] for job in body["jobs"]}
