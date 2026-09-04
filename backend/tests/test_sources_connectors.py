"""The three connectors, driven against the responses their sources really sent.

Every payload below comes from ``fixtures/sources``. Two of the three sets are
live captures — arbeitnow's three pages and remotive's whole feed — and the
JSearch ones say in their own ``_comment`` that they were reconstructed from the
behaviour recorded in the phase-3 brief, because this checkout has no RapidAPI
key. That distinction is worth keeping in mind while reading the JSearch tests:
they pin the rules the brief measured, not bytes anyone has seen twice.

Nothing here reaches the network. ``respx`` serves every request, so an
unmocked URL fails the test instead of quietly hitting a vendor whose terms cap
us at four calls a day.

What these tests exist to protect is a specific class of regression: a connector
that still returns postings after somebody "tidies up" a rule that looks
arbitrary. Each of those rules cost a live call to discover and none of them is
self-evident from the code —

* ``date_posted=all`` looks like a missing feature and is the difference
  between ten results and none;
* ``job_uid`` looks like a worse identifier than ``job_id`` and is the only
  stable one;
* remotive's empty query string looks like an unfinished filter and is the
  measured truth about an endpoint that ignores its own documented parameters;
* arbeitnow's per-slug deduplication looks like paranoia and is the reason a
  three-page walk does not insert the same posting twice.

A connector that drops any one of them still compiles, still runs, and still
looks like it works.
"""

import json
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.core.config import settings
from app.db.enums import RemoteType
from app.sources.arbeitnow import API_URL as ARBEITNOW_URL
from app.sources.arbeitnow import ArbeitnowSource
from app.sources.base import BaseSource, RawPosting, SearchQuery
from app.sources.http import SourceClient
from app.sources.jsearch import (
    BASE_URL as JSEARCH_URL,
)
from app.sources.jsearch import (
    DATE_POSTED,
    JSearchSource,
    clean_location,
    looks_remote,
)
from app.sources.remotive import FEED_URL as REMOTIVE_URL
from app.sources.remotive import PUBLISHED_AT_KEY, RemotiveSource

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "sources"

#: The credential JSearch needs. A value, not a real key: the connector only
#: ever reads it to build a header, and asserting on that header is how we know
#: configuration reached the wire.
RAPIDAPI_KEY = SecretStr("test")

#: arbeitnow is a CRAWL source, so the shared client asks the host for its
#: robots.txt before every first request. Allowing everything is what the live
#: file does; stating it here keeps the test about the connector.
ROBOTS_ALLOW_ALL = "User-agent: *\nAllow: /\n"

#: The one posting in arbeitnow pages two and three whose text mentions robots.
#: Chosen because it matches exactly one slug, so a filter that silently stopped
#: filtering would return ten and be caught.
ROBOTICS_SLUG = "robotics-software-engineer-grasping-munich-440025"

#: The two slugs arbeitnow served on both page two and page three. Twelve
#: entries, ten distinct postings — the overlap the live capture preserves.
REPEATED_SLUGS = (
    "senior-customer-success-manager-munchen-bavaria-474817",
    "product-manager-bsg-376558",
)

#: remotive's Freelance Copywriter: the entry carrying the naive timestamp, the
#: free-text salary and the company name with a trailing space.
COPYWRITER_ID = "1749306"


def payload(name: str) -> dict[str, Any]:
    """One saved response, re-read per call so no test can mutate another's."""
    with (FIXTURES / f"{name}.json").open(encoding="utf-8") as handle:
        data: dict[str, Any] = json.load(handle)
    return data


def responses(*payloads: dict[str, Any]) -> list[httpx.Response]:
    """The given payloads as 200s, in the order the connector will ask for them."""
    return [httpx.Response(200, json=body) for body in payloads]


def empty_arbeitnow_page() -> dict[str, Any]:
    """A well-formed arbeitnow page with nothing on it: the feed's own terminator."""
    return {"data": [], "links": {}, "meta": {}}


def query_params(route: respx.Route) -> list[dict[str, str]]:
    """The query string of every request this route served, in order."""
    return [dict(call.request.url.params) for call in route.calls]


async def collect(source: BaseSource, query: SearchQuery | None = None) -> list[RawPosting]:
    """Everything one search yields, drained the way the pipeline drains it."""
    return [posting async for posting in source.search(query or SearchQuery())]


async def _instant(_seconds: float) -> None:
    """Stand in for ``asyncio.sleep``, so a token bucket costs no wall clock."""
    return None


class KnownIdsSpy:
    """The pipeline's "have we got this already?" hook, without a database.

    Records what it was asked as well as what it answered: a connector that
    stopped consulting the hook at all would otherwise pass the negative test
    below for entirely the wrong reason.
    """

    def __init__(self, *held: str) -> None:
        self.held = set(held)
        self.asked: list[list[str]] = []

    async def __call__(self, candidates: Sequence[str]) -> set[str]:
        """Answer with the intersection, exactly as the repository query does."""
        self.asked.append(list(candidates))
        return self.held.intersection(candidates)


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def http() -> Iterator[respx.MockRouter]:
    """Every outbound request, intercepted before it leaves the process."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SourceClient]:
    """The shared client, with the dev disk cache off and the waiting removed.

    ``http_cache_dir`` is cleared before construction because ``SourceClient``
    reads it once, in ``__init__``: a developer with a populated cache would
    otherwise have these tests served from disk and never notice that respx was
    not called at all.
    """
    monkeypatch.setattr(settings, "http_cache_dir", None)
    source_client = SourceClient(sleep=_instant)
    try:
        yield source_client
    finally:
        await source_client.aclose()


@pytest.fixture
def jsearch(client: SourceClient, monkeypatch: pytest.MonkeyPatch) -> JSearchSource:
    """A configured JSearch connector bound to the mocked client."""
    monkeypatch.setattr(settings, "source_credentials", {"jsearch.rapidapi_key": RAPIDAPI_KEY})
    source = JSearchSource()
    return source.bind(client.bind(source))


@pytest.fixture
def arbeitnow(client: SourceClient, http: respx.MockRouter) -> ArbeitnowSource:
    """An arbeitnow connector, with the robots.txt its CRAWL mode insists on."""
    http.get("https://www.arbeitnow.com/robots.txt").mock(
        return_value=httpx.Response(200, text=ROBOTS_ALLOW_ALL)
    )
    source = ArbeitnowSource()
    return source.bind(client.bind(source))


@pytest.fixture
def remotive(client: SourceClient) -> RemotiveSource:
    """A remotive connector bound to the mocked client."""
    source = RemotiveSource()
    return source.bind(client.bind(source))


# ── jsearch: the request we are allowed to send ───────────────────────


async def test_every_jsearch_request_pins_date_posted_to_all(
    jsearch: JSearchSource, http: respx.MockRouter
) -> None:
    """Any other value for date_posted empties the result set.

    Measured, not guessed: ``date_posted=week`` returned nothing where
    ``date_posted=all`` returned ten, because seven of those ten carry a null
    ``job_posted_at`` and the upstream filter drops every posting whose date it
    does not know. The obvious "improvement" — deriving the parameter from
    ``SearchQuery.posted_within_days`` — therefore deletes 70% of the feed while
    looking like a freshness optimisation, which is why the query below asks for
    a three-day window and must still go out asking for everything.
    """
    route = http.get(JSEARCH_URL).mock(
        side_effect=responses(payload("jsearch_page1"), payload("jsearch_page2"))
    )

    await collect(jsearch, SearchQuery(keywords=("Python",), posted_within_days=3))

    assert route.call_count == 2
    assert [params["date_posted"] for params in query_params(route)] == [DATE_POSTED] * 2
    assert DATE_POSTED == "all"


async def test_jsearch_never_sends_work_from_home(
    jsearch: JSearchSource, http: respx.MockRouter
) -> None:
    """The parameter costs a credit and buys nothing, so it must not reappear.

    With "remote" already in the query text it returned the same ten postings in
    a different order. Adding it back would spend one of a hundred daily
    requests to re-sort a page we already had, and a remote-only search is
    exactly the query that tempts someone to add it.
    """
    route = http.get(JSEARCH_URL).mock(
        side_effect=responses(payload("jsearch_unstable_job_id")["run_1"])
    )

    await collect(
        jsearch, SearchQuery(keywords=("Python", "remote"), remote=RemoteType.FULL, limit=1)
    )

    assert route.call_count == 1
    assert "work_from_home" not in query_params(route)[0]


@pytest.mark.parametrize(
    ("country", "language"),
    [("kz", "ru"), ("ru", "ru"), ("us", "en"), ("de", "en")],
)
async def test_jsearch_asks_a_russian_market_in_russian(
    jsearch: JSearchSource, http: respx.MockRouter, country: str, language: str
) -> None:
    """``country=kz`` with no language returned zero postings; with ``ru`` it returned ten.

    The language parameter is not a nicety here, it is the difference between a
    working connector and an empty Kazakh market that looks like there are no
    jobs. Defaulting the rest of the world to English is the other half: sending
    ``ru`` to a US search would be the same mistake pointing the other way.
    """
    route = http.get(JSEARCH_URL).mock(
        side_effect=responses(payload("jsearch_unstable_job_id")["run_1"])
    )

    await collect(jsearch, SearchQuery(keywords=("Python",), country=country))

    sent = query_params(route)[0]
    assert sent["country"] == country
    assert sent["language"] == language


# ── jsearch: which identifier ends up in the database ─────────────────


async def test_jsearch_identifies_a_posting_by_uid_so_two_runs_upsert_once(
    jsearch: JSearchSource, http: respx.MockRouter
) -> None:
    """``job_id`` changes between runs; using it means every run re-inserts the feed.

    The same CPI Card Group posting came back under two different ``job_id``
    values because the request context is encoded into it, while ``job_uid`` was
    byte-identical both times. An external id that moves makes the upsert on
    ``(source, external_id)`` never match, so the vacancy table grows a new row
    per run for a posting that has not changed — and the duplicates reach the
    dashboard, where the user sees the same job four times.
    """
    both = payload("jsearch_unstable_job_id")
    route = http.get(JSEARCH_URL).mock(side_effect=responses(both["run_1"], both["run_2"]))

    first = await collect(jsearch, SearchQuery(keywords=("Software Engineer",)))
    second = await collect(jsearch, SearchQuery(keywords=("Software Engineer",)))

    assert route.call_count == 2
    assert [posting.external_id for posting in first] == ["Wt7L6sJVi5nHIBUMAAAAAA=="]
    assert first[0].external_id == second[0].external_id
    assert first[0].key == second[0].key
    # The identifiers the upstream actually differed on, so the test proves the
    # connector chose rather than that the two runs were identical.
    assert first[0].raw["job_id"] != second[0].raw["job_id"]


async def test_the_four_hundred_character_job_id_never_reaches_a_bounded_column(
    jsearch: JSearchSource, http: respx.MockRouter
) -> None:
    """``job_id`` overflows ``vacancy_source.external_id`` and takes the whole batch with it.

    ``RawPosting`` mirrors the column widths precisely so an over-long value
    fails at the connector boundary with the field named. ``job_id`` is roughly
    400 characters, twice the 200-character limit — writing it would not lose one
    posting, it would abort ``bulk_upsert`` and lose the page. It survives only
    inside ``raw``, which is JSONB and has no width to overflow.
    """
    both = payload("jsearch_unstable_job_id")
    http.get(JSEARCH_URL).mock(side_effect=responses(both["run_1"]))
    job_id: str = both["run_1"]["data"]["jobs"][0]["job_id"]

    posting = (await collect(jsearch, SearchQuery(keywords=("Software Engineer",))))[0]

    assert len(job_id) > 200
    bounded = (posting.external_id, posting.url, posting.title, posting.company or "")
    assert not any(job_id in value for value in bounded)
    assert len(posting.external_id) <= 200
    assert posting.raw["job_id"] == job_id


# ── jsearch: pagination and the early exit ────────────────────────────


async def test_jsearch_walks_the_cursor_and_stops_when_it_runs_out(
    jsearch: JSearchSource, http: respx.MockRouter
) -> None:
    """There is no ``page`` parameter, so a lost cursor is a connector stuck on page one.

    An empty cursor asks for the first page and a null one means there are no
    more. Getting the first half wrong sends ``cursor=None`` upstream and gets a
    400; getting the second half wrong turns a finished feed into a loop that
    spends credits until ``MAX_PAGES`` catches it.
    """
    route = http.get(JSEARCH_URL).mock(
        side_effect=responses(payload("jsearch_page1"), payload("jsearch_page2"))
    )

    postings = await collect(jsearch, SearchQuery(keywords=("Python",)))

    assert route.call_count == 2
    assert [params.get("cursor") for params in query_params(route)] == [None, "PAGE2CURSOR"]
    assert {posting.external_id for posting in postings} == {"aaa1", "bbb2", "ccc3", "ddd4"}


async def test_jsearch_stops_paying_for_a_page_it_mostly_already_holds(
    jsearch: JSearchSource, http: respx.MockRouter
) -> None:
    """Without a usable date filter the feed mixes old postings into every page.

    Each page costs one of a hundred daily credits, and ``date_posted`` is
    pinned to ``all``, so nothing upstream keeps yesterday's postings out of
    today's page two. Recognising them mid-pagination is the only brake there
    is, and the brake trips at ``KNOWN_SHARE_STOP``: every posting on page two
    is one we already hold, so a third credit would buy nothing.

    The share is what is being tested, not the count. The pages here hold three
    postings, so two of them is 67% and does NOT trip a 70% threshold — that
    case is the sibling test below, which asserts the walk continues. Asserting
    a stop at 67% would be asserting a different constant than the one the
    connector documents.

    The third page is a trap: it is registered so a connector that ignores the
    hook has something to fetch, and fetching it at all is the failure.
    """
    page_two = payload("jsearch_page2")
    page_two["data"]["cursor"] = "PAGE3CURSOR"
    page_three = payload("jsearch_page2")
    page_three["data"]["cursor"] = None
    route = http.get(JSEARCH_URL).mock(
        side_effect=responses(payload("jsearch_page1"), page_two, page_three)
    )
    held = KnownIdsSpy("aaa1", "bbb2", "ddd4")
    jsearch.with_known_ids(held)

    await collect(jsearch, SearchQuery(keywords=("Python",)))

    assert held.asked, "the connector never consulted the known-ids hook"
    assert "PAGE3CURSOR" not in [params.get("cursor") for params in query_params(route)]
    assert route.call_count == 2


async def test_jsearch_keeps_paginating_below_the_known_share_threshold(
    jsearch: JSearchSource, http: respx.MockRouter
) -> None:
    """The brake is a share, and a share below the threshold is not a brake.

    Two of three is 67%, under the connector's 70%. Stopping there would be a
    different rule from the one the module documents, and on a real ten-item
    page it would cut the walk off after seven familiar postings — which is an
    ordinary page, not a spent feed.
    """
    page_two = payload("jsearch_page2")
    page_two["data"]["cursor"] = "PAGE3CURSOR"
    page_three = payload("jsearch_page2")
    page_three["data"]["jobs"] = [
        job for job in page_three["data"]["jobs"] if job["job_uid"] == "ddd4"
    ]
    page_three["data"]["cursor"] = None
    route = http.get(JSEARCH_URL).mock(
        side_effect=responses(payload("jsearch_page1"), page_two, page_three)
    )
    jsearch.with_known_ids(KnownIdsSpy("aaa1", "bbb2"))

    await collect(jsearch, SearchQuery(keywords=("Python",)))

    assert route.call_count == 3


async def test_jsearch_keeps_paginating_while_the_pages_are_all_new(
    jsearch: JSearchSource, http: respx.MockRouter
) -> None:
    """The other half of the brake: a hook holding nothing must never stop the walk.

    An early exit that fired on an empty database would cap every source at one
    page on the very first run — the run where there is most to fetch and
    nothing yet to recognise. The failure would look like a small feed rather
    than like a bug.
    """
    page_two = payload("jsearch_page2")
    page_two["data"]["cursor"] = "PAGE3CURSOR"
    page_three = payload("jsearch_page2")
    page_three["data"]["jobs"] = [
        job for job in page_three["data"]["jobs"] if job["job_uid"] == "ddd4"
    ]
    page_three["data"]["cursor"] = None
    route = http.get(JSEARCH_URL).mock(
        side_effect=responses(payload("jsearch_page1"), page_two, page_three)
    )
    held = KnownIdsSpy()
    jsearch.with_known_ids(held)

    await collect(jsearch, SearchQuery(keywords=("Python",)))

    assert route.call_count == 3
    assert [params.get("cursor") for params in query_params(route)] == [
        None,
        "PAGE2CURSOR",
        "PAGE3CURSOR",
    ]
    assert held.asked == [["aaa1", "bbb2", "ccc3"], ["aaa1", "bbb2", "ddd4"], ["ddd4"]]


# ── jsearch: the payload's own lies ───────────────────────────────────


async def test_a_title_saying_remote_outvotes_a_flag_saying_otherwise(
    jsearch: JSearchSource, http: respx.MockRouter
) -> None:
    """One live posting titled "... (Remote)" arrived with ``job_is_remote=false``.

    Remoteness is the single filter a candidate in Almaty applies hardest, so a
    trusted flag quietly hides the remote jobs — the ones this whole source
    exists to find — behind a boolean the publisher never filled in. The flag is
    kept as the weakest witness: it may add remoteness, never remove it.
    """
    http.get(JSEARCH_URL).mock(side_effect=responses(payload("jsearch_page1")))

    postings = {
        posting.external_id: posting
        for posting in await collect(jsearch, SearchQuery(keywords=("Python",), limit=3))
    }

    mislabelled = postings["aaa1"]
    assert mislabelled.raw["job_is_remote"] is False
    assert mislabelled.raw["_derived"]["remote"] == RemoteType.FULL.value
    # Stated on the function too, with a location that cannot be doing the work:
    # the fixture's "Anywhere" would make the title's contribution invisible.
    assert looks_remote("Backend Engineer (Remote)", "Алматы, Казахстан", False) is RemoteType.FULL
    assert looks_remote("Backend Engineer", "Алматы, Казахстан", False) is RemoteType.NO
    assert looks_remote("Backend Engineer", "Алматы, Казахстан", True) is RemoteType.FULL


async def test_the_publisher_tail_is_cut_off_the_location(
    jsearch: JSearchSource, http: respx.MockRouter
) -> None:
    """The location arrives as a place and a publisher glued together by a bullet.

    Everything from the bullet on names the board the posting was scraped from,
    not where the work is. Left in, the geocoder is handed a string no city
    matches, every Almaty posting fails to resolve, and the area filter — the
    other half of what a candidate searches on — silently returns nothing.
    """
    http.get(JSEARCH_URL).mock(side_effect=responses(payload("jsearch_page1")))

    postings = {
        posting.external_id: posting
        for posting in await collect(jsearch, SearchQuery(keywords=("Python",), limit=3))
    }

    assert postings["bbb2"].raw["job_location"] == "Алматы, Казахстан • via HeadHunter"
    assert postings["bbb2"].raw["_derived"]["location"] == "Алматы, Казахстан"
    assert clean_location("Almaty, Kazakhstan • via LinkedIn") == "Almaty, Kazakhstan"
    assert clean_location("• via LinkedIn") is None
    assert clean_location(None) is None


async def test_a_posting_with_no_apply_link_is_dropped_not_raised_on(
    jsearch: JSearchSource, http: respx.MockRouter
) -> None:
    """``job_apply_link`` is optional upstream and NOT NULL in our schema.

    Both shapes occur — the key absent and the key explicitly null — and either
    one reaching ``RawPosting`` raises, which in an async generator kills the
    walk partway through and loses every posting after it as well. A link the
    user cannot click is not a vacancy, so the row is skipped and its
    page-mates still arrive.
    """
    page = payload("jsearch_page1")
    page["data"]["cursor"] = None
    del page["data"]["jobs"][0]["job_apply_link"]
    page["data"]["jobs"][2]["job_apply_link"] = None
    http.get(JSEARCH_URL).mock(side_effect=responses(page))

    postings = await collect(jsearch, SearchQuery(keywords=("Python",)))

    assert [posting.external_id for posting in postings] == ["bbb2"]
    assert postings[0].url == "https://example.com/apply/bbb2"


# ── arbeitnow ─────────────────────────────────────────────────────────


async def test_arbeitnow_yields_each_slug_once_across_overlapping_pages(
    arbeitnow: ArbeitnowSource, http: respx.MockRouter
) -> None:
    """The live feed served two of the same postings on consecutive pages.

    It refreshes hourly and paginates by offset, so a posting sitting near a
    page boundary while the window shifts is returned twice — twelve entries
    across these two captured pages are ten distinct jobs.
    ``search_batch`` deduplicates *between* queries and cannot help here,
    because the repeat happens inside a single ``search`` call. Without the
    per-slug set the pipeline pays to normalise, embed and score the same
    posting twice, and it reaches the dashboard as two identical cards.
    """
    route = http.get(ARBEITNOW_URL).mock(
        side_effect=responses(
            payload("arbeitnow_page2"), payload("arbeitnow_page3"), empty_arbeitnow_page()
        )
    )

    postings = await collect(arbeitnow)

    identifiers = [posting.external_id for posting in postings]
    assert len(identifiers) == 10
    assert len(set(identifiers)) == 10
    assert route.call_count == 3
    for slug in REPEATED_SLUGS:
        assert identifiers.count(slug) == 1


async def test_arbeitnow_stops_when_a_page_adds_nothing_new(
    arbeitnow: ArbeitnowSource, http: respx.MockRouter
) -> None:
    """A page of entries we have all seen is the feed standing still, not progress.

    ``?page=N`` walks an hourly-refreshed window, so there is no "last page" flag
    to trust; a connector without this terminator keeps asking until MAX_PAGES,
    spending thirty requests on a host that asks politely not to be abused and
    getting nothing for twenty-nine of them.
    """
    route = http.get(ARBEITNOW_URL).mock(
        side_effect=responses(payload("arbeitnow_page2"), payload("arbeitnow_page2"))
    )

    postings = await collect(arbeitnow)

    assert len(postings) == 6
    assert route.call_count == 2


async def test_arbeitnow_honours_the_query_limit_mid_page(
    arbeitnow: ArbeitnowSource, http: respx.MockRouter
) -> None:
    """The ceiling exists so one broad query cannot consume a whole run's budget.

    A limit enforced only between pages would overshoot by up to a full page —
    175 postings on the live feed — and the planner crosses skill groups with
    placements, so "one query overshoots a little" multiplies by the number of
    queries in the run.
    """
    route = http.get(ARBEITNOW_URL).mock(side_effect=responses(payload("arbeitnow_page2")))

    postings = await collect(arbeitnow, SearchQuery(limit=4))

    assert len(postings) == 4
    assert route.call_count == 1


async def test_arbeitnow_filters_keywords_here_because_the_endpoint_takes_none(
    arbeitnow: ArbeitnowSource, http: respx.MockRouter
) -> None:
    """``job-board-api`` accepts no query, no location and no tag parameter.

    Sending one is not an error upstream — it is ignored — so a connector that
    "adds search support" would look correct, return the unfiltered feed, and
    hand the matcher every German-language posting on the board. The request may
    therefore carry nothing but ``page``, and the narrowing has to happen on
    this side against the title, the tags and the description.
    """
    route = http.get(ARBEITNOW_URL).mock(
        side_effect=responses(payload("arbeitnow_page2"), empty_arbeitnow_page())
    )

    postings = await collect(arbeitnow, SearchQuery(keywords=("Robotics",)))

    assert query_params(route) == [{"page": "1"}, {"page": "2"}]
    assert [posting.external_id for posting in postings] == [ROBOTICS_SLUG]


# ── remotive ──────────────────────────────────────────────────────────


async def test_remotive_is_asked_for_the_feed_with_no_parameters_at_all(
    remotive: RemotiveSource, http: respx.MockRouter
) -> None:
    """``category=software-dev`` and ``category=design`` returned byte-identical id sets.

    The endpoint documents ``search``, ``category`` and ``limit`` and honours
    none of them: it answers with the same fixed feed spanning nine categories
    whatever it is sent. Sending them anyway would not be harmless — it would
    make the response look filtered, and the local filter below would then be
    read as redundant and deleted by the next person through here.
    """
    route = http.get(REMOTIVE_URL).mock(return_value=httpx.Response(200, json=payload("remotive")))

    postings = await collect(remotive, SearchQuery(keywords=("Golang",), limit=5))

    assert route.call_count == 1
    assert route.calls[0].request.url.query == b""
    assert str(route.calls[0].request.url) == REMOTIVE_URL
    assert postings, "the feed came back but nothing survived the local filter"


async def test_remotive_publication_dates_come_out_timezone_aware(
    remotive: RemotiveSource, http: respx.MockRouter
) -> None:
    """The payload's timestamp is naive and every column it feeds is aware.

    ``"2026-09-02T19:59:53"`` carries no offset. Passed on as-is it either
    raises on write or is read as local time — a silent five-hour shift in
    "posted at" for a machine in Almaty, in the one field freshness filtering
    and sorting are computed from. Remotive publishes UTC, so the assumption is
    stated once, here, under its own key: normalisation reads
    ``publication_date_utc`` and never the source's own field, which stays
    untouched in ``raw`` beside it.
    """
    http.get(REMOTIVE_URL).mock(return_value=httpx.Response(200, json=payload("remotive")))

    postings = {posting.external_id: posting for posting in await collect(remotive)}

    copywriter = postings[COPYWRITER_ID]
    stored = copywriter.raw[PUBLISHED_AT_KEY]
    assert stored == "2026-09-02T19:59:53+00:00"
    corrected = datetime.fromisoformat(stored)
    assert corrected.utcoffset() == timedelta(0)
    assert corrected == datetime(2026, 9, 2, 19, 59, 53, tzinfo=UTC)
    # The vendor's own field is preserved exactly as it arrived, naive and all,
    # so a later re-normalisation can still see what was actually sent.
    assert copywriter.raw["publication_date"] == "2026-09-02T19:59:53"
    assert datetime.fromisoformat(copywriter.raw["publication_date"]).tzinfo is None


async def test_remotive_leaves_the_free_text_salary_unparsed(
    remotive: RemotiveSource, http: respx.MockRouter
) -> None:
    """One salary field, four grammars: $20k -$35k, $120 - $170 /hour, Pay per task, empty.

    Guessing a currency, a period and a range out of that inside a connector
    hides the guess: the number reaches the salary sort looking like a measured
    figure, and "$20k -$35k" read as 20 USD/month would bury a real posting at
    the bottom of every ranking with nothing to explain why. Parsing is
    normalisation's job, done once for every source, against text this connector
    is required to hand over intact.
    """
    http.get(REMOTIVE_URL).mock(return_value=httpx.Response(200, json=payload("remotive")))
    original = {str(job["id"]): job for job in payload("remotive")["jobs"]}

    postings = {posting.external_id: posting for posting in await collect(remotive)}

    copywriter = postings[COPYWRITER_ID]
    assert copywriter.raw["salary"] == "$20k -$35k"
    # The only key this connector is allowed to add is the corrected timestamp;
    # anything else here would be a derived number masquerading as source data.
    assert set(copywriter.raw) - set(original[COPYWRITER_ID]) == {PUBLISHED_AT_KEY}
    # Display strings are still tidied: remotive's company names carry trailing
    # spaces often enough that this is the normal case, not defensive coding.
    assert copywriter.company == "Coalition Technologies"


async def test_remotive_narrows_the_fixed_feed_on_our_side(
    remotive: RemotiveSource, http: respx.MockRouter
) -> None:
    """One request returns all eighteen postings whatever was asked for.

    So the keywords have to be applied here, over the title, category, tags and
    the de-tagged description. A connector that skipped this step would look
    like it worked — postings come back — while handing the matcher a feed with
    copywriting and sales jobs in it for a backend engineer's profile, and the
    cost of that lands on the LLM re-rank, per posting, per run.
    """
    http.get(REMOTIVE_URL).mock(return_value=httpx.Response(200, json=payload("remotive")))

    everything = await collect(remotive)
    narrowed = await collect(remotive, SearchQuery(keywords=("copywriter",)))

    assert len(everything) == 18
    assert [posting.external_id for posting in narrowed] == [COPYWRITER_ID]
    # Matching is case-insensitive substring over the whole posting, so the
    # title's own capitalisation is not what made the match.
    assert narrowed[0].title == "Freelance Copywriter"


def test_remotive_carries_the_credit_line_its_terms_require() -> None:
    """Attribution is a condition of access here, not a courtesy.

    Remotive grants API access on the stated condition that the postings link
    back and name Remotive as the source, and says plainly that access is
    terminated otherwise. The dashboard renders whatever ``attribution`` holds,
    so this class attribute going empty does not break a test anywhere else — it
    just quietly puts the project in breach until the feed stops answering.
    """
    assert RemotiveSource.attribution is not None
    assert "Remotive" in RemotiveSource.attribution
    assert RemotiveSource().attribution == RemotiveSource.attribution
    assert RemotiveSource.terms_url == "https://remotive.com/api-documentation"
