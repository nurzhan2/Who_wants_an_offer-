"""The hh connector, driven against pages hh really served on 2026-09-06.

Every payload under ``fixtures/sources/hh_*`` is a live capture, trimmed only of
CDN paths, map coordinates and paid branding; the one derived file says so in a
``_comment``. That matters more here than for the feed connectors, because this
source parses a third party's internal frontend state rather than a documented
response, and a fixture somebody invented would pin what we imagined instead of
what hh sends.

What this file exists to protect is a short list of rules that each cost a live
request to discover and none of which is self-evident from the code:

* ``{"noCompensation": {}}`` is a non-empty dict, so the obvious salary check
  reports a salary that is not there;
* ``keySkills`` is a wrapper on one page and ``null`` on the next, and so is
  every other collection, which is why there is one ``unwrap`` and not a rule
  per field;
* a vacancy that has been taken down answers 404 **with the marker still
  present** and an empty view, so "the marker is there" is not the same as "the
  page parsed" — the canary asserts both;
* the walk must survive that 404, because a sitemap is a snapshot and the site
  is not;
* the position is per sitemap file and advances only over entries actually
  dealt with, or a truncated run silently skips whatever it did not reach;
* and it must advance AT THE VALUES THIS CONNECTOR SHIPS. Every watermark test
  here once ran with ``WATERMARK_LAG`` monkeypatched to 0, 1, 2 or 3, so the
  mechanism was thoroughly proved with its safety margin turned off — the one
  configuration that never ships. At the shipped 200 a run had to store 201
  postings before the mark moved at all; the live run of 2026-09-06 stored 172,
  was stopped by a captcha, and recorded nothing, as had every run before it.
  ``source_state`` was empty, not stale. The tests that now carry that weight
  patch no constant they are testing, and they say so in their own docstrings;
* no URL this connector builds may carry a query string, because that is the one
  thing hh's robots.txt forbids.

A connector that loses any one of them still compiles, still runs, and still
looks like it works — which is exactly how a dashboard ends up quietly showing
no hh vacancies for a week.

Nothing here reaches the network. ``respx`` answers every request and an
unmocked URL fails the test. The live check lives in ``test_hh_canary.py``
behind the ``network`` marker.
"""

import html
import json
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from app.core.config import settings
from app.core.exceptions import SourceError
from app.db.enums import RemoteType, SalaryPeriod
from app.pipeline.runner import UPSERT_BATCH
from app.sources.base import RawPosting, SearchQuery
from app.sources.hh import (
    GONE_STATUSES,
    HEAD_SLICE,
    MAX_EXTERNAL_ID,
    MAX_MARKUP_FAILURES,
    MAX_TIED_IDS,
    WATERMARK_LAG,
    WATERMARK_SAVE_EVERY,
    FileWatermark,
    HHMarkupError,
    HHSite,
    HHSource,
    SitemapEntry,
    _entry,
    _remote_from,
    _salary,
    load_sites,
    strip_html,
    unwrap,
)
from app.sources.http import HHChallengedError, ResponseCache, SourceClient

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "sources"

HOST = "almaty.hh.kz"
INDEX_URL = f"https://{HOST}/sitemap/main.xml"
VACANCY0_URL = f"https://{HOST}/sitemap/vacancy0.xml"
VACANCY1_URL = f"https://{HOST}/sitemap/vacancy1.xml"
ROBOTS_URL = f"https://{HOST}/robots.txt"

#: The wildcard group of the live file, in full. The three Allow lines are the
#: exceptions a search URL does not match, and the Disallow is the rule
#: robotparser cannot apply — see test_sources_http.py.
HH_ROBOTS = (
    "User-agent: *\n"
    "Allow: *?u*\n"
    "Allow: *?currencyCode*\n"
    "Allow: *?vacancyId*\n"
    "Disallow: *?*\n"
    "Disallow: /resume$\n"
)

#: The captured pages, by the id hh gave them.
FULL = "136773120"
NULL_COLLECTIONS = "136583540"
NO_COMPENSATION = "137006870"
SALARY_TO_ONLY = "136079610"
SALARY_FROM_ONLY = "136401000"
SALARY_NO_FREQUENCY = "136555460"
WRAPPED_COLLECTION = "136390570"
EMPTY_DESCRIPTION = "136721860"

CASES: dict[str, str] = {
    FULL: "hh_vacancy_full",
    NULL_COLLECTIONS: "hh_vacancy_null_collections",
    NO_COMPENSATION: "hh_vacancy_no_compensation",
    SALARY_TO_ONLY: "hh_vacancy_salary_to_only",
    SALARY_FROM_ONLY: "hh_vacancy_salary_from_only",
    SALARY_NO_FREQUENCY: "hh_vacancy_salary_no_frequency",
    WRAPPED_COLLECTION: "hh_vacancy_wrapped_collection",
    EMPTY_DESCRIPTION: "hh_vacancy_empty_description",
}

WHEN = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)


# -- helpers -----------------------------------------------------------


def state(vacancy_id: str) -> dict[str, Any]:
    """One captured page state, re-read per call so no test can mutate another's."""
    with (FIXTURES / f"{CASES[vacancy_id]}.json").open(encoding="utf-8") as handle:
        payload: dict[str, Any] = json.load(handle)
    return payload


def page(payload: dict[str, Any]) -> str:
    """A vacancy page carrying that state, escaped the way hh escapes it."""
    return (
        "<!doctype html><html><head><title>hh</title></head><body><div id=HH-React-Root>"
        '</div><template style="display:none" id="HH-Lux-InitialState">'
        + html.escape(json.dumps(payload, ensure_ascii=False))
        + "</template></body></html>"
    )


def vacancy_url(vacancy_id: str) -> str:
    """Where the connector will look for that posting."""
    return f"https://{HOST}/vacancy/{vacancy_id}"


def sitemap(entries: Sequence[tuple[str, datetime]]) -> str:
    """A vacancy sitemap listing those ids with those timestamps."""
    body = "".join(
        f"<url><loc>{vacancy_url(vacancy_id)}</loc><lastmod>{when.isoformat()}</lastmod></url>"
        for vacancy_id, when in entries
    )
    return (
        "<?xml version='1.0' encoding='utf-8'?>"
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + body + "</urlset>"
    )


def dated(ids: Sequence[str], *, start: datetime = WHEN) -> list[tuple[str, datetime]]:
    """Those ids, one minute apart, oldest first."""
    return [(vacancy_id, start + timedelta(minutes=index)) for index, vacancy_id in enumerate(ids)]


class StateStore:
    """The pipeline's crawl-position store, without a database.

    Records reads as well as writes: a connector that stopped consulting its
    position would otherwise pass the incremental test for the wrong reason.
    """

    def __init__(self) -> None:
        self.saved: dict[str, dict[str, Any]] = {}
        self.reads: list[str] = []

    async def load(self, key: str) -> dict[str, Any] | None:
        """Answer with what was stored, exactly as the repository does."""
        self.reads.append(key)
        return self.saved.get(key)

    async def save(self, key: str, value: dict[str, Any]) -> None:
        """Store it, replacing whatever was there."""
        self.saved[key] = value


async def _instant(_seconds: float) -> None:
    """Stand in for ``asyncio.sleep``, so a token bucket costs no wall clock."""
    return None


async def collect(source: HHSource, query: SearchQuery | None = None) -> list[RawPosting]:
    """Everything one walk yields, drained the way the pipeline drains it."""
    return [posting async for posting in source.search_batch([query or SearchQuery()])]


def derived(posting: RawPosting) -> dict[str, Any]:
    """The block the connector worked out for that posting."""
    block: dict[str, Any] = posting.raw["_derived"]
    return block


# -- fixtures ----------------------------------------------------------


#: The shipped head slice, bound at import and therefore before the fixture
#: below patches the module attribute. A test that wants the real value has to
#: put it back, and this is where it is kept so that putting it back cannot
#: quietly become "put 50 back" after somebody edits the connector.
SHIPPED_HEAD_SLICE = HEAD_SLICE


@pytest.fixture(autouse=True)
def no_head_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the newest-first head pass off for every test that is not about it.

    With it on, a ten-entry fixture is entirely head — the slice is fifty — and
    every assertion about walk order would become an assertion about the head
    pass instead. The tests that own that behaviour set the slice themselves.

    Autouse and blanket, which makes this the second constant in the module that
    nothing ever exercised as it ships. See
    ``test_the_shipped_head_slice_walks_a_small_file_once_and_records_it``.
    """
    monkeypatch.setattr("app.sources.hh.HEAD_SLICE", 0)


@pytest.fixture
def http() -> Iterator[respx.MockRouter]:
    """Every outbound request, intercepted before it leaves the process."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SourceClient]:
    """The shared client with the dev disk cache off and the waiting removed.

    ``http_cache_dir`` is cleared before construction because ``SourceClient``
    reads it once: a developer with a populated cache would otherwise have these
    tests served from disk and never notice respx was not called.
    """
    monkeypatch.setattr(settings, "http_cache_dir", None)
    source_client = SourceClient(sleep=_instant)
    try:
        yield source_client
    finally:
        await source_client.aclose()


@pytest.fixture
def store() -> StateStore:
    """Where this run's crawl position goes."""
    return StateStore()


@pytest.fixture
def hh(client: SourceClient, http: respx.MockRouter, store: StateStore) -> HHSource:
    """A connector bound to the mocked client, with robots.txt and the index served.

    hh is a CRAWL source, so the shared client asks for robots.txt before the
    first request on the host; the file served here is the live wildcard group.
    """
    http.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=HH_ROBOTS))
    http.get(INDEX_URL).mock(
        return_value=httpx.Response(
            200, text=(FIXTURES / "hh_sitemap_index.xml").read_text(encoding="utf-8")
        )
    )
    http.get(VACANCY1_URL).mock(return_value=httpx.Response(200, text=sitemap([])))
    source = HHSource()
    return source.bind(client.bind(source)).with_state(store.load, store.save)


def serve(http: respx.MockRouter, ids: Sequence[str]) -> dict[str, respx.Route]:
    """Serve a sitemap listing those ids, and each of their pages."""
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(dated(ids))))
    return {
        vacancy_id: http.get(vacancy_url(vacancy_id)).mock(
            return_value=httpx.Response(200, text=page(state(vacancy_id)))
        )
        for vacancy_id in ids
    }


def minted_ids(count: int, *, start: int = 900_000_000) -> list[str]:
    """That many vacancy ids, nine digits like hh's own.

    Deliberately far above every captured id, so a route minted here can never
    collide with one a test served from a fixture.
    """
    return [str(start + index) for index in range(count)]


def serve_many(http: respx.MockRouter, ids: Sequence[str]) -> dict[str, respx.Route]:
    """A sitemap of those ids, each page answering under its own id.

    One captured page — 136721860 — with ``vacancyId`` rewritten per id, because
    a page that answers for a different vacancy is refused by the walk and the
    runs this exists for need more pages than were ever captured. Nothing else
    on the payload is invented: the point of these runs is the arithmetic of the
    position, and every field they read is hh's.

    Eight fixtures could not do it. Clearing the shipped ``WATERMARK_LAG``
    demands more than a hundred postings in one run, which is the whole reason
    no test had ever done it.
    """
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(dated(ids))))
    payload = state(EMPTY_DESCRIPTION)
    routes: dict[str, respx.Route] = {}
    for vacancy_id in ids:
        payload["vacancyView"]["vacancyId"] = int(vacancy_id)
        routes[vacancy_id] = http.get(vacancy_url(vacancy_id)).mock(
            return_value=httpx.Response(200, text=page(payload))
        )
    return routes


# -- the walk ----------------------------------------------------------


async def test_a_walk_yields_a_posting_per_page(hh: HHSource, http: respx.MockRouter) -> None:
    """The whole path, end to end: index, sitemap, pages, postings."""
    serve(http, [FULL, NULL_COLLECTIONS])

    postings = await collect(hh)

    assert [posting.external_id for posting in postings] == [FULL, NULL_COLLECTIONS]
    assert {posting.source_slug for posting in postings} == {"hh"}
    assert postings[0].title == "Служба Заботы"
    assert postings[0].company == "Inspire International"
    assert postings[0].url == vacancy_url(FULL)


async def test_only_vacancy_sitemaps_are_ever_requested(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The rule with a person on the other end of it.

    The index lists ``resumes0.xml`` alongside the vacancy files, and those are
    living people's resumes. The selection is an allow-list on the whole file
    name, so the guarantee is a property of the code rather than of the care
    taken by whoever edits it next. ``employers`` and ``vacancies`` (SEO landing
    pages) are out of scope for this phase and must not be fetched either.
    """
    serve(http, [FULL])

    await collect(hh)

    asked = [str(call.request.url) for call in http.calls]
    # The index really does list a resumes file — without this the two
    # assertions below quantify over an opportunity the connector was never
    # given, and would pass on a fixture that had never mentioned resumes.
    assert "resumes0.xml" in _index_body()
    assert not [url for url in asked if "resumes" in url]
    assert not [url for url in asked if "/employers" in url or "/vacancies" in url]
    assert VACANCY0_URL in asked


async def test_no_request_this_connector_makes_carries_a_query_string(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The one thing hh's robots.txt forbids, asserted over every call made.

    The transport refuses a query string on this host, so a regression here
    surfaces as an exception rather than as a quiet violation — but asserting it
    over the whole walk is what proves no code path was tempted to add one.
    """
    serve(http, [FULL, NULL_COLLECTIONS])

    await collect(hh)

    # Not vacuous: a walk that issued nothing would satisfy the comparison
    # below, and this test is the one naming the rule hh's robots.txt states.
    assert len(http.calls) > 3
    assert [call.request.url.query for call in http.calls] == [b""] * len(http.calls)


async def test_a_vacancy_that_has_gone_does_not_stop_the_walk(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """A sitemap is a snapshot; the site is not.

    hh answers 404 for a posting taken down since the file was written, and on
    a corpus of fourteen thousand that happens on every run. The walk logs it
    and carries on with the next entry.
    """
    http.get(VACANCY0_URL).mock(
        return_value=httpx.Response(200, text=sitemap(dated([FULL, NO_COMPENSATION])))
    )
    http.get(vacancy_url(FULL)).mock(
        return_value=httpx.Response(404, text=page({"errorCode": 404}))
    )
    http.get(vacancy_url(NO_COMPENSATION)).mock(
        return_value=httpx.Response(200, text=page(state(NO_COMPENSATION)))
    )

    postings = await collect(hh)

    assert [posting.external_id for posting in postings] == [NO_COMPENSATION]


async def test_a_page_that_answers_200_with_an_empty_view_is_skipped_quietly(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The one silent case, and why it has to be silent.

    A removed posting keeps the marker and empties ``vacancyView``. Treating
    that as a markup change would make an ordinary Tuesday look like hh having
    renamed everything, so it is skipped — and the loud case is the marker being
    gone, which the next test covers.
    """
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(dated([FULL]))))
    http.get(vacancy_url(FULL)).mock(
        return_value=httpx.Response(200, text=page({"vacancyView": {}, "errorCode": 404}))
    )

    assert await collect(hh) == []


async def test_one_unreadable_page_is_tolerated_and_a_pattern_of_them_is_not(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """Loud, but only once it is a pattern.

    A single odd page — truncated response, posting mid-edit — must not wedge
    the crawl, because the walk resumes at the same entry every run and would
    never get past it. Three in one run is not an odd page: it is hh having
    moved the state we parse, and the whole point of this connector's error
    handling is to say so instead of returning nothing.
    """
    ids = [FULL, NULL_COLLECTIONS, NO_COMPENSATION, SALARY_TO_ONLY]
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(dated(ids))))
    http.get(vacancy_url(FULL)).mock(
        return_value=httpx.Response(200, text="<html>hh redesigned this page</html>")
    )
    http.get(vacancy_url(NULL_COLLECTIONS)).mock(
        return_value=httpx.Response(200, text=page(state(NULL_COLLECTIONS)))
    )
    for vacancy_id in (NO_COMPENSATION, SALARY_TO_ONLY):
        http.get(vacancy_url(vacancy_id)).mock(
            return_value=httpx.Response(200, text="<html>hh redesigned this page</html>")
        )

    with pytest.raises(HHMarkupError) as excinfo:
        await collect(hh)

    assert MAX_MARKUP_FAILURES == 3
    assert excinfo.value.extra["response_status"] == 200
    assert excinfo.value.extra["body_bytes"] > 0
    assert "HH-Lux-InitialState" in excinfo.value.detail


async def test_an_index_without_vacancy_sitemaps_is_a_markup_change(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The other end of the same silence: the map itself moving."""
    http.get(INDEX_URL).mock(
        return_value=httpx.Response(
            200,
            text=(
                "<?xml version='1.0'?><sitemapindex><sitemap>"
                f"<loc>https://{HOST}/sitemap/resumes0.xml</loc>"
                "</sitemap></sitemapindex>"
            ),
        )
    )

    with pytest.raises(HHMarkupError):
        await collect(hh)


async def test_an_archived_posting_is_not_stored(hh: HHSource, http: respx.MockRouter) -> None:
    """A job nobody can apply to must not sit in the dashboard beside ones they can."""
    archived = state(FULL)
    archived["vacancyView"]["status"] = {
        "active": False,
        "archived": True,
        "disabled": False,
        "needFix": False,
        "waiting": False,
    }
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(dated([FULL]))))
    http.get(vacancy_url(FULL)).mock(return_value=httpx.Response(200, text=page(archived)))

    assert await collect(hh) == []


# -- the position ------------------------------------------------------


async def test_the_second_run_fetches_only_what_changed(
    hh: HHSource, http: respx.MockRouter, store: StateStore
) -> None:
    """The whole reason a position is stored per sitemap file.

    A first run walks everything and writes down where it got to. A second run
    over the same file asks for nothing, and a file that has gained one entry
    costs exactly one page — which on a corpus of fourteen thousand is the
    difference between a delta and a full re-crawl every three hours.
    """
    routes = serve(http, [FULL, NULL_COLLECTIONS])
    assert len(await collect(hh)) == 2
    assert store.saved, "the position was never written"

    http.get(VACANCY0_URL).mock(
        return_value=httpx.Response(
            200, text=sitemap(dated([FULL, NULL_COLLECTIONS, NO_COMPENSATION]))
        )
    )
    fresh = http.get(vacancy_url(NO_COMPENSATION)).mock(
        return_value=httpx.Response(200, text=page(state(NO_COMPENSATION)))
    )

    second = await collect(hh)

    assert [posting.external_id for posting in second] == [NO_COMPENSATION]
    assert fresh.call_count == 1
    assert routes[FULL].call_count == 1, "an unchanged posting was fetched twice"


async def test_the_position_is_kept_per_sitemap_file(
    hh: HHSource, http: respx.MockRouter, store: StateStore
) -> None:
    """Per file, never global.

    ``lastmod`` values are grouped by the file that carries them, and one global
    mark would let a busy file's timestamps hide every entry of a quiet one.
    """
    serve(http, [FULL])

    await collect(hh)

    assert set(store.saved) == {f"sitemap:{HOST}:vacancy0"}
    # Both files are consulted, each under its own key. The sequence is not
    # asserted: a walk reads a mark once to work out what is due and again to
    # advance it, and pinning that would be pinning the loop rather than the
    # rule.
    assert set(store.reads) == {f"sitemap:{HOST}:vacancy0", f"sitemap:{HOST}:vacancy1"}


async def test_the_position_advances_over_a_page_that_yielded_nothing(
    hh: HHSource, http: respx.MockRouter, store: StateStore
) -> None:
    """Dealt with is not the same as stored.

    A posting that is gone, archived or filtered out has still cost a request,
    and leaving the mark behind it would make every future run buy that same
    page again forever.
    """
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(dated([FULL]))))
    gone = http.get(vacancy_url(FULL)).mock(return_value=httpx.Response(404, text=page({})))

    assert await collect(hh) == []
    assert gone.call_count == 1

    assert await collect(hh) == []
    assert gone.call_count == 1, "the missing page was bought a second time"


async def test_a_truncated_run_leaves_the_rest_for_the_next_one(
    hh: HHSource, http: respx.MockRouter, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The budget bounds a run without losing what it did not reach.

    Ascending order is what makes this work: the mark can only ever say
    "everything up to here is done", so a run that stops early leaves an
    unbroken remainder. Newest-first would strand the tail of a corpus this size
    permanently.
    """
    monkeypatch.setattr("app.sources.hh.MAX_PAGES_PER_RUN", 4)
    # The lag is what keeps the mark behind the pipeline's unwritten batch; at
    # this budget it would swallow the whole run, so it is stood down here and
    # pinned on its own below.
    monkeypatch.setattr("app.sources.hh.WATERMARK_LAG", 0)
    ids = [FULL, NULL_COLLECTIONS, NO_COMPENSATION, SALARY_TO_ONLY]
    routes = serve(http, ids)

    first = await collect(hh)

    # Four requests buy the index, both sitemap files, and one page. The
    # sitemaps are read up front rather than lazily because the newest-first
    # head slice is chosen across all of a site's files at once.
    assert [posting.external_id for posting in first] == [FULL]
    assert routes[NULL_COLLECTIONS].call_count == 0

    monkeypatch.setattr("app.sources.hh.MAX_PAGES_PER_RUN", 20)
    second = await collect(hh)

    assert [posting.external_id for posting in second] == [
        NULL_COLLECTIONS,
        NO_COMPENSATION,
        SALARY_TO_ONLY,
    ]
    assert routes[FULL].call_count == 1, "a posting already dealt with was bought again"


async def test_a_position_that_no_longer_parses_is_treated_as_none(
    hh: HHSource, http: respx.MockRouter, store: StateStore
) -> None:
    """One re-crawl of a file beats a source that cannot start until a row is deleted."""
    store.saved[f"sitemap:{HOST}:vacancy0"] = {"lastmod": "not a timestamp"}
    serve(http, [FULL])

    assert len(await collect(hh)) == 1


def test_a_watermark_never_moves_backwards_and_remembers_a_tie() -> None:
    """Ties at one second are why the mark carries ids as well as a timestamp.

    Without them, resuming has to choose between repeating that second's work on
    every run or skipping whatever tied with it — and skipping loses postings,
    which is the asymmetry the fingerprint module argues for elsewhere.
    """
    first = SitemapEntry(external_id="1", url=vacancy_url("1"), lastmod=WHEN)
    tied = SitemapEntry(external_id="2", url=vacancy_url("2"), lastmod=WHEN)
    later = SitemapEntry(external_id="3", url=vacancy_url("3"), lastmod=WHEN + timedelta(minutes=1))
    earlier = SitemapEntry(
        external_id="0", url=vacancy_url("0"), lastmod=WHEN - timedelta(minutes=1)
    )

    mark = FileWatermark().advanced(first)
    assert mark.is_done(first)
    assert not mark.is_done(tied), "a tie must not be mistaken for done"

    mark = mark.advanced(tied)
    assert mark.is_done(tied)
    assert mark.ids_at_lastmod == ("1", "2")

    mark = mark.advanced(later)
    assert mark.ids_at_lastmod == ("3",), "a new second replaces the tie list"
    assert mark.is_done(first) and mark.is_done(tied)

    assert mark.advanced(earlier) == mark, "the mark must never move backwards"


# -- what a posting carries --------------------------------------------


async def test_the_description_reaches_the_column_as_text_and_the_markup_survives(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """Two fields out of one, within the contract that exists today.

    ``RawPosting.description`` becomes ``vacancy.description_raw``, which is the
    column the embedding is computed from, so it carries the flattened text —
    HTML there would put tag names into every vector. The markup is kept in the
    derived block so the dashboard and a later HTML-to-markdown pass need no
    re-fetch.
    """
    serve(http, [FULL])

    posting = (await collect(hh))[0]

    assert posting.description is not None
    assert "<p>" not in posting.description
    assert "Мы Inspire" in posting.description
    assert derived(posting)["description_html"].startswith("<p><strong>")


async def test_an_empty_description_produces_no_description_at_all(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """So the pipeline records a stub rather than a full posting that scored badly."""
    serve(http, [EMPTY_DESCRIPTION])

    posting = (await collect(hh))[0]

    assert posting.description is None


async def test_the_employers_billing_block_is_never_stored(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """It is on the page whether anyone wants it there or not.

    ``vacancyProperties.properties`` is the employer's invoice — package names,
    service ids, paid-placement windows — and the captured page carries
    ``HH_AUTO_RENEWAL`` with ``intervalMinutes = 4320`` inside it. None of it
    describes the job. The derived flags beside it do, and those are kept.
    """
    payload = state(FULL)
    assert "HH_AUTO_RENEWAL" in json.dumps(payload, ensure_ascii=False)
    payload["vacancyView"]["vacancyProperties"]["calculatedStates"]["HH"].update(
        {"advertising": True, "anonymous": True}
    )
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(dated([FULL]))))
    http.get(vacancy_url(FULL)).mock(return_value=httpx.Response(200, text=page(payload)))

    posting = (await collect(hh))[0]

    stored = json.dumps(posting.raw, ensure_ascii=False)
    assert "HH_AUTO_RENEWAL" not in stored
    assert "serviceId" not in stored
    assert "packageName" not in stored
    # Asserted against values that are NOT the model's defaults, so the wiring
    # is what is under test and not the dataclass. Every captured page carries
    # false for both, so the fixture is bent rather than trusted.
    assert derived(posting)["advertising"] is True
    assert derived(posting)["anonymous"] is True


async def test_recruiter_contacts_and_search_telemetry_are_never_stored(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """Personal data we have no reason to hold, and tracking that describes nothing."""
    serve(http, [FULL])

    stored = json.dumps((await collect(hh))[0].raw, ensure_ascii=False)

    for key in ("contactInfo", "asyncContactInfo", "@showContact", "searchRid", "clickUrl"):
        assert key not in stored


async def test_key_skills_arrive_as_a_structured_list(hh: HHSource, http: respx.MockRouter) -> None:
    """The one place this source beats the feeds.

    hh publishes the requirement list as data, so hard-skill coverage for an hh
    posting is a set intersection rather than a model's guess at what the prose
    meant. Matching should branch on this being present.
    """
    serve(http, [FULL])

    skills = derived((await collect(hh))[0])["key_skills"]

    assert "Деловое общение" in skills
    assert len(skills) == 8


async def test_the_page_dictionary_renders_the_coded_fields(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """No vocabulary is hardcoded in this repository.

    hh ships the decoding table with every page, so a value they add tomorrow
    renders tomorrow instead of after somebody notices a blank in the dashboard.
    ``workExperience`` is the exception hh makes itself — it is not in the
    dictionary and its rendering arrives in ``translations``.
    """
    serve(http, [WRAPPED_COLLECTION])

    labels = derived((await collect(hh))[0])["labels"]

    assert labels["employmentForm"] == "Частичная"
    # A hard space, exactly as hh typesets it: the label is passed through
    # verbatim, which is the point of taking it from the page at all.
    assert labels["workFormats"] == "На месте работодателя"
    assert labels["workExperience"] == "не требуется"


async def test_collections_that_arrive_wrapped_or_null_both_come_out_as_lists(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The same field, two shapes, two pages — measured, not imagined."""
    serve(http, [WRAPPED_COLLECTION, NULL_COLLECTIONS])

    wrapped, empty = await collect(hh)

    assert derived(wrapped)["professional_role_ids"] == [40]
    assert derived(wrapped)["key_skills"]
    assert derived(empty)["key_skills"] == []


async def test_the_sitemap_timestamp_travels_with_the_posting(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """It is what the cache and the position are keyed on, so it belongs with the payload."""
    serve(http, [FULL])

    assert derived((await collect(hh))[0])["sitemap_lastmod"].startswith("2026-09-06T10:00:00")


# -- keywords ----------------------------------------------------------


async def test_no_posting_is_dropped_for_relevance(
    hh: HHSource, http: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    """The filter this connector deliberately does not have.

    Filtering here would save nothing — the sitemap carries no title, so a page
    is already fetched and parsed by the time a keyword could be applied — and
    it would cost something permanent. The walk records how far it got, so a
    posting rejected by today's keywords is marked as dealt with and is never
    fetched again by any future run: upload a CV with new skills and everything
    the old keyword set rejected stays invisible forever. Relevance is scored
    downstream, on what is stored.
    """
    serve(http, [FULL, NO_COMPENSATION])

    postings = await collect(hh, SearchQuery(keywords=("кубернетес", "ассемблер")))

    assert [posting.external_id for posting in postings] == [FULL, NO_COMPENSATION]


# -- the parse layer ---------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, []),
        ({"keySkill": ["a", "b"]}, ["a", "b"]),
        ({"driverLicenseType": ["B"]}, ["B"]),
        (["a"], ["a"]),
        ([], []),
        ({"one": "value"}, ["value"]),
        ({"two": 1, "keys": 2}, []),
    ],
)
def test_unwrap_flattens_every_shape_hh_uses(value: Any, expected: list[Any]) -> None:
    """One helper for every collection, because the shape varies per field per page."""
    assert unwrap(value) == expected


@pytest.mark.parametrize(
    ("vacancy_id", "expected"),
    [
        (NO_COMPENSATION, None),
        (SALARY_TO_ONLY, (None, "450000", "KZT", False, SalaryPeriod.MONTH, "MONTHLY")),
        (SALARY_FROM_ONLY, ("450000", None, "KZT", True, SalaryPeriod.MONTH, "MONTHLY")),
        (SALARY_NO_FREQUENCY, ("220000", "220000", "KZT", False, SalaryPeriod.MONTH, None)),
        (FULL, ("300000", "500000", "KZT", True, SalaryPeriod.MONTH, "TWICE_PER_MONTH")),
    ],
)
def test_every_captured_compensation_shape_reads_correctly(
    vacancy_id: str, expected: tuple[Any, ...] | None
) -> None:
    """Six key sets in twenty-two pages, which is why the model has optional fields.

    ``perModeFrom`` and ``perModeTo`` both occur, ``frequency`` is often absent,
    and the bounds arrive alone as often as in pairs.
    """
    salary = _salary(state(vacancy_id)["vacancyView"]["compensation"])

    if expected is None:
        assert salary is None
        return
    assert salary is not None
    assert (
        None if salary.min is None else str(salary.min),
        None if salary.max is None else str(salary.max),
        salary.currency,
        salary.is_gross,
        salary.period,
        salary.frequency,
    ) == expected


def test_no_compensation_is_a_dict_that_passes_a_truthiness_test() -> None:
    """The trap, pinned as a fact about the payload rather than as a comment."""
    empty = state(NO_COMPENSATION)["vacancyView"]["compensation"]

    assert empty == {"noCompensation": {}}
    assert bool(empty) is True, "this is why the check is for the key, not for the value"
    assert _salary(empty) is None


@pytest.mark.parametrize("mode", ["SHIFT", "FLY_IN_FLY_OUT", "SERVICE"])
def test_a_period_we_cannot_express_is_left_unset_rather_than_guessed(mode: str) -> None:
    """A shift is not a day, and a wrong period is a wrong normalised salary.

    The amount and the original mode are kept, so widening the mapping later
    needs no re-crawl; what is refused is inventing a period the enum cannot
    honestly hold.
    """
    salary = _salary({"from": 5000, "currencyCode": "KZT", "gross": False, "mode": mode})

    assert salary is not None
    assert salary.period is None
    assert salary.mode == mode
    assert salary.min == 5000


def test_a_compensation_with_no_amounts_is_no_salary() -> None:
    """A currency and a mode with nothing attached says nothing about the pay."""
    assert _salary({"currencyCode": "KZT", "gross": False, "mode": "MONTH"}) is None


@pytest.mark.parametrize(
    ("formats", "expected"),
    [
        (["ON_SITE"], RemoteType.NO),
        (["REMOTE"], RemoteType.FULL),
        (["HYBRID"], RemoteType.HYBRID),
        (["ON_SITE", "REMOTE"], RemoteType.FULL),
        (["ON_SITE", "HYBRID"], RemoteType.HYBRID),
        (["FIELD_WORK"], RemoteType.NO),
        ([], RemoteType.NO),
        (["SOMETHING_NEW"], RemoteType.NO),
    ],
)
def test_remoteness_takes_the_most_remote_format_offered(
    formats: list[str], expected: RemoteType
) -> None:
    """A posting offering both is one the candidate can take remotely."""
    assert _remote_from(formats) is expected


def test_strip_html_keeps_the_line_breaks_that_carry_meaning() -> None:
    """A requirements list flattened to one line reads badly and embeds worse."""
    text = strip_html(
        "<p>Обязанности:</p><ul><li>Первое</li><li>Второе</li></ul>"
        "<p>Зарплата&nbsp;— <strong>высокая</strong><br/>и вовремя</p>"
    )

    assert text is not None
    assert text.splitlines() == [
        "Обязанности:",
        "Первое",
        "Второе",
        "Зарплата — высокая",
        "и вовремя",
    ]


@pytest.mark.parametrize("markup", ["", None, "<p></p>", "   "])
def test_strip_html_answers_none_for_nothing(markup: str | None) -> None:
    """None rather than an empty string, so the caller records a stub."""
    assert strip_html(markup) is None


# -- sitemap parsing ---------------------------------------------------


def test_a_captured_sitemap_file_parses_into_dated_entries() -> None:
    """The real file, with the real timestamp format and the real ordering."""
    site = HHSite(host=HOST, city="Алматы", country="KZ")
    body = (FIXTURES / "hh_sitemap_vacancy0.xml").read_text(encoding="utf-8")
    import re

    pairs = re.findall(r"<loc>(.*?)</loc>\s*<lastmod>(.*?)</lastmod>", body)
    entries = [entry for loc, mod in pairs if (entry := _entry(site, loc, mod))]

    assert len(entries) == len(pairs) == 10
    assert all(entry.lastmod.tzinfo is not None for entry in entries)
    assert entries[0].external_id.isdigit()


@pytest.mark.parametrize(
    "loc",
    [
        "https://astana.hh.kz/vacancy/1",  # another host's file
        "https://almaty.hh.kz/vacancy/1?utm=x",  # a query string
        "https://almaty.hh.kz/resume/1",  # not a vacancy
        "https://almaty.hh.kz/vacancies/python",  # an SEO landing page
        "https://almaty.hh.kz/vacancy/abc",  # not an id
    ],
)
def test_a_sitemap_line_that_is_not_ours_is_dropped(loc: str) -> None:
    """A sitemap is a document somebody else writes, so its URLs are input."""
    site = HHSite(host=HOST, city="Алматы", country="KZ")

    assert _entry(site, loc, "2026-09-06T10:00:00+03:00") is None


def test_a_url_is_rebuilt_rather_than_taken_from_the_sitemap() -> None:
    """So nothing a sitemap says can put a query string into a URL we then fetch."""
    site = HHSite(host=HOST, city="Алматы", country="KZ")

    entry = _entry(site, f"https://{HOST}/vacancy/136773120", "2026-09-06T10:00:00+03:00")

    assert entry is not None
    assert entry.url == vacancy_url("136773120")
    assert entry.lastmod.utcoffset() == timedelta(hours=3)


def test_a_sitemap_line_with_an_unparsable_date_is_dropped_not_fatal() -> None:
    """One malformed entry must not cost the other 1386."""
    site = HHSite(host=HOST, city="Алматы", country="KZ")

    assert _entry(site, f"https://{HOST}/vacancy/1", "last tuesday") is None


# -- configuration -----------------------------------------------------


def test_the_shipped_site_list_is_usable_and_names_a_default() -> None:
    """A plan naming no city we serve still has somewhere to look."""
    sites = load_sites()

    assert sites
    assert [site for site in sites if site.default]
    assert all(site.host.endswith(("hh.kz", "hh.ru")) for site in sites)


def test_a_planned_area_picks_its_city_and_anything_else_falls_back() -> None:
    """The area is free text out of a resume, so it is matched, never parsed."""
    source = HHSource()

    assert [site.host for site in source.sites_for([SearchQuery(area="Алматы")])] == [
        "almaty.hh.kz"
    ]
    assert [site.host for site in source.sites_for([SearchQuery(area="astana")])] == [
        "astana.hh.kz"
    ]
    assert [site.host for site in source.sites_for([SearchQuery(area="Берлин")])] == [
        "almaty.hh.kz"
    ]
    assert [site.host for site in source.sites_for([SearchQuery()])] == ["almaty.hh.kz"]


def test_the_connector_declares_no_credentials_and_is_always_configured() -> None:
    """Anonymous is the whole design: no key can be added without changing that."""
    source = HHSource()

    assert source.required_credentials == ()
    assert source.requires_auth is False
    assert source.is_configured()
    assert source.unavailable() is None
    assert source.daily_quota is None


async def test_a_source_with_no_position_store_still_walks(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """Absent hooks mean "start from the beginning", not a crash.

    A connector held directly — in a script, in a test — has no pipeline behind
    it to supply a store, and it has to work anyway.
    """
    http.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=HH_ROBOTS))
    http.get(INDEX_URL).mock(
        return_value=httpx.Response(
            200, text=(FIXTURES / "hh_sitemap_index.xml").read_text(encoding="utf-8")
        )
    )
    http.get(VACANCY1_URL).mock(return_value=httpx.Response(200, text=sitemap([])))
    serve(http, [FULL])
    source = HHSource()
    source.bind(client.bind(source))

    postings = [posting async for posting in source.search(SearchQuery())]

    assert len(postings) == 1


async def test_the_transport_refuses_a_search_url_even_if_this_connector_asked(
    hh: HHSource,
) -> None:
    """The ban is not a promise this file makes; it is one the transport keeps."""
    with pytest.raises(SourceError) as excinfo:
        await hh.http.get_text(f"https://{HOST}/search/vacancy")

    assert "закрыт" in excinfo.value.detail


def _index_body() -> str:
    """The captured sitemap index, for the assertion that it really lists resumes."""
    return (FIXTURES / "hh_sitemap_index.xml").read_text(encoding="utf-8")


# -- ordering, the position's lag, and sharing a budget ----------------


async def test_the_freshest_postings_arrive_in_the_first_run(
    hh: HHSource, http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the walk is not purely ascending.

    A city's sitemap spans about a month. Ascending order is what makes the
    position resumable, and on its own it would spend the first several runs on
    three-week-old postings — a good share of them already expired — while the
    vacancy published this morning waited a fortnight. For a job search that is
    the wrong end of the file, so a bounded slice of the newest entries is
    bought first.
    """
    monkeypatch.setattr("app.sources.hh.HEAD_SLICE", 2)
    ids = [FULL, NULL_COLLECTIONS, NO_COMPENSATION, SALARY_TO_ONLY]
    serve(http, ids)  # dated ascending, so SALARY_TO_ONLY is the newest

    postings = await collect(hh)

    assert [posting.external_id for posting in postings][:2] == [SALARY_TO_ONLY, NO_COMPENSATION]
    assert sorted(posting.external_id for posting in postings) == sorted(ids)


async def test_a_head_pass_entry_is_not_bought_twice_in_one_run(
    hh: HHSource, http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ascending pass walks past what the head pass already paid for."""
    monkeypatch.setattr("app.sources.hh.HEAD_SLICE", 2)
    ids = [FULL, NULL_COLLECTIONS, NO_COMPENSATION, SALARY_TO_ONLY]
    routes = serve(http, ids)

    postings = await collect(hh)

    assert len(postings) == len(ids)
    assert [route.call_count for route in routes.values()] == [1, 1, 1, 1]


async def test_the_head_pass_does_not_declare_the_tail_done(
    hh: HHSource, http: respx.MockRouter, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invariant the whole ordering rests on.

    A mark can only ever say "everything up to here is done". Advancing it to an
    entry the head pass fetched would declare every older entry done as well,
    and the tail of the file would never be crawled at all — the exact silent
    loss the ascending pass exists to prevent. So the head pass records nothing.
    """
    monkeypatch.setattr("app.sources.hh.HEAD_SLICE", 2)
    monkeypatch.setattr("app.sources.hh.WATERMARK_LAG", 0)
    # Budget: index + two sitemaps + the two head pages, and nothing after.
    monkeypatch.setattr("app.sources.hh.MAX_PAGES_PER_RUN", 5)
    ids = [FULL, NULL_COLLECTIONS, NO_COMPENSATION, SALARY_TO_ONLY]
    routes = serve(http, ids)

    first = await collect(hh)

    assert [posting.external_id for posting in first] == [SALARY_TO_ONLY, NO_COMPENSATION]
    assert store.saved == {}, "the head pass must record no position at all"

    monkeypatch.setattr("app.sources.hh.MAX_PAGES_PER_RUN", 20)
    second = await collect(hh)

    assert {posting.external_id for posting in second} == set(ids)
    assert routes[FULL].call_count == 1, "the oldest entry was reached exactly once"


def test_the_lag_is_derived_from_the_pipelines_unwritten_window() -> None:
    """The one number this connector shares with the pipeline, checked both ways.

    *The floor.* The runner appends every posting it is handed and commits the
    moment the batch reaches ``UPSERT_BATCH``, so after ``S`` postings it has
    written ``floor(S / B) * B`` of them and holds at most ``B - 1``. The walk
    advances the mark over an entry once at least ``WATERMARK_LAG`` further
    postings have gone by, and ``floor(S / B) * B > S - B`` then puts that
    entry's own posting inside the written part as long as the lag is at least
    ``B - 1``. Below that the mark can name a posting the runner is still
    holding, and a crash makes it unreachable forever.

    *The ceiling, which is the half nothing was checking.* A run advances the
    mark ``max(0, stored - WATERMARK_LAG)`` times, so every posting of margin
    above the floor is a posting a short run must reach before it can record
    anything at all. The value was 200 on the reasoning that margin is free. It
    is not: the live run of 2026-09-06 stored 172 postings before hh's captcha
    stopped it, advanced the mark zero times, wrote no row, and left the next
    run to start from the top of the file again — which is what every run had
    been doing, which is why ``source_state`` was empty rather than stale.

    Both bounds, so that moving ``UPSERT_BATCH`` fails the build and forces the
    derivation to be done again rather than left to a margin that silently stops
    recording.
    """
    assert WATERMARK_LAG >= UPSERT_BATCH - 1
    assert WATERMARK_LAG <= UPSERT_BATCH


async def test_the_position_trails_what_has_been_handed_over(
    hh: HHSource, http: respx.MockRouter, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the mark trails what has been handed over.

    A posting is yielded long before it is written: the runner accumulates a
    batch and commits it in one statement. A mark naming the posting just
    yielded would, after a crash, declare written what was only ever in memory —
    and because the mark says done, those postings are never fetched again.

    The mechanism, at a lag small enough to watch. That the shipped lag is big
    enough to be safe, and small enough to be reachable, is
    ``test_the_lag_is_derived_from_the_pipelines_unwritten_window``; that a run
    at the shipped lag records anything at all is the two tests below it.
    """
    monkeypatch.setattr("app.sources.hh.WATERMARK_LAG", 3)
    monkeypatch.setattr("app.sources.hh.WATERMARK_SAVE_EVERY", 1)
    ids = [
        FULL,
        NULL_COLLECTIONS,
        NO_COMPENSATION,
        SALARY_TO_ONLY,
        SALARY_FROM_ONLY,
        SALARY_NO_FREQUENCY,
    ]
    # Stop the walk before the file is drained: a finished file records its
    # tail, and it is the mid-walk behaviour that matters here. Three requests
    # go on the index and the two sitemaps, five on pages.
    monkeypatch.setattr("app.sources.hh.MAX_PAGES_PER_RUN", 8)
    serve(http, ids)

    await collect(hh)

    saved = store.saved[f"sitemap:{HOST}:vacancy0"]
    # Five entries dealt with, a lag of three: the mark names the second of
    # them and says nothing about the three most recent.
    assert saved["ids_at_lastmod"] == [NULL_COLLECTIONS]


async def test_a_run_cut_by_a_challenge_records_a_position_at_the_shipped_lag(
    hh: HHSource, http: respx.MockRouter, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live run of 2026-09-06, at the constants that live run used.

    **Nothing here patches ``WATERMARK_LAG`` or ``WATERMARK_SAVE_EVERY``, and
    that is the test.** Every other watermark test in this file stands them down
    to 0, 1, 2 or 3, so the mechanism was proved with its safety margin removed
    — the one configuration that never ships — and at the shipped 200 a run had
    to store 201 postings before the mark moved at all. This run stores 172, the
    number hh really served before the captcha, and at 200 it recorded nothing
    anywhere: no periodic write, no write at the exit, an empty ``source_state``
    and a next run that starts again from the top of the file.

    What is safe to record here is derivable and is asserted as a value rather
    than as "something". 172 postings, one per entry, and a lag of 100: entry
    ``i`` is released once ``stored - i > 100``, so the walk releases entries 0
    to 71 and holds the rest. The mark names entry 71 and nothing after it.

    It also pins the write on the way out. 72 releases means one periodic write
    at 50 and 22 releases left over, so a run that only wrote periodically would
    stop at entry 49 and buy 22 pages again next time — on a source where this
    is how every run so far has ended.

    The head pass stays off. At the shipped slice the newest 50 entries include
    the challenged one, the captcha would arrive during a pass that records no
    position by design, and the test would be about the head slice instead.
    """
    ids = minted_ids(173)
    routes = serve_many(http, ids)
    refused = ids[172]
    http.get(vacancy_url(refused)).mock(
        return_value=httpx.Response(302, headers={"location": CAPTCHA_URL})
    )

    got = await drain_until_challenged(hh)

    assert len(got) == 172, "the run under test is the one hh really stopped"
    saved = store.saved[f"sitemap:{HOST}:vacancy0"]
    assert saved["ids_at_lastmod"] == [ids[71]]
    assert saved["lastmod"].startswith(dated(ids)[71][1].isoformat()[:19])
    assert refused not in saved["ids_at_lastmod"], "the refused page was declared dealt with"

    # And the consequence, which is the thing that was actually broken: the next
    # run starts at entry 72 rather than at entry 0. One page of budget is
    # enough to show which one it asks for.
    monkeypatch.setattr("app.sources.hh.MAX_PAGES_PER_RUN", 4)
    await collect(hh)

    assert routes[ids[72]].call_count == 2, "the run did not resume where the mark said"
    assert routes[ids[0]].call_count == 1, "the corpus was re-read from the top"


async def test_a_run_cut_by_its_page_budget_records_a_position_at_the_shipped_lag(
    hh: HHSource, http: respx.MockRouter, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other exit, and the one that governs every run of a full crawl.

    A city's sitemap holds some 13 557 entries and a run may buy 1200 pages, so
    for the dozen runs a backfill takes, the budget is how every run ends. The
    lag is not patched here either; ``MAX_PAGES_PER_RUN`` is, because it is a
    magnitude rather than a threshold — no arithmetic in the walk compares
    against it — and 1200 mocked pages would prove nothing 147 do not.

    147 pages bought (three of the budget go on the index and the two sitemap
    files), 147 postings, a lag of 100: entries 0 to 46 are released. 47 is
    fewer than ``WATERMARK_SAVE_EVERY``, so no periodic write ever fires and
    what is asserted below is the exit itself writing. At the old lag of 200 it
    wrote nothing, and the next run re-bought all 147 pages.
    """
    assert WATERMARK_SAVE_EVERY > 47, "otherwise a periodic write, not the exit, is under test"
    monkeypatch.setattr("app.sources.hh.MAX_PAGES_PER_RUN", 150)
    ids = minted_ids(200)
    serve_many(http, ids)

    postings = await collect(hh)

    assert len(postings) == 147
    saved = store.saved[f"sitemap:{HOST}:vacancy0"]
    assert saved["ids_at_lastmod"] == [ids[46]]


async def test_a_position_that_cannot_be_written_does_not_replace_the_reason_the_run_stopped(
    hh: HHSource,
    http: respx.MockRouter,
    store: StateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recording the position on the way out must not become the failure reported.

    The walk now saves its mark as a challenge unwinds. If that write fails —
    the database is the thing that is down — letting it out replaces
    ``HHChallengedError`` with a store error, and the two are read completely
    differently by whoever gets the run report: one is rescheduled, the other
    sends somebody to debug a connector that is fine. The pipeline makes the
    same trade one layer up for its own rescue write.
    """
    monkeypatch.setattr("app.sources.hh.WATERMARK_LAG", 1)
    # High enough that no periodic write fires: what must survive a failing
    # store is the write on the way out, and only that one.
    monkeypatch.setattr("app.sources.hh.WATERMARK_SAVE_EVERY", 999)

    attempts: list[str] = []

    async def refuse(key: str, value: dict[str, Any]) -> None:
        attempts.append(key)
        raise RuntimeError("source_state is unavailable")

    hh.with_state(store.load, refuse)
    ids = [FULL, NULL_COLLECTIONS, NO_COMPENSATION]
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(dated(ids))))
    for vacancy_id in (FULL, NULL_COLLECTIONS):
        http.get(vacancy_url(vacancy_id)).mock(
            return_value=httpx.Response(200, text=page(state(vacancy_id)))
        )
    http.get(vacancy_url(NO_COMPENSATION)).mock(
        return_value=httpx.Response(302, headers={"location": CAPTCHA_URL})
    )

    with pytest.raises(HHChallengedError):
        await collect(hh)

    # Not vacuous: without the write on the way out there is nothing to fail,
    # and a test asserting only that the challenge surfaced would pass on a
    # walk that never tried to record anything.
    assert attempts == [f"sitemap:{HOST}:vacancy0"]
    assert store.saved == {}


async def test_the_shipped_head_slice_walks_a_small_file_once_and_records_it(
    hh: HHSource, http: respx.MockRouter, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second constant this file had never run as it ships.

    ``HEAD_SLICE`` is monkeypatched to 0 by an autouse fixture, so every test
    here walks with the newest-first pass switched off, and the two that do turn
    it on set it to 2. Fifty is what runs in production, and on a file with
    fewer than fifty outstanding entries it changes the shape of the whole walk:
    every entry is bought by the head pass, the ascending pass then buys nothing
    and only walks past them, and the position has to come out of that walk
    anyway. Nothing was checking that it did.
    """
    monkeypatch.setattr("app.sources.hh.HEAD_SLICE", SHIPPED_HEAD_SLICE)
    ids = [FULL, NULL_COLLECTIONS, NO_COMPENSATION]
    routes = serve(http, ids)

    postings = await collect(hh)

    # Newest first, which is what the head pass is for.
    assert [posting.external_id for posting in postings] == list(reversed(ids))
    assert [route.call_count for route in routes.values()] == [1, 1, 1]
    assert store.saved[f"sitemap:{HOST}:vacancy0"]["ids_at_lastmod"] == [NO_COMPENSATION]


async def test_every_configured_city_is_reached_before_any_city_gets_seconds(
    client: SourceClient,
    http: respx.MockRouter,
    store: StateStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A shared counter walked in order starves the cities after the first.

    Backfilling one city takes about a dozen runs, so a single budget spent
    front to back means the second city in the file sees nothing for days —
    while the run reports success and an empty crawl looks like an empty market.
    """
    sites = tmp_path / "hh_sites.yaml"
    sites.write_text(
        "sites:\n"
        "  - host: almaty.hh.kz\n    city: Алматы\n    country: KZ\n    default: true\n"
        "    aliases: [алматы]\n"
        "  - host: astana.hh.kz\n    city: Астана\n    country: KZ\n    default: true\n"
        "    aliases: [астана]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.sources.hh.SITES_FILE", sites)
    monkeypatch.setattr("app.sources.hh.HEAD_SLICE", 0)
    monkeypatch.setattr("app.sources.hh.WATERMARK_LAG", 0)
    # Eight requests, four per city: index, two sitemaps, one page.
    monkeypatch.setattr("app.sources.hh.MAX_PAGES_PER_RUN", 8)

    for host in ("almaty.hh.kz", "astana.hh.kz"):
        http.get(f"https://{host}/robots.txt").mock(
            return_value=httpx.Response(200, text=HH_ROBOTS)
        )
        http.get(f"https://{host}/sitemap/main.xml").mock(
            return_value=httpx.Response(
                200,
                text=(FIXTURES / "hh_sitemap_index.xml")
                .read_text(encoding="utf-8")
                .replace("almaty.hh.kz", host),
            )
        )
        http.get(f"https://{host}/sitemap/vacancy1.xml").mock(
            return_value=httpx.Response(200, text=sitemap([]))
        )
        body = sitemap(dated([FULL, NULL_COLLECTIONS])).replace("almaty.hh.kz", host)
        http.get(f"https://{host}/sitemap/vacancy0.xml").mock(
            return_value=httpx.Response(200, text=body)
        )
        for vacancy_id in (FULL, NULL_COLLECTIONS):
            http.get(f"https://{host}/vacancy/{vacancy_id}").mock(
                return_value=httpx.Response(200, text=page(state(vacancy_id)))
            )

    source = HHSource()
    source.bind(client.bind(source)).with_state(store.load, store.save)
    postings = [posting async for posting in source.search_batch([SearchQuery()])]

    hosts = {posting.url.split("/")[2] for posting in postings}
    assert hosts == {"almaty.hh.kz", "astana.hh.kz"}


# -- sitemap files that are empty, and files that are broken -----------


async def test_an_empty_sitemap_file_is_survivable(hh: HHSource, http: respx.MockRouter) -> None:
    """A small city legitimately has one, and it must not kill the walk."""
    http.get(VACANCY0_URL).mock(
        return_value=httpx.Response(
            200,
            text=(
                "<?xml version='1.0' encoding='utf-8'?>"
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'
            ),
        )
    )

    assert await collect(hh) == []


async def test_a_sitemap_listing_urls_it_cannot_read_is_a_markup_change(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The distinction the empty case must not swallow.

    A file with entries we can no longer parse — a renamed element, a dropped
    lastmod — is hh changing the format, and it has to be loud. A file with
    nothing in it is just empty. Both look like "zero entries" to a caller that
    does not check which.
    """
    http.get(VACANCY0_URL).mock(
        return_value=httpx.Response(
            200,
            text=(
                "<?xml version='1.0' encoding='utf-8'?><urlset><url>"
                f"<loc>{vacancy_url(FULL)}</loc><changed>2026-09-06</changed>"
                "</url></urlset>"
            ),
        )
    )

    with pytest.raises(HHMarkupError):
        await collect(hh)


async def test_a_page_that_answers_for_a_different_vacancy_is_not_stored(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The shared client follows redirects, so the page answering is not always the page asked for.

    hh forwards a superseded posting to its replacement. Storing that under the
    id we asked for would file one vacancy's text under another's key, and every
    later run would overwrite it again.
    """
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(dated([FULL]))))
    http.get(vacancy_url(FULL)).mock(
        return_value=httpx.Response(200, text=page(state(NULL_COLLECTIONS)))
    )

    assert await collect(hh) == []


@pytest.mark.parametrize(
    "frequency", ["MONTHLY", "TWICE_PER_MONTH", "WEEKLY", "DAILY", "PER_PROJECT"]
)
def test_the_payment_schedule_is_never_read_as_the_period(frequency: str) -> None:
    """``frequency`` says how often it is paid, ``mode`` says what it is per.

    Both are measured on the same postings — a monthly salary paid twice a month
    is ordinary here — and reading the schedule as the period would divide a
    salary by two, by 4.3 or by 21 before anything compares it to another
    currency.
    """
    salary = _salary(
        {
            "from": 300000,
            "currencyCode": "KZT",
            "gross": False,
            "mode": "MONTH",
            "frequency": frequency,
        }
    )

    assert salary is not None
    assert salary.period is SalaryPeriod.MONTH
    assert salary.frequency == frequency


async def test_a_sitemap_index_pointing_off_host_is_not_followed(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The index is a document somebody else writes, so its URLs are input.

    The file-name pattern is anchored on the tail of the path and would match
    ``vacancy0.xml`` on any host at all. ``_entry`` already checks the host of
    every vacancy URL for this reason; the index deserves the same.
    """
    http.get(INDEX_URL).mock(
        return_value=httpx.Response(
            200,
            text=(
                "<?xml version='1.0'?><sitemapindex><sitemap>"
                "<loc>https://evil.example/sitemap/vacancy0.xml</loc>"
                "</sitemap></sitemapindex>"
            ),
        )
    )
    elsewhere = http.get("https://evil.example/sitemap/vacancy0.xml").mock(
        return_value=httpx.Response(200, text=sitemap(dated([FULL])))
    )

    # Nothing on this host is a vacancy sitemap, which is itself a markup change.
    with pytest.raises(HHMarkupError):
        await collect(hh)

    assert elsewhere.call_count == 0


async def test_entries_that_yield_nothing_do_not_drag_the_position_forward(
    hh: HHSource, http: respx.MockRouter, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lag is measured in postings, and a dead stretch is why.

    Counting it in sitemap entries looks equivalent and is not: a run of
    entries that yield nothing — taken down, archived, answering for a
    different vacancy — pushes the mark forward while the postings before them
    are still in the pipeline's unwritten batch. A corpus of fourteen thousand
    pages has such stretches, and the damage is silent, because the mark then
    says those postings were dealt with.
    """
    monkeypatch.setattr("app.sources.hh.WATERMARK_LAG", 2)
    monkeypatch.setattr("app.sources.hh.WATERMARK_SAVE_EVERY", 1)
    monkeypatch.setattr("app.sources.hh.MAX_PAGES_PER_RUN", 8)
    live = [FULL, NULL_COLLECTIONS]
    # One more dead entry than the run can reach, so the file does not drain:
    # a drained file legitimately records its tail once the walk is over, and
    # what is under test here is the mark moving mid-walk.
    dead = [NO_COMPENSATION, SALARY_TO_ONLY, SALARY_FROM_ONLY, SALARY_NO_FREQUENCY]
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(dated(live + dead))))
    for vacancy_id in live:
        http.get(vacancy_url(vacancy_id)).mock(
            return_value=httpx.Response(200, text=page(state(vacancy_id)))
        )
    for vacancy_id in dead:
        http.get(vacancy_url(vacancy_id)).mock(
            return_value=httpx.Response(404, text=page({"vacancyView": {}, "errorCode": 404}))
        )

    postings = await collect(hh)

    assert [posting.external_id for posting in postings] == live
    # Two postings yielded and three dead entries after them: with the lag
    # counted in entries the mark would have walked past both postings. Counted
    # in postings, it has not moved at all.
    assert store.saved == {}


async def test_a_drained_file_records_its_tail_only_once_the_walk_is_over(
    hh: HHSource, http: respx.MockRouter, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blocker this ordering exists to prevent.

    A file that runs out has nothing left to keep the lag honest, so recording
    its remainder immediately declares written a batch the pipeline is still
    holding. If the run then dies in a later file, those postings are lost and
    the position says they were dealt with — they are never fetched again.

    So the remainder waits until every posting of the run has been handed over.
    Here the run dies in the second file, and the first file's tail is
    deliberately not recorded.
    """
    monkeypatch.setattr("app.sources.hh.WATERMARK_LAG", 1)
    monkeypatch.setattr("app.sources.hh.WATERMARK_SAVE_EVERY", 1)
    http.get(VACANCY0_URL).mock(
        return_value=httpx.Response(200, text=sitemap(dated([FULL, NULL_COLLECTIONS])))
    )
    for vacancy_id in (FULL, NULL_COLLECTIONS):
        http.get(vacancy_url(vacancy_id)).mock(
            return_value=httpx.Response(200, text=page(state(vacancy_id)))
        )
    broken = [NO_COMPENSATION, SALARY_TO_ONLY, SALARY_FROM_ONLY]
    http.get(VACANCY1_URL).mock(return_value=httpx.Response(200, text=sitemap(dated(broken))))
    for vacancy_id in broken:
        http.get(vacancy_url(vacancy_id)).mock(
            return_value=httpx.Response(200, text="<html>hh redesigned this page</html>")
        )

    with pytest.raises(HHMarkupError):
        await collect(hh)

    recorded = store.saved.get(f"sitemap:{HOST}:vacancy0", {}).get("ids_at_lastmod", [])
    assert NULL_COLLECTIONS not in recorded, "the last posting of the file was declared done"


async def test_a_walk_that_finishes_records_the_tail_it_was_holding(
    hh: HHSource, http: respx.MockRouter, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And the other half: holding it back forever would re-buy it every run."""
    monkeypatch.setattr("app.sources.hh.WATERMARK_LAG", 1)
    routes = serve(http, [FULL, NULL_COLLECTIONS])

    assert len(await collect(hh)) == 2
    assert store.saved[f"sitemap:{HOST}:vacancy0"]["ids_at_lastmod"] == [NULL_COLLECTIONS]

    assert await collect(hh) == []
    assert routes[FULL].call_count == 1


async def test_the_experience_requirement_survives_the_page(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """A field with no alias reads nothing at all, and reads it quietly.

    ``populate_by_name`` only adds the field name as an accepted key when there
    IS an alias; without one, ``work_experience`` never matched hh's
    ``workExperience`` and every posting carried a null where a requirement
    should be. Nothing failed — the block was simply always empty, on all 22
    captured pages.
    """
    serve(http, [FULL])

    block = derived((await collect(hh))[0])

    assert block["work_experience"] == "between1And3"
    assert block["labels"]["workExperience"]


async def test_language_requirements_do_not_pretend_to_be_skills(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """hh renders them into keySkills, and matching intersects that list at full weight.

    Nine of 121 measured skills were strings like "Русский — C1 — Продвинутый".
    Left in the skill set, every one of them reads as a required hard skill the
    candidate does not have, and the name of the missing skill is a sentence
    about a language.
    """
    payload = state(FULL)
    payload["vacancyView"]["keySkills"] = {
        "keySkill": ["Python", "Русский — C1 — Продвинутый", "SQL"]
    }
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(dated([FULL]))))
    http.get(vacancy_url(FULL)).mock(return_value=httpx.Response(200, text=page(payload)))

    block = derived((await collect(hh))[0])

    assert block["key_skills"] == ["Python", "SQL"]
    assert block["language_requirements"] == ["Русский — C1 — Продвинутый"]


async def test_the_sitemaps_are_never_served_from_the_disk_cache(
    client: SourceClient,
    http: respx.MockRouter,
    store: StateStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The thirty-day cache is for pages, and it must not reach the index.

    A vacancy page carries its own lastmod as a cache salt, so an edited posting
    misses and an untouched one hits — which is the whole reason the TTL can be
    a month. The sitemaps have no such salt and their entire job is to say what
    changed, so inheriting that TTL freezes them: with the dev cache on, the
    second run reads a month-old index and finds no vacancy hh published since.
    """
    monkeypatch.setattr(settings, "http_cache_dir", tmp_path)
    cached = SourceClient(cache=ResponseCache(tmp_path), sleep=_instant)
    try:
        http.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=HH_ROBOTS))
        http.get(INDEX_URL).mock(
            return_value=httpx.Response(
                200, text=(FIXTURES / "hh_sitemap_index.xml").read_text(encoding="utf-8")
            )
        )
        http.get(VACANCY1_URL).mock(return_value=httpx.Response(200, text=sitemap([])))
        http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(dated([FULL]))))
        for vacancy_id in (FULL, NULL_COLLECTIONS):
            http.get(vacancy_url(vacancy_id)).mock(
                return_value=httpx.Response(200, text=page(state(vacancy_id)))
            )
        source = HHSource()
        source.bind(cached.bind(source)).with_state(store.load, store.save)

        first = [posting async for posting in source.search_batch([SearchQuery()])]
        assert [posting.external_id for posting in first] == [FULL]

        # hh publishes another vacancy; the live sitemap now lists both.
        http.get(VACANCY0_URL).mock(
            return_value=httpx.Response(200, text=sitemap(dated([FULL, NULL_COLLECTIONS])))
        )
        second = [posting async for posting in source.search_batch([SearchQuery()])]

        assert [posting.external_id for posting in second] == [NULL_COLLECTIONS]
    finally:
        await cached.aclose()


async def test_a_shape_change_reports_the_field_and_not_the_page(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The error a person reads must not carry the payload it was reading.

    This string is committed to ``pipeline_run.errors`` and served by the API.
    Pydantic puts the whole validated object into every error entry, so with the
    input left in, one renamed key ships the recruiter's contacts, the
    employer's billing block and the manager's id into a JSONB column and an
    HTTP response — 6.8 kB of it, measured on a real page.
    """
    # Three, because one odd page is tolerated: the raise is what happens when
    # it is a pattern, and the raise is what reaches the API.
    ids = [FULL, NULL_COLLECTIONS, NO_COMPENSATION]
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(dated(ids))))
    for vacancy_id in ids:
        broken = state(vacancy_id)
        del broken["vacancyView"]["name"]
        http.get(vacancy_url(vacancy_id)).mock(return_value=httpx.Response(200, text=page(broken)))

    with pytest.raises(HHMarkupError) as excinfo:
        await collect(hh)

    detail = excinfo.value.detail
    assert "'loc': ('name',)" in detail or "'loc': ['name']" in detail
    for leaked in ("contactInfo", "managerId", "HH_AUTO_RENEWAL", "vacancyProperties"):
        assert leaked not in detail
    assert len(detail) < 500


async def test_every_derived_field_is_wired_to_the_page_it_came_from(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The block a walk actually produces, field by field, from a real page.

    Each rule below has its own unit test against the helper that implements it
    — the salary shapes, remoteness from workFormats, the derived publication
    flags. What none of those can catch is the wiring: replace
    ``salary=_salary(...)`` with ``salary=None`` in ``_derive`` and every one of
    them still passes, because they never look at what a crawl hands over.
    Twelve such cuts were applied at once to this connector and the whole suite
    stayed green.

    So this asserts the finished block, on the payload hh really served for
    vacancy 136773120 on 2026-09-06. It is deliberately not a spot check: a
    field added to HHDerived and left unwired should fail here.
    """
    serve(http, [FULL])

    block = derived((await collect(hh))[0])

    assert block == {
        "external_id": FULL,
        "url": vacancy_url(FULL),
        "city": "Алматы",
        "country": "KZ",
        "remote": "no",
        "salary": {
            "min": "300000",
            "max": "500000",
            "currency": "KZT",
            "is_gross": True,
            "period": "month",
            "mode": "MONTH",
            "frequency": "TWICE_PER_MONTH",
        },
        "published_at": "2026-09-06T09:56:03.075000+03:00",
        "expires_at": "2026-09-30T09:56:03.086000+03:00",
        "key_skills": [
            "Деловое общение",
            "Эмпатия",
            "Телефонные переговоры",
            "Работа с базами данных",
            "Деловая переписка",
            "Работа с большим объемом информации",
            "забота",
            "Обратная связь",
        ],
        "language_requirements": [],
        "professional_role_ids": [121],
        "work_formats": ["ON_SITE"],
        "employment_form": "FULL",
        "work_experience": "between1And3",
        "labels": {
            "employmentForm": "Полная",
            "workFormats": "На месте работодателя",
            "workExperience": "1–3 года",
        },
        "closed_for_applicants": False,
        "accredited_it_employer": False,
        "employer_on_additional_check": False,
        "anonymous": False,
        "advertising": False,
        "pay_for_performance": False,
        "description_html": block["description_html"],
        "sitemap_lastmod": "2026-09-06T10:00:00Z",
    }
    assert block["description_html"].startswith("<p><strong>Мы Inspire</strong>")


async def test_the_sitemap_timestamp_is_what_the_cache_is_keyed_on(
    hh: HHSource, http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mechanism the thirty-day TTL rests on, asserted where it is supplied.

    The transport's own test proves a changed salt is a miss and an unchanged
    one a hit. What that cannot show is that hh passes one: every test here runs
    with the dev cache off, so deleting the argument breaks nothing in this file
    and nothing in that one, and a deployment with HTTP_CACHE_DIR set then
    serves month-old vacancy pages.
    """
    salts: list[str | None] = []
    original = type(hh.http).get_text

    async def spy(self: Any, url: str, **kwargs: Any) -> str:
        if "/vacancy/" in url:
            salts.append(kwargs.get("cache_salt"))
        text: str = await original(self, url, **kwargs)
        return text

    monkeypatch.setattr(type(hh.http), "get_text", spy)
    serve(http, [FULL])

    await collect(hh)

    assert salts == ["2026-09-06T10:00:00+00:00"]


# -- the rules a mutation pass found nothing holding ---------------------


async def test_a_posting_that_answers_410_is_skipped_like_a_404(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """410 Gone means the same thing as 404 here and must not abort the walk.

    hh answers 404 for most removals, and the walk survives that. Nothing held
    the other half of the pair: with 410 dropped from GONE_STATUSES the
    exception escapes ``_fetch``, the whole crawl ends on one retired posting,
    and the suite stayed green.
    """
    http.get(VACANCY0_URL).mock(
        return_value=httpx.Response(200, text=sitemap(dated([FULL, NO_COMPENSATION])))
    )
    http.get(vacancy_url(FULL)).mock(return_value=httpx.Response(410, text="Gone"))
    http.get(vacancy_url(NO_COMPENSATION)).mock(
        return_value=httpx.Response(200, text=page(state(NO_COMPENSATION)))
    )

    postings = await collect(hh)

    assert [posting.external_id for posting in postings] == [NO_COMPENSATION]


def test_an_hourly_rate_keeps_its_period() -> None:
    """Both mappings, not just the one every captured page happened to use.

    Every live sample paid by the month, so removing HOUR from the table broke
    nothing anybody could see — and an hourly salary with no period is a number
    the normaliser cannot compare to anything.
    """
    hourly = _salary({"from": 4000, "currencyCode": "KZT", "gross": False, "mode": "HOUR"})
    monthly = _salary({"from": 400000, "currencyCode": "KZT", "gross": False, "mode": "MONTH"})

    assert hourly is not None and hourly.period is SalaryPeriod.HOUR
    assert monthly is not None and monthly.period is SalaryPeriod.MONTH


async def test_the_name_the_employer_chose_is_the_one_stored(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """``visibleName`` wins over ``name``, which the fixtures cannot show.

    Every captured page has the two equal, so reversing the preference was
    invisible to the suite. They differ in the wild — a legal entity on one side
    and the brand a candidate would recognise on the other — and the dashboard
    should show the second.
    """
    payload = state(FULL)
    payload["vacancyView"]["company"]["name"] = 'ТОО "Инспайр Интернешнл КЗ"'
    payload["vacancyView"]["company"]["visibleName"] = "Inspire"
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(dated([FULL]))))
    http.get(vacancy_url(FULL)).mock(return_value=httpx.Response(200, text=page(payload)))

    assert (await collect(hh))[0].company == "Inspire"


async def test_the_country_comes_from_the_page_not_from_the_site(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The site's country is the fallback, and the fixtures cannot tell them apart.

    Every captured page is a KZ vacancy on a KZ host, so reading the site
    instead of the page changed nothing visible. hh publishes postings whose
    area sits in another country — that is what ``@countryIsoCode`` is for.
    """
    payload = state(FULL)
    payload["vacancyView"]["area"]["@countryIsoCode"] = "UZ"
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(dated([FULL]))))
    http.get(vacancy_url(FULL)).mock(return_value=httpx.Response(200, text=page(payload)))

    assert derived((await collect(hh))[0])["country"] == "UZ"


def test_the_tie_list_cannot_grow_without_bound() -> None:
    """The cap on ids remembered at one timestamp, which nothing was holding.

    The list exists so that resuming neither repeats nor skips entries sharing a
    second. It is stored as JSONB on every save, so an unbounded one is a row
    that grows all run; past the cap the tie resolves by repeating, which costs
    requests and never costs a posting.
    """
    mark = FileWatermark()
    for index in range(MAX_TIED_IDS + 10):
        mark = mark.advanced(
            SitemapEntry(external_id=str(index), url=vacancy_url(str(index)), lastmod=WHEN)
        )

    assert len(mark.ids_at_lastmod) == MAX_TIED_IDS
    # The cap keeps the NEWEST ids: those are the ones a resume would otherwise
    # re-fetch first.
    assert mark.ids_at_lastmod[-1] == str(MAX_TIED_IDS + 9)
    assert mark.is_done(
        SitemapEntry(external_id=str(MAX_TIED_IDS + 9), url=vacancy_url("x"), lastmod=WHEN)
    )


def test_a_sitemap_id_too_long_for_the_column_is_dropped() -> None:
    """``vacancy_source.external_id`` is 200 characters and a cut id is a different key.

    hh's ids are nine digits, so nothing in the fixtures comes near it — which
    is exactly why the guard needs a test rather than a sample.
    """
    site = HHSite(host=HOST, city="Алматы", country="KZ")
    long_id = "1" * (MAX_EXTERNAL_ID + 1)

    assert _entry(site, f"https://{HOST}/vacancy/{long_id}", "2026-09-06T10:00:00+03:00") is None
    assert _entry(site, f"https://{HOST}/vacancy/1" * 1, "2026-09-06T10:00:00+03:00") is not None


# -- a check for robots stops the walk ---------------------------------


CAPTCHA_URL = f"https://{HOST}/account/captcha?backurl=/vacancy/1&state=7f3c1a"


async def drain_until_challenged(source: HHSource) -> list[RawPosting]:
    """Everything the walk yielded before hh refused it.

    Written out rather than reusing ``collect`` because the list comprehension
    there discards its partial result when the generator raises, and what the
    walk had already handed over is precisely what this section is about.
    """
    got: list[RawPosting] = []
    with pytest.raises(HHChallengedError):
        async for posting in source.search_batch([SearchQuery()]):
            got.append(posting)
    return got


async def test_a_captcha_redirect_stops_the_walk_and_keeps_what_it_bought(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The live run of 2026-09-06, in miniature.

    172 vacancies had been read when request 173 — a plain ``/vacancy/{id}``
    with no query string — was answered ``302`` into hh's captcha. Everything
    read before it is real and paid for and is handed over; the walk then stops
    for this source rather than trying the next page, because a captcha is a
    decision about this crawler and the next page would get the same one.
    """
    ids = [FULL, NULL_COLLECTIONS, NO_COMPENSATION]
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(dated(ids))))
    for vacancy_id in (FULL, NULL_COLLECTIONS):
        http.get(vacancy_url(vacancy_id)).mock(
            return_value=httpx.Response(200, text=page(state(vacancy_id)))
        )
    http.get(vacancy_url(NO_COMPENSATION)).mock(
        return_value=httpx.Response(302, headers={"location": CAPTCHA_URL})
    )
    captcha = http.get(url__startswith=f"https://{HOST}/account/captcha").mock(
        return_value=httpx.Response(200, text="докажите, что вы не робот")
    )

    got = await drain_until_challenged(hh)

    assert [posting.external_id for posting in got] == [FULL, NULL_COLLECTIONS]
    assert captcha.call_count == 0, "the captcha page is never fetched, let alone solved"


async def test_a_challenge_is_not_counted_as_an_unreadable_page(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """One odd page is tolerated; one captcha is not, and the difference matters.

    The markup tolerance exists because a truncated response must not wedge a
    walk that resumes at the same entry every run. A challenge is the opposite
    situation: the markup was never seen, the site has decided something about
    us, and spending two more requests to confirm it before reporting a page
    redesign that did not happen is both wrong and impolite.
    """
    ids = [FULL, NULL_COLLECTIONS, NO_COMPENSATION]
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(dated(ids))))
    http.get(vacancy_url(FULL)).mock(
        return_value=httpx.Response(302, headers={"location": CAPTCHA_URL})
    )
    later = {
        vacancy_id: http.get(vacancy_url(vacancy_id)).mock(
            return_value=httpx.Response(200, text=page(state(vacancy_id)))
        )
        for vacancy_id in (NULL_COLLECTIONS, NO_COMPENSATION)
    }

    with pytest.raises(HHChallengedError) as excinfo:
        await collect(hh)

    assert MAX_MARKUP_FAILURES == 3, "the tolerance this must not consume"
    assert all(route.call_count == 0 for route in later.values())
    assert "проверкой на робота" in excinfo.value.detail
    assert not isinstance(excinfo.value, HHMarkupError)


async def test_a_challenge_never_declares_the_page_it_was_refused_done(
    hh: HHSource, http: respx.MockRouter, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interruption that proved the watermark, asserted as a rule.

    The position never names the page we were refused, nor any entry after it,
    so the next run asks for that page again — which is the whole reason a
    challenged run is recorded as an interruption and not as a failure.

    Renamed and given a third assertion on 2026-09-07. It used to be called
    ``test_a_challenge_leaves_the_crawl_position_where_it_was``, which claimed
    more than it checked and more than is true: the mark does move on a
    challenged run, up to the last entry whose posting the pipeline has written,
    and the run this file was written for moved it nowhere only because the lag
    was unreachable. What the assertions always pinned — and still pin — is
    which entries the mark may NOT name. The positive one below is new, so the
    test can no longer pass by recording nothing at all.
    """
    monkeypatch.setattr("app.sources.hh.WATERMARK_LAG", 1)
    monkeypatch.setattr("app.sources.hh.WATERMARK_SAVE_EVERY", 1)
    ids = [FULL, NULL_COLLECTIONS, NO_COMPENSATION, SALARY_TO_ONLY]
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(dated(ids))))
    for vacancy_id in (FULL, NULL_COLLECTIONS):
        http.get(vacancy_url(vacancy_id)).mock(
            return_value=httpx.Response(200, text=page(state(vacancy_id)))
        )
    for vacancy_id in (NO_COMPENSATION, SALARY_TO_ONLY):
        http.get(vacancy_url(vacancy_id)).mock(
            return_value=httpx.Response(302, headers={"location": CAPTCHA_URL})
        )

    await drain_until_challenged(hh)

    recorded = store.saved.get(f"sitemap:{HOST}:vacancy0", {}).get("ids_at_lastmod", [])
    assert NO_COMPENSATION not in recorded, "the refused page was declared dealt with"
    assert SALARY_TO_ONLY not in recorded, "an entry never reached was declared dealt with"
    # And it did move: to the last entry the lag had released, and no further.
    assert recorded == [FULL]


async def test_the_transport_and_not_this_connector_decides_what_a_captcha_is(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The connector builds no ``/account`` URL and needs no rule about one.

    The redirect is chosen by hh and resolved inside httpx, so the only place
    that sees the hop is the shared client's request hook. A check written here
    would look like the guard while guarding nothing — the page would already
    have been fetched by the time this module could look at it.
    """
    serve(http, [FULL])
    http.get(vacancy_url(FULL)).mock(
        return_value=httpx.Response(302, headers={"location": CAPTCHA_URL})
    )

    with pytest.raises(HHChallengedError) as excinfo:
        await collect(hh)

    assert excinfo.value.challenge_path == "/account/captcha"
    assert excinfo.value.challenge_host == HOST


async def test_a_challenge_names_the_host_and_the_page_in_the_log(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The one behaviour the ``except HHChallengedError`` clause actually adds.

    Not consuming the markup tolerance is a property of the type — a challenge
    is not an ``HHMarkupError``, so the counter never sees one — and holds with
    that clause deleted; measured by deleting it, at which point every other
    test in this file still passed. What deleting it loses is this line, which
    is the only record of which host and which page a crawl was cut on, and
    which somebody deciding whether to walk that host more slowly has to read.
    Its two counters say how much of the walk had already been paid for.
    """
    ids = [FULL, NULL_COLLECTIONS]
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(dated(ids))))
    http.get(vacancy_url(FULL)).mock(return_value=httpx.Response(200, text=page(state(FULL))))
    http.get(vacancy_url(NULL_COLLECTIONS)).mock(
        return_value=httpx.Response(302, headers={"location": CAPTCHA_URL})
    )

    with capture_logs() as records:
        got = await drain_until_challenged(hh)

    assert [posting.external_id for posting in got] == [FULL]
    stopped = [record for record in records if record["event"] == "sources.hh.challenged"]
    assert len(stopped) == 1, "a crawl is cut once and says so once"
    assert stopped[0]["log_level"] == "warning"
    assert stopped[0]["host"] == HOST
    assert stopped[0]["url"] == vacancy_url(NULL_COLLECTIONS)
    assert stopped[0]["fetched"] == 2
    assert stopped[0]["stored"] == 1


class ChallengeWithAStatusError(HHChallengedError):
    """A challenge that has learned a response status, as a later one might.

    Today's carries none, and that is an accident of how this one arrives: the
    transport refuses the redirect before the captcha hop is sent, so there is
    no response to read a status off. A challenge delivered as a status code
    would have one, and 404 is the value that makes the order of the ``except``
    clauses in ``_fetch`` load-bearing rather than decorative — it is in
    ``GONE_STATUSES``, so the clause below would file the page as taken down.
    """

    def __init__(self) -> None:
        super().__init__(
            "hh: источник ответил проверкой на робота",
            host=HOST,
            path="/account/captcha",
            source_slug="hh",
        )
        self.extra["response_status"] = 404


async def test_a_challenge_carrying_a_gone_status_is_still_a_stopped_crawl(
    hh: HHSource, http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``except HHChallengedError: raise`` in ``_fetch``, pinned to a test.

    That clause is a no-op today — nothing carries a status for the clause
    under it to read — which is exactly why it would survive a review as dead
    code and be deleted. The day a challenge does carry 404, deleting it means
    ``GONE_STATUSES`` matches, ``_fetch`` returns None, the page is counted as
    a posting taken down, and the walk carries on through a host that has just
    stopped us: no exception, no report, one more request. This asserts the
    ordering instead of the accident that currently hides it.
    """
    assert 404 in GONE_STATUSES, "otherwise this proves nothing about the ordering"
    ids = [FULL, NULL_COLLECTIONS]
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(dated(ids))))
    asked: list[str] = []
    served = hh.http.get_text

    async def challenge_the_vacancy_pages(url: str, **kwargs: Any) -> str:
        """Serve the sitemaps as usual; answer any vacancy page with a challenge."""
        if "/vacancy/" not in url:
            return await served(url, **kwargs)
        asked.append(url)
        raise ChallengeWithAStatusError

    monkeypatch.setattr(hh.http, "get_text", challenge_the_vacancy_pages)

    with pytest.raises(HHChallengedError) as excinfo:
        await collect(hh)

    assert excinfo.value.extra["response_status"] in GONE_STATUSES
    assert asked == [vacancy_url(FULL)], "the walk went on past a host that had stopped it"
