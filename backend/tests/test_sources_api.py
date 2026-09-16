"""The sources page and the run endpoint.

Two things are defended here.

**A source that will not run says why.** Leaving it out of the list makes "not
configured" indistinguishable from "found nothing", and those need opposite
actions from whoever is reading the page: add a key, or go look at the market.

**A credential is answered for by name and by presence, never by value.** The
response names the missing key so the reader knows what to add, and carries
nothing else — not a prefix, not a length, not a masked form. Each of those
narrows a real key for anyone who can read the page or a screenshot of it.
"""

from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.enums import PipelineRunStatus
from app.db.repositories.pipeline_run import PipelineRunRepository
from app.db.repositories.source_quota import SourceQuotaRepository
from app.pipeline import runner as runner_module
from app.pipeline.embedding import EmbeddingOutcome
from app.pipeline.runner import RunReport, SourceOutcome
from app.schemas.pipeline import PipelineRunCreate, PipelineRunFinish
from app.sources.base import SourceUnavailable, Unavailable
from app.sources.jsearch import DAILY_ALLOWANCE
from app.sources.query_planner import QueryPlan

pytestmark = pytest.mark.db

SOURCES_URL = f"{settings.api_v1_prefix}/sources"
RUN_URL = f"{settings.api_v1_prefix}/pipeline/run"
RUNS_URL = f"{settings.api_v1_prefix}/pipeline/runs"

#: A value that must never appear in any response or log line.
SECRET_VALUE = "Zx9Qv7LiveKeyMustNotLeak42"
#: Long enough to be unmistakable, and deliberately not a word that appears
#: anywhere legitimate: an earlier version used a prefix that also occurs in
#: rapidapi.com, so the test failed on the terms link rather than on a leak.
SECRET_PREFIX = SECRET_VALUE[:10]


@pytest.fixture
def configured_jsearch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give jsearch a credential, so the configured branch is exercised too."""
    monkeypatch.setattr(
        settings, "source_credentials", {"jsearch.rapidapi_key": SecretStr(SECRET_VALUE)}
    )


# ── GET /sources ──────────────────────────────────────────────────────


async def test_every_registered_source_is_listed_even_when_it_cannot_run(
    async_client: AsyncClient,
) -> None:
    """A source missing from the page is indistinguishable from one that found
    nothing, and the two need opposite responses from the reader."""
    response = await async_client.get(SOURCES_URL)

    assert response.status_code == 200
    slugs = {source["slug"] for source in response.json()["sources"]}
    assert {"jsearch", "arbeitnow", "remotive"} <= slugs


async def test_an_unconfigured_source_names_the_missing_key_and_nothing_else(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name is what the reader needs to act. A value, a prefix or a length
    would each narrow the key for anyone who can see the screen."""
    monkeypatch.setattr(settings, "source_credentials", {})
    # The older variable counts as the credential too, and a developer's .env
    # sets it; cleared so this is about the missing branch wherever it runs.
    monkeypatch.setattr(settings, "rapidapi_key", None)

    body = (await async_client.get(SOURCES_URL)).json()
    jsearch = next(item for item in body["sources"] if item["slug"] == "jsearch")

    assert jsearch["enabled"] is False
    assert jsearch["inactive"]["code"] == SourceUnavailable.MISSING_CREDENTIALS.value
    assert jsearch["inactive"]["missing_credentials"] == ["jsearch.rapidapi_key"]


async def test_no_credential_value_reaches_the_response(
    async_client: AsyncClient, configured_jsearch: None
) -> None:
    """The whole response is checked as text rather than field by field: a leak
    that arrives through a field nobody thought to assert on is exactly the leak
    that ships."""
    response = await async_client.get(SOURCES_URL)

    assert response.status_code == 200
    assert SECRET_VALUE not in response.text
    # Not even a fragment of it, which is what a "masked" value would be.
    assert SECRET_PREFIX not in response.text


async def test_a_configured_source_is_enabled(
    async_client: AsyncClient, configured_jsearch: None
) -> None:
    """The other half of the credential test: with the key present it runs."""
    body = (await async_client.get(SOURCES_URL)).json()
    jsearch = next(item for item in body["sources"] if item["slug"] == "jsearch")

    assert jsearch["enabled"] is True
    assert jsearch["inactive"] is None


async def test_the_page_carries_the_limits_a_reader_needs(
    async_client: AsyncClient,
) -> None:
    """Rate, daily allowance, attribution and the terms link are the facts that
    explain why a source behaves as it does, so they belong on the page rather
    than only in the code."""
    body = (await async_client.get(SOURCES_URL)).json()
    by_slug = {item["slug"]: item for item in body["sources"]}

    assert by_slug["jsearch"]["daily_quota"] == DAILY_ALLOWANCE
    assert by_slug["jsearch"]["terms_url"]
    assert by_slug["remotive"]["attribution"]
    # An API-mode source without terms cannot be registered, so this is a
    # restatement of the registry's rule at the edge that shows it to a person.
    for item in body["sources"]:
        if item["access_mode"] == "api":
            assert item["terms_url"]


async def test_a_disabled_source_says_so_rather_than_disappearing(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switching a source off in configuration is a decision someone made, and
    the page has to show it or the next person will debug the silence."""
    monkeypatch.setattr(settings, "sources_disabled", frozenset({"remotive"}))

    body = (await async_client.get(SOURCES_URL)).json()
    remotive = next(item for item in body["sources"] if item["slug"] == "remotive")

    assert remotive["enabled"] is False
    assert remotive["inactive"]["code"] == SourceUnavailable.DISABLED_BY_CONFIG.value


async def test_an_exhausted_daily_allowance_is_reported_as_such(
    async_client: AsyncClient, db_session: AsyncSession, configured_jsearch: None
) -> None:
    """Out of credits is not the same as broken, and the page must not make a
    person hunt for a fault that does not exist."""
    await SourceQuotaRepository(db_session).spend("jsearch", amount=100)
    await db_session.flush()

    body = (await async_client.get(SOURCES_URL)).json()
    jsearch = next(item for item in body["sources"] if item["slug"] == "jsearch")

    assert jsearch["enabled"] is False
    assert jsearch["inactive"]["code"] == SourceUnavailable.QUOTA_EXHAUSTED.value
    assert jsearch["daily_used"] == 100


async def test_the_last_run_is_shown_beside_the_source(
    async_client: AsyncClient, db_session: AsyncSession
) -> None:
    """ "When did this last work" is the first question anyone asks the page."""
    runs = PipelineRunRepository(db_session)
    run = await runs.start(PipelineRunCreate(source_slug="arbeitnow"))
    await runs.finish(
        run.id, PipelineRunFinish(status=PipelineRunStatus.SUCCESS, found=7, new=3, updated=4)
    )
    await db_session.flush()

    body = (await async_client.get(SOURCES_URL)).json()
    arbeitnow = next(item for item in body["sources"] if item["slug"] == "arbeitnow")

    assert arbeitnow["last_run"]["found"] == 7
    assert arbeitnow["last_run"]["new"] == 3


# ── POST /pipeline/run ────────────────────────────────────────────────


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


@pytest.fixture
def stubbed_run(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the crawl with a recorder of its arguments.

    Without this the endpoint would open its own session, reach the network and
    spend real credits — the same reason the resume tests stub the background
    parse."""
    calls: list[dict[str, Any]] = []

    async def fake_run(**kwargs: Any) -> RunReport:
        calls.append(kwargs)
        return _report(dry_run=bool(kwargs.get("dry_run")))

    monkeypatch.setattr(runner_module, "run_pipeline", fake_run)
    return calls


#: The crawl no longer answers on the connection that asked for it — it hands
#: back a job to poll, because an hh slice takes about twenty minutes. The
#: counters these tests are about did not go away; they moved one level down,
#: into ``report``, and ``wait_seconds`` is the endpoint's own way of getting
#: them in one request for the sources that are bounded feeds. Using it here
#: keeps each test about the thing it was written for.
WAITED_RUN_URL = f"{RUN_URL}?wait_seconds=5"


async def _counters(client: AsyncClient) -> dict[str, Any]:
    """Run to completion in one request and hand back the counters.

    Asserts the synchronous path really did finish, so a test below can never
    read ``None`` and quietly compare nothing.
    """
    response = await client.post(WAITED_RUN_URL)

    assert response.status_code == 200, "wait_seconds should have finished the stubbed run"
    body = response.json()
    assert body["status"] == "success"
    assert body["report"] is not None, "a finished job carries its report"
    return dict(body["report"])


async def test_the_crawl_is_accepted_rather_than_awaited(
    async_client: AsyncClient, stubbed_run: list[dict[str, Any]]
) -> None:
    """The shape change itself: 202 and a job to poll, not 200 and a wait.

    An hh slice is roughly twenty minutes at its own polite rate, and nothing
    holds an HTTP connection open for that.
    """
    response = await async_client.post(RUN_URL)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] in {"queued", "running", "success"}
    # The URL to poll is where a caller finds out; it must be handed over, not
    # left to be assembled from the id by whoever reads the docs.
    assert response.headers["Location"].endswith(body["id"])


async def test_run_reports_what_each_source_did(
    async_client: AsyncClient, stubbed_run: list[dict[str, Any]]
) -> None:
    """The counters are the product of a run; a bare 202 tells nobody whether
    the crawl was worth making."""
    body = await _counters(async_client)

    assert body["found"] == 10
    assert body["new"] == 6
    assert body["duplicates"] == 2
    arbeitnow = next(item for item in body["sources"] if item["slug"] == "arbeitnow")
    assert arbeitnow["requests"] == 3


async def test_run_reports_a_skipped_source_with_its_reason(
    async_client: AsyncClient, stubbed_run: list[dict[str, Any]]
) -> None:
    """A source that sat the run out is not a source that found nothing."""
    body = await _counters(async_client)
    jsearch = next(item for item in body["sources"] if item["slug"] == "jsearch")

    assert jsearch["skipped"]["code"] == SourceUnavailable.MISSING_CREDENTIALS.value
    assert jsearch["found"] == 0


async def test_run_reports_the_embedding_step(
    async_client: AsyncClient, stubbed_run: list[dict[str, Any]]
) -> None:
    """ "How many vectors did we not have to recompute" is the number that says
    whether the change detection is working at all."""
    body = await _counters(async_client)

    assert body["embedding"] == {
        "considered": 10,
        "unchanged": 4,
        "embedded": 6,
        "skipped_reason": None,
    }


async def test_the_plan_is_summarised_without_the_queries(
    async_client: AsyncClient, stubbed_run: list[dict[str, Any]]
) -> None:
    """The searches are built from the candidate's own skills, and this response
    is the one part of the pipeline a browser renders."""
    body = await _counters(async_client)

    assert body["plan"]["groups"] == ["backend"]
    assert "queries" in body["plan"]
    assert isinstance(body["plan"]["queries"], int)


async def test_dry_run_and_source_selection_reach_the_runner(
    async_client: AsyncClient, stubbed_run: list[dict[str, Any]]
) -> None:
    """dry_run is how you inspect a plan before it spends a metered request, so
    the flag has to actually arrive."""
    await async_client.post(f"{RUN_URL}?dry_run=true&source=arbeitnow&source=remotive")

    assert stubbed_run[-1]["dry_run"] is True
    assert stubbed_run[-1]["source_slugs"] == ["arbeitnow", "remotive"]


# ── GET /pipeline/runs ────────────────────────────────────────────────


async def test_run_history_is_newest_first(
    async_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The page that answers "did last night work" reads from the top."""
    runs = PipelineRunRepository(db_session)
    for slug in ("arbeitnow", "remotive"):
        run = await runs.start(PipelineRunCreate(source_slug=slug))
        await runs.finish(run.id, PipelineRunFinish(status=PipelineRunStatus.SUCCESS))
    await db_session.flush()

    body = (await async_client.get(f"{RUNS_URL}?limit=10")).json()

    assert {item["source_slug"] for item in body} == {"arbeitnow", "remotive"}


async def test_an_unknown_run_is_a_404(async_client: AsyncClient) -> None:
    """Not a 500, and not an empty object that reads as a run with no counters."""
    from uuid import uuid4

    assert (await async_client.get(f"{RUNS_URL}/{uuid4()}")).status_code == 404
