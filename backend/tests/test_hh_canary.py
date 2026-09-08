"""Does hh still serve the state this connector parses? Asked of the live site.

This is the only test in the suite that makes a real request, and it exists
because of what the hh connector is: not a client of a documented API, but a
reader of somebody else's frontend boot state. That state is not a contract.
It will change, without notice, in an ordinary release — and when it does,
``app/sources/hh.py`` stops finding postings while every other test in this
repository stays green, because every one of them is driven by a fixture that
was captured before the change.

The failure it guards against is therefore not "the code is wrong". It is "the
world moved and nothing told us": a dashboard that shows no new hh vacancies,
looks exactly like a quiet week, and is noticed about seven days late.

Deselected by default and never part of the ordinary suite. ``addopts`` in
``pyproject.toml`` carries ``-m "not network"``, so this file does not run on a
laptop or in the pull-request build — deselected rather than skipped, because
``conftest.py`` makes a skip fatal in CI and a test that needs a third party to
be up can never be part of that promise. It runs on a schedule instead:

    uv run pytest -m network

What it asserts is deliberately narrow: the three shapes the connector cannot
work without, checked through the connector's own parsing code rather than
alongside it. It does not assert on any particular vacancy, salary or title —
those change hourly and a canary that cries wolf gets muted, which is the same
outcome as not having one.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import SourceError
from app.sources.hh import (
    SITEMAP_CACHE_TTL,
    VACANCY_ID_ON_PAGE,
    HHSite,
    HHSource,
    catalog_entries,
)
from app.sources.http import SourceClient

#: Network, and slow: three polite requests at the connector's own rate.
pytestmark = [pytest.mark.network, pytest.mark.slow]

#: Pages tried before giving up. The sitemap is a snapshot, so the newest
#: entries in it include postings already taken down; one 404 is normal and
#: three in a row is not.
ATTEMPTS = 3

#: The profession the catalogue is asked about. A common one on purpose: the
#: first measurement of the catalogue used a rare one and concluded from an
#: almost-empty page that catalogue pages do not list vacancies at all.
CANARY_SLUG = "programmist"

#: Vacancy ids that page must still name. It carried 50 on 2026-09-08; the
#: threshold is a third of that, low enough not to cry wolf on a quiet market
#: and high enough that "the page stopped listing vacancies" fails here.
CANARY_IDS = 15


@pytest.fixture
def site() -> HHSite:
    """The host the canary asks about: the default from the shipped file."""
    sites = HHSource().sites
    return next(site for site in sites if site.default)


@pytest.fixture
async def hh() -> AsyncIterator[HHSource]:
    """A connector on a real client — the same path a pipeline run takes.

    Not a bare httpx call: the point is to exercise robots.txt, the rate
    limiter, the User-Agent and the parsing exactly as production does, so that
    a break in any of them fails here rather than in a crawl at three in the
    morning.
    """
    client = SourceClient()
    source = HHSource()
    try:
        yield source.bind(client.bind(source))
    finally:
        await client.aclose()


async def test_the_sitemap_still_lists_dated_vacancy_pages(hh: HHSource, site: HHSite) -> None:
    """The map, and the two fields the incremental crawl is built on.

    Without ``lastmod`` there is no delta and every run is a full re-crawl of
    fourteen thousand pages, so its disappearance is a design failure rather
    than a parsing one.
    """
    files = await hh._vacancy_sitemaps(site)
    assert files, "the sitemap index no longer lists vacancy*.xml"
    assert not [name for name, _ in files if "resume" in name]

    entries = await hh._sitemap_entries(site, files[0][1])

    assert len(entries) > 100, "a city sitemap held 1387 entries when this was written"
    newest = max(entry.lastmod for entry in entries)
    # A week, not a year. The whole incremental design rests on lastmod moving,
    # and the measured file had its newest entries stamped within the hour — so
    # a threshold loose enough to pass on a sitemap frozen last spring reports
    # nothing for the 300-odd daily runs in which the crawl finds no new
    # vacancy, which is exactly the silence this file exists to break.
    assert datetime.now(UTC) - newest < timedelta(days=7), (
        "the newest lastmod in this sitemap is over a week old — it has stopped moving"
    )


async def test_a_live_vacancy_page_still_carries_the_state_we_parse(
    hh: HHSource, site: HHSite
) -> None:
    """The assertion the connector's whole design rests on.

    Both halves matter and neither is enough alone. A missing marker means hh
    renamed or removed the template. A present marker with an empty
    ``vacancyView`` is what a *deleted posting* looks like — hh answers 404 with
    the template still in place — so asserting only that the marker is there
    would pass on a page carrying nothing at all, which is precisely the silent
    failure this file exists to prevent.
    """
    files = await hh._vacancy_sitemaps(site)
    entries = await hh._sitemap_entries(site, files[0][1])
    newest = sorted(entries, key=lambda entry: entry.lastmod, reverse=True)[:ATTEMPTS]

    parsed = []
    for entry in newest:
        try:
            body = await hh.http.get_text(entry.url)
        except SourceError:
            # SourceError, not httpx.HTTPError: the transport turns a 404 into
            # one of ours, and a posting taken down between the sitemap being
            # written and this run reading it is the most ordinary thing that
            # can happen here. Catching the wrong type made a normal Tuesday
            # look like hh having changed, on a job that runs daily.
            continue
        state = hh._state(entry, body)
        if state is not None:
            parsed.append((entry, state[0], state[1]))

    assert parsed, f"none of the {ATTEMPTS} newest postings yielded a vacancyView"
    _, view, dictionary = parsed[0]
    assert view.name.strip(), "a posting with no title is not a posting"
    assert view.vacancy_id > 0
    # The decoding table travels with the page, which is why no hh vocabulary is
    # hardcoded in this repository. If it stops arriving, the labels quietly
    # become blanks in the dashboard.
    assert dictionary.get("workFormats"), "vacancyFieldsDictionary no longer ships with the page"


async def test_a_whole_posting_still_comes_out_of_a_live_page(
    hh: HHSource, site: HHSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end against the live site, bounded to a handful of requests.

    The two tests above check the shapes; this one checks that they still add up
    to something storable — a posting with an id, a URL and a title. No position
    store is installed, so nothing this run does is recorded, and the budget is
    cut so a canary can never turn into a crawl.

    The budget has to clear the planning cost, and that is the thing worth
    knowing: a site's sitemaps are all read before any page is fetched, because
    the newest-first slice is chosen across the whole city at once. Almaty has
    ten files, so eleven requests buy nothing at all — a smaller number here
    fails with "site not reached" and would read as hh having broken.
    """
    monkeypatch.setattr("app.sources.hh.MAX_PAGES_PER_RUN", 20)
    monkeypatch.setattr("app.sources.hh.HEAD_SLICE", 5)

    postings = [posting async for posting in hh.search_batch([])]

    assert postings, "a live walk produced no postings at all"
    first = postings[0]
    assert first.source_slug == "hh"
    assert first.external_id.isdigit()
    assert first.url.startswith(f"https://{site.host}/vacancy/")
    assert "?" not in first.url
    assert first.title.strip()


async def test_the_catalogue_still_lists_vacancies_by_profession(
    hh: HHSource, site: HHSite
) -> None:
    """The other half of the crawl, and the half that fails silently.

    A run reads catalogue pages to decide which postings to fetch first. If hh
    stops publishing them, or stops putting vacancy ids on them, nothing raises:
    the id set comes back empty, the walk falls back to the date order it had
    before, and the corpus goes back to being sales managers — which took a
    scoring pass over 643 vacancies to notice the first time.

    Asked of ``programmist`` deliberately. The first measurement of the catalogue
    opened a rare profession, found almost nothing on it, and concluded the
    catalogue was a dead end; one page of a profession nobody hires for is not a
    measurement of the catalogue.
    """
    files = await hh._catalog_sitemaps(site, lambda: None)
    assert files, "the sitemap index no longer lists vacancies*.xml"

    slugs, locs, lastmods = catalog_entries(
        await hh.http.get_text(files[0][1], cache_ttl=SITEMAP_CACHE_TTL), site.host
    )
    assert len(slugs) > 100, "a catalogue file held 5716 slugs when this was written"
    assert lastmods == 0, "the catalogue has grown dates — it could be walked by freshness now"
    assert locs

    body = await hh.http.get_text(
        f"https://{site.host}/vacancies/{CANARY_SLUG}", cache_ttl=SITEMAP_CACHE_TTL
    )
    ids = set(VACANCY_ID_ON_PAGE.findall(body))

    assert len(ids) >= CANARY_IDS, (
        f"/vacancies/{CANARY_SLUG} named {len(ids)} vacancies; it carried 50 when this was "
        "written, and below this the crawl by profession has quietly stopped working"
    )
