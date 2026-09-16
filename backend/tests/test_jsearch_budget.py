"""JSearch spends a fixed slice of a monthly allowance, across the whole plan.

Measured 2026-09-16: the plan is 200 requests a month, and the source had never
run because its key sat in ``RAPIDAPI_KEY``. Each test below pins one of the
rules that makes the allowance last and reach every search:

* a run buys :data:`PAGES_PER_RUN` pages and no more;
* those pages go one per search, and the next run continues with the search
  after the last one asked;
* a search that has more pages continues from its cursor on its next turn;
* the place is in the text and the country is derived, because JSearch has no
  area parameter and defaults to the United States.
"""

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.core.config import settings
from app.db.enums import RemoteType
from app.schemas.crawl import SavedState, SearchUse
from app.sources.base import SearchQuery
from app.sources.http import SourceClient
from app.sources.jsearch import (
    BASE_URL,
    DAILY_ALLOWANCE,
    MONTHLY_QUOTA,
    PAGES_PER_RUN,
    ROTATION_KEY,
    RUN_EVERY,
    CountryAliases,
    JSearchSource,
    PlannedSearch,
    Rotation,
    country_for,
    load_countries,
    parse_rotation,
    planned_searches,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "sources"

KZ = (CountryAliases(code="kz", aliases=("алматы", "almaty")),)


def payload(name: str) -> dict[str, Any]:
    """One saved response, re-read per call so no test can mutate another's."""
    with (FIXTURES / f"{name}.json").open(encoding="utf-8") as handle:
        data: dict[str, Any] = json.load(handle)
    return data


async def _instant(_seconds: float) -> None:
    """Stand in for ``asyncio.sleep``."""
    return None


class MemoryState:
    """The pipeline's state hooks, kept in a dict."""

    def __init__(self, stored: dict[str, Any] | None = None) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        if stored is not None:
            self.rows[ROTATION_KEY] = stored

    async def load(self, key: str) -> dict[str, Any] | None:
        """What was saved under ``key``."""
        return self.rows.get(key)

    async def save(self, key: str, value: dict[str, Any]) -> None:
        """Save ``value`` under ``key``."""
        self.rows[key] = value

    @property
    def rotation(self) -> Rotation:
        """The rotation as last saved."""
        return Rotation.model_validate(self.rows[ROTATION_KEY])


@pytest.fixture
def http() -> Iterator[respx.MockRouter]:
    """Every outbound request, intercepted."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SourceClient]:
    """The shared client with the disk cache off and no waiting."""
    monkeypatch.setattr(settings, "http_cache_dir", None)
    source_client = SourceClient(sleep=_instant)
    try:
        yield source_client
    finally:
        await source_client.aclose()


@pytest.fixture
def state() -> MemoryState:
    """A fresh, empty state store."""
    return MemoryState()


@pytest.fixture
def jsearch(
    client: SourceClient, state: MemoryState, monkeypatch: pytest.MonkeyPatch
) -> JSearchSource:
    """A connector keyed through the older variable, with in-memory state."""
    monkeypatch.setattr(settings, "source_credentials", {})
    monkeypatch.setattr(settings, "rapidapi_key", SecretStr("legacy"))
    source = JSearchSource()
    return source.bind(client.bind(source)).with_state(state.load, state.save)


def plan(*titles: str, area: str | None = "Алматы") -> list[SearchQuery]:
    """A plan of one query per title, in one place."""
    return [SearchQuery(keywords=(title,), area=area, headline="x") for title in titles]


async def drain(source: JSearchSource, queries: list[SearchQuery]) -> list[str]:
    """Run one batch and return the external ids it yielded."""
    return [posting.external_id async for posting in source.search_batch(queries)]


def texts(route: respx.Route) -> list[str]:
    """The ``query`` parameter of every request the route served."""
    return [call.request.url.params["query"] for call in route.calls]


# ── the allowance ─────────────────────────────────────────────────────


def test_the_budget_fits_the_measured_month() -> None:
    """Two full runs a day for a 31-day month stay inside the plan."""
    runs_per_day = int(86_400 // RUN_EVERY.total_seconds())

    assert DAILY_ALLOWANCE * 31 <= MONTHLY_QUOTA
    assert PAGES_PER_RUN * runs_per_day <= DAILY_ALLOWANCE
    assert JSearchSource.daily_quota == DAILY_ALLOWANCE
    assert JSearchSource.min_interval == RUN_EVERY


def test_the_key_in_rapidapi_key_configures_the_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """This is the line that kept the source from ever running."""
    monkeypatch.setattr(settings, "source_credentials", {})
    monkeypatch.setattr(settings, "rapidapi_key", SecretStr("legacy"))

    assert JSearchSource().unavailable() is None


def test_without_either_key_the_source_names_the_missing_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The framework's own answer still stands when nothing is set."""
    monkeypatch.setattr(settings, "source_credentials", {})
    monkeypatch.setattr(settings, "rapidapi_key", None)

    assert JSearchSource().missing_credentials() == ("jsearch.rapidapi_key",)


async def test_a_run_buys_exactly_its_pages_one_per_search(
    jsearch: JSearchSource, http: respx.MockRouter, state: MemoryState
) -> None:
    """Five searches, a budget of three: the first three each get one page."""
    route = http.get(BASE_URL).mock(return_value=httpx.Response(200, json=payload("jsearch_page1")))

    await drain(jsearch, plan("A", "B", "C", "D", "E"))

    assert route.call_count == PAGES_PER_RUN
    assert texts(route) == ["A Алматы", "B Алматы", "C Алматы"][:PAGES_PER_RUN]
    assert state.rotation.offset == PAGES_PER_RUN
    assert {call.request.headers["X-RapidAPI-Key"] for call in route.calls} == {"legacy"}


async def test_the_next_run_starts_where_the_last_one_stopped(
    client: SourceClient, http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Across runs the turn order wraps, so the fifth search is not starved."""
    monkeypatch.setattr(settings, "rapidapi_key", SecretStr("legacy"))
    route = http.get(BASE_URL).mock(return_value=httpx.Response(200, json=payload("jsearch_page2")))
    state = MemoryState(Rotation(offset=3).model_dump(mode="json"))
    source = JSearchSource()
    source = source.bind(client.bind(source)).with_state(state.load, state.save)

    await drain(source, plan("A", "B", "C", "D", "E"))

    assert texts(route) == ["D Алматы", "E Алматы", "A Алматы"][:PAGES_PER_RUN]


async def test_a_search_with_more_pages_continues_from_its_cursor(
    jsearch: JSearchSource, http: respx.MockRouter, state: MemoryState
) -> None:
    """One search in the plan: its second turn reads page two, not page one again."""
    route = http.get(BASE_URL).mock(
        side_effect=[
            httpx.Response(200, json=payload("jsearch_page1")),
            httpx.Response(200, json=payload("jsearch_page2")),
            httpx.Response(200, json=payload("jsearch_page1")),
        ]
    )

    ids = await drain(jsearch, plan("Python Developer"))

    cursors = [call.request.url.params.get("cursor") for call in route.calls]
    assert cursors[:2] == [None, "PAGE2CURSOR"]
    # Page two had no cursor, so the third turn starts the search over.
    if PAGES_PER_RUN >= 3:
        assert cursors[2] is None
    assert len(ids) == len(set(ids)), "a posting seen on two pages is yielded once"


async def test_the_cursor_survives_into_the_next_run(
    jsearch: JSearchSource, http: respx.MockRouter, state: MemoryState
) -> None:
    """The saved position names the page to read next, per search."""
    http.get(BASE_URL).mock(return_value=httpx.Response(200, json=payload("jsearch_page1")))

    await drain(jsearch, plan("A", "B", "C", "D", "E"))

    key = PlannedSearch(text="A Алматы", country="kz").key
    assert state.rotation.cursors[key].cursor == "PAGE2CURSOR"
    assert state.rotation.cursors[key].page == 2


async def test_searches_that_left_the_plan_are_forgotten(
    client: SourceClient, http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A title the owner removed must not keep a cursor alive forever."""
    monkeypatch.setattr(settings, "rapidapi_key", SecretStr("legacy"))
    http.get(BASE_URL).mock(return_value=httpx.Response(200, json=payload("jsearch_page2")))
    stale = {"offset": 0, "cursors": {"deadbeefdeadbeef": {"cursor": "OLD", "page": 4}}}
    state = MemoryState(stale)
    source = JSearchSource()
    source = source.bind(client.bind(source)).with_state(state.load, state.save)

    await drain(source, plan("A"))

    assert "deadbeefdeadbeef" not in state.rotation.cursors


async def test_an_empty_plan_spends_nothing(jsearch: JSearchSource, http: respx.MockRouter) -> None:
    """No words, no request: a keyword-less search is a download of everything."""
    route = http.get(BASE_URL).mock(return_value=httpx.Response(200, json=payload("jsearch_page1")))

    assert await drain(jsearch, [SearchQuery()]) == []
    assert route.call_count == 0


async def test_the_query_limit_stops_the_run_early(
    jsearch: JSearchSource, http: respx.MockRouter
) -> None:
    """A limit of two postings does not buy a second page."""
    route = http.get(BASE_URL).mock(return_value=httpx.Response(200, json=payload("jsearch_page1")))

    queries = [SearchQuery(keywords=(word,), limit=2) for word in ("A", "B")]

    ids = await drain(jsearch, queries)

    assert len(ids) == 2
    assert route.call_count == 1


# ── what is asked ─────────────────────────────────────────────────────


def test_the_place_goes_into_the_text_and_the_country_is_derived() -> None:
    """JSearch has no area parameter and defaults to the United States."""
    searches = planned_searches(
        [
            SearchQuery(keywords=("Python Developer",), area="Алматы"),
            SearchQuery(keywords=("Python Developer",), remote=RemoteType.FULL),
            SearchQuery(keywords=("Python Developer",)),
            SearchQuery(keywords=("Python Developer",), area="Алматы"),
        ],
        KZ,
    )

    assert searches == (
        PlannedSearch(text="Python Developer Алматы", country="kz"),
        PlannedSearch(text="Python Developer remote"),
        PlannedSearch(text="Python Developer"),
    )


def test_a_country_nobody_listed_is_left_unset() -> None:
    """Guessing a country narrows the search to nothing, silently."""
    assert country_for("Тбилиси", KZ) is None
    assert country_for(None, KZ) is None
    assert country_for("Алматы, Казахстан", KZ) == "kz"


def test_the_shipped_country_table_knows_kazakhstan() -> None:
    """The deployment's own city has to resolve, or the queries go to the US."""
    assert country_for("Алматы", load_countries()) == "kz"
    assert country_for("Astana", load_countries()) == "kz"


def test_an_explicit_country_on_the_query_wins() -> None:
    """A hand-built query that names its country is taken at its word."""
    searches = planned_searches([SearchQuery(keywords=("x",), area="Алматы", country="DE")], KZ)

    assert searches[0].country == "de"


def test_a_broken_rotation_record_starts_the_order_over() -> None:
    """Losing the turn order costs a few repeated pages, not a failed run."""
    assert parse_rotation({"offset": -3}) == Rotation()
    assert parse_rotation(None) == Rotation()


def test_the_publisher_is_read_from_the_payload() -> None:
    """«jsearch → LinkedIn» is what the card shows next to the source."""
    source = JSearchSource()

    assert source.publisher_of({"job_publisher": " LinkedIn "}) == "LinkedIn"
    assert source.publisher_of({"job_publisher": ""}) is None
    assert source.publisher_of({}) is None


def test_the_preview_lists_the_searches_in_turn_order() -> None:
    """What the owner sees is what the next runs will send, starting at the offset."""
    source = JSearchSource()
    stored = [
        SavedState(
            key=ROTATION_KEY,
            value=Rotation(offset=1).model_dump(mode="json"),
            updated_at="2026-09-16T00:00:00Z",
        )
    ]

    preview = source.preview_search(plan("A", "B"), stored)

    assert preview.use is SearchUse.QUERY
    assert preview.terms == ["B Алматы · KZ", "A Алматы · KZ"]
    assert str(MONTHLY_QUOTA) in (preview.note or "")
