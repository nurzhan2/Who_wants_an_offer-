"""The crawl by profession: the catalogue decides what a short run spends itself on.

The failure this replaces was measured, not imagined. A walk ordered purely by
date returned 294 hh postings for Almaty, none of them in a development role,
with sales managers at the top of the scored list — the connector working
exactly as written and collecting the wrong corpus. hh publishes a page per
profession, ``/vacancies/programmist`` carried 50 vacancy ids when it was
opened on 2026-09-08, and those ids are now fetched first.

What the tests here hold on to is the set of properties that make that a
REORDERING rather than a filter, because every one of them is a way this could
quietly become a worse crawl than the one it replaced:

* nothing the catalogue failed to name is dropped — it is fetched second;
* the position machinery still records everything, so a run interrupted in the
  middle of a scattered walk resumes rather than re-buying;
* a run with no profile behind it does not spend sixteen requests learning that
  it has nothing to select by;
* the plan is stored and rotated, so consecutive runs open different pages
  instead of the same head of the slug list;
* a stale plan is better than none, and a new resume invalidates one at once;
* and a check for robots on a catalogue page stops the run exactly as one on a
  vacancy page does.

The vacancy pages here are minimal on purpose. What hh really puts on one is
tested in ``test_sources_hh.py`` against live captures; these are a vehicle for
the walk, and inventing a rich page would only pin what this file imagined.
"""

import html
import json
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from app.core.config import settings
from app.pipeline.runner import UPSERT_BATCH
from app.sources.base import RawPosting, SearchQuery
from app.sources.hh import (
    CATALOG_HEAD_PAGES,
    CATALOG_PAGES_PER_RUN,
    ROLES_URL,
    CatalogPlan,
    HHSource,
    pages_this_run,
)
from app.sources.http import SourceClient

pytestmark = pytest.mark.unit

HOST = "almaty.hh.kz"
INDEX_URL = f"https://{HOST}/sitemap/main.xml"
VACANCY0_URL = f"https://{HOST}/sitemap/vacancy0.xml"
CATALOG0_URL = f"https://{HOST}/sitemap/vacancies0.xml"
ROBOTS_URL = f"https://{HOST}/robots.txt"
API_ROBOTS_URL = "https://api.hh.ru/robots.txt"

HH_ROBOTS = "User-agent: *\nDisallow: *?*\nDisallow: /resume$\n"

WHEN = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)

#: The keywords a junior Python backend profile produces, which pick the
#: ``backend`` and ``devops`` families out of the shipped config.
KEYWORDS = ("python", "docker")

#: Shaped the way hh nests one, with the collision the live directory has.
DIRECTORY: dict[str, Any] = {
    "categories": [
        {
            "id": "11",
            "name": "Информационные технологии",
            "roles": [
                {"id": "96", "name": "Программист, разработчик"},
                {"id": "160", "name": "DevOps-инженер"},
            ],
        }
    ]
}

#: hh's id for «Программист, разработчик», confirmed against the directory.
PROGRAMMER_ROLE = 96
SALES_ROLE = 70


async def _instant(_seconds: float) -> None:
    """Stand in for ``asyncio.sleep``, so a token bucket costs no wall clock."""
    return None


class StateStore:
    """The pipeline's state store, without a database."""

    def __init__(self) -> None:
        self.saved: dict[str, dict[str, Any]] = {}

    async def load(self, key: str) -> dict[str, Any] | None:
        return self.saved.get(key)

    async def save(self, key: str, value: dict[str, Any]) -> None:
        self.saved[key] = value


def vacancy_page(vacancy_id: str, *, roles: Sequence[int] = ()) -> str:
    """A vacancy page carrying the least state the connector will accept."""
    state = {
        "vacancyView": {
            "vacancyId": int(vacancy_id),
            "name": f"Разработчик {vacancy_id}",
            "status": {"active": True},
            "professionalRoleIds": list(roles),
        },
        "vacancyFieldsDictionary": {},
    }
    return (
        '<html><body><template style="display:none" id="HH-Lux-InitialState">'
        + html.escape(json.dumps(state, ensure_ascii=False))
        + "</template></body></html>"
    )


def sitemap(ids: Sequence[str]) -> str:
    """A vacancy sitemap listing those ids, NEWEST FIRST as written."""
    body = "".join(
        f"<url><loc>https://{HOST}/vacancy/{vacancy_id}</loc>"
        f"<lastmod>{(WHEN - timedelta(minutes=index)).isoformat()}</lastmod></url>"
        for index, vacancy_id in enumerate(ids)
    )
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'


def catalog_sitemap(slugs: Sequence[str]) -> str:
    """A catalogue sitemap: slugs, and no ``lastmod`` on any of them."""
    body = "".join(f"<url><loc>https://{HOST}/vacancies/{slug}</loc></url>" for slug in slugs)
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'


def catalog_page(ids: Sequence[str]) -> str:
    """A catalogue page, as the connector reads one: the ids and nothing else."""
    links = "".join(f'<a href="/vacancy/{vacancy_id}">x</a>' for vacancy_id in ids)
    return f"<html><body>{links}</body></html>"


@pytest.fixture
def http() -> Iterator[respx.MockRouter]:
    """Every outbound request, intercepted before it leaves the process."""
    with respx.mock(assert_all_called=False) as router:
        router.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=HH_ROBOTS))
        router.get(API_ROBOTS_URL).mock(return_value=httpx.Response(404))
        router.get(INDEX_URL).mock(
            return_value=httpx.Response(
                200,
                text=(
                    '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"<sitemap><loc>{VACANCY0_URL}</loc></sitemap>"
                    f"<sitemap><loc>{CATALOG0_URL}</loc></sitemap>"
                    f"<sitemap><loc>https://{HOST}/sitemap/resumes0.xml</loc></sitemap>"
                    "</sitemapindex>"
                ),
            )
        )
        router.get(ROLES_URL).mock(return_value=httpx.Response(200, json=DIRECTORY))
        yield router


@pytest.fixture
def store() -> StateStore:
    """Where the crawl position and the catalogue plan go."""
    return StateStore()


@pytest.fixture
async def hh(monkeypatch: pytest.MonkeyPatch, store: StateStore) -> AsyncIterator[HHSource]:
    """A connector bound to the mocked client, with the disk cache off."""
    monkeypatch.setattr(settings, "http_cache_dir", None)
    client = SourceClient(sleep=_instant)
    source = HHSource()
    try:
        yield source.bind(client.bind(source)).with_state(store.load, store.save)
    finally:
        await client.aclose()


def serve(
    http: respx.MockRouter,
    *,
    vacancies: Sequence[str],
    catalog: dict[str, Sequence[str]],
    roles: dict[str, Sequence[int]] | None = None,
) -> None:
    """One host's whole surface: the sitemap, the catalogue and every page."""
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(vacancies)))
    http.get(CATALOG0_URL).mock(
        return_value=httpx.Response(200, text=catalog_sitemap(list(catalog)))
    )
    for slug, ids in catalog.items():
        http.get(f"https://{HOST}/vacancies/{slug}").mock(
            return_value=httpx.Response(200, text=catalog_page(ids))
        )
    for vacancy_id in vacancies:
        http.get(f"https://{HOST}/vacancy/{vacancy_id}").mock(
            return_value=httpx.Response(
                200, text=vacancy_page(vacancy_id, roles=(roles or {}).get(vacancy_id, ()))
            )
        )


async def collect(
    source: HHSource, keywords: Sequence[str] = KEYWORDS, headline: str | None = None
) -> list[RawPosting]:
    """One walk, drained and confirmed the way ``pipeline/runner.py`` drains it."""
    collected: list[RawPosting] = []
    durable = 0
    query = SearchQuery(keywords=tuple(keywords), headline=headline)
    async for posting in source.search_batch([query]):
        collected.append(posting)
        if len(collected) - durable >= UPSERT_BATCH:
            durable = len(collected)
            await source.record_progress(durable)
    await source.record_progress(len(collected))
    return collected


def walked(postings: Sequence[RawPosting]) -> list[str]:
    """The order the walk actually took."""
    return [posting.external_id for posting in postings]


def _opened(http: respx.MockRouter) -> tuple[str, ...]:
    """The catalogue pages a run opened, in order."""
    return tuple(
        str(call.request.url).rsplit("/", 1)[-1]
        for call in http.calls
        if "/vacancies/" in str(call.request.url)
    )


# -- the reordering ----------------------------------------------------


async def test_the_professions_are_walked_before_everything_newer(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The oldest posting on the page goes first when the catalogue named it.

    This is the whole change in one assertion. The sitemap is ordered by date and
    the walk still is, inside each half; what moves is which half a posting is
    in. A run that hh stops after fifty pages now spends those fifty on the
    professions the profile asked for.
    """
    serve(
        http,
        vacancies=["100", "101", "102", "103"],
        catalog={"programmist": ["103"]},
    )

    postings = await collect(hh)

    assert walked(postings) == ["103", "100", "101", "102"]


async def test_nothing_the_catalogue_missed_is_dropped(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """A filter here would be permanent, and this is not one.

    The walk records how far it got, so a posting skipped for today's keywords
    would be marked as dealt with and never fetched by any future run. Upload a
    new CV and everything the old keyword set rejected stays invisible forever.
    So the catalogue moves postings up the queue and removes none of them.
    """
    serve(http, vacancies=["100", "101", "102"], catalog={"programmist": ["101"]})

    postings = await collect(hh)

    assert set(walked(postings)) == {"100", "101", "102"}


async def test_an_id_the_catalogue_names_that_the_sitemap_does_not_is_left_alone(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The cost of intersecting with the sitemap, paid deliberately.

    A catalogue page gives an id and nothing else; the sitemap gives the date and
    the file that the recorded position is made of. An id with no place to be
    recorded would be re-bought every run forever, so it is not fetched — and the
    run says how many it passed over rather than letting the number hide.
    """
    serve(http, vacancies=["100"], catalog={"programmist": ["100", "999"]})
    http.get(f"https://{HOST}/vacancy/999").mock(return_value=httpx.Response(200, text=""))

    with capture_logs() as logs:
        postings = await collect(hh)

    assert walked(postings) == ["100"]
    planned = next(entry for entry in logs if entry["event"] == "sources.hh.role_pass_planned")
    assert planned["catalog_ids"] == 2
    assert planned["due_now"] == 1


async def test_the_run_reports_how_many_postings_were_in_the_wanted_roles(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The number the whole path exists to move, per run rather than per quarter.

    Before this change a walk of Almaty returned 294 postings with none of them
    in a development role, and the only way to find that out was to score the
    corpus a week later.
    """
    serve(
        http,
        vacancies=["100", "101"],
        catalog={"programmist": ["100"]},
        roles={"100": [PROGRAMMER_ROLE], "101": [SALES_ROLE]},
    )

    with capture_logs() as logs:
        await collect(hh)

    finished = next(entry for entry in logs if entry["event"] == "sources.hh.site_finished")
    assert finished["fetched"] == 2
    assert finished["role_pass"] == 1
    assert finished["role_hits"] == 1


# -- the plan ----------------------------------------------------------


async def test_a_run_without_a_profile_never_opens_the_catalogue(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """Sixteen requests to learn there is nothing to select by is sixteen wasted.

    A crawl with no keywords behind it is the date-ordered walk this connector
    always had, which is the right answer rather than a degraded one.
    """
    serve(http, vacancies=["100"], catalog={"programmist": ["100"]})

    postings = await collect(hh, keywords=())

    assert walked(postings) == ["100"]
    asked = [str(call.request.url) for call in http.calls]
    assert not any("vacancies" in url or "professional_roles" in url for url in asked)


async def test_the_plan_is_stored_and_the_next_run_reuses_it(
    hh: HHSource, http: respx.MockRouter, store: StateStore
) -> None:
    """Professions do not turn over weekly; the postings behind them do.

    Re-resolving costs hh's directory plus every catalogue sitemap, and a run
    that spent those on an unchanged list of professions would be spending them
    instead of on vacancies.
    """
    serve(http, vacancies=["100", "101"], catalog={"programmist": ["100"], "devops-inzhener": []})

    await collect(hh)
    plan = store.saved[f"catalog:{HOST}"]
    before = sum(1 for call in http.calls if CATALOG0_URL in str(call.request.url))

    await collect(hh)

    assert plan["families"] == ["backend", "devops"]
    assert plan["role_ids"] == [PROGRAMMER_ROLE, 160]
    # Ranked, not alphabetical: the shorter, more general page first, and both
    # of them ahead of anything naming a technology this profile never claimed.
    assert plan["slugs"] == ["programmist", "devops-inzhener"]
    after = sum(1 for call in http.calls if CATALOG0_URL in str(call.request.url))
    assert after == before == 1


async def test_a_new_resume_re_resolves_the_plan_at_once(
    hh: HHSource, http: respx.MockRouter, store: StateStore
) -> None:
    """Whatever the refresh interval says.

    The stored plan records which families asked for it, so a profile that
    changed stack does not crawl the previous one's professions for a week.
    """
    serve(http, vacancies=["100"], catalog={"programmist": ["100"]})
    await collect(hh)

    await collect(hh, keywords=("pytest", "selenium"))

    assert store.saved[f"catalog:{HOST}"]["families"] == ["qa"]


async def test_the_best_pages_are_reopened_and_the_rest_are_swept(
    hh: HHSource, http: respx.MockRouter, store: StateStore
) -> None:
    """One page per profession is all hh gives us, so breadth is the only depth.

    Paging exists on the catalogue only as ``?page=0..3``, which hh's
    ``Disallow: *?*`` closes, so the slug list is walked across runs instead. Not
    as a plain rotation, though: the list is ranked by nearness to the profile,
    and a rotation over the whole of it would spend run ten on the 1C pages at
    the bottom. The head is re-read every run — that is how a new python posting
    is found within hours — and the tail is swept behind it.
    """
    slugs = {f"programmist-{index:02d}": [] for index in range(CATALOG_PAGES_PER_RUN + 4)}
    serve(http, vacancies=["100"], catalog=slugs)

    await collect(hh)
    first = _opened(http)
    await collect(hh)
    # Sliced rather than reset: respx keeps every call of the fixture's life, and
    # the second run's pages are the ones after the first run's.
    second = _opened(http)[len(first) :]

    assert len(first) == len(second) == CATALOG_PAGES_PER_RUN
    head = tuple(store.saved[f"catalog:{HOST}"]["slugs"][:CATALOG_HEAD_PAGES])
    assert first[:CATALOG_HEAD_PAGES] == second[:CATALOG_HEAD_PAGES] == head
    assert set(first[CATALOG_HEAD_PAGES:]).isdisjoint(second[CATALOG_HEAD_PAGES:])


def test_a_plan_short_enough_to_read_in_one_run_is_read_whole() -> None:
    """No rotation to do, and no cursor to move."""
    plan = CatalogPlan(resolved_at=WHEN, slugs=("a", "b"), offset=0)

    assert pages_this_run(plan) == (("a", "b"), 0)


def test_the_window_wraps_without_repeating_the_head() -> None:
    """The tail is a ring and the head is not part of it.

    A cursor that ran over the whole list would put the head's pages into the
    rotation as well, and the run would open one of them twice.
    """
    slugs = tuple(f"s{index}" for index in range(CATALOG_HEAD_PAGES + 3))
    take = CATALOG_PAGES_PER_RUN - CATALOG_HEAD_PAGES
    plan = CatalogPlan(resolved_at=WHEN, slugs=slugs, offset=2)

    opened, offset = pages_this_run(plan)

    assert opened[:CATALOG_HEAD_PAGES] == slugs[:CATALOG_HEAD_PAGES]
    assert len(opened) == len(set(opened))
    assert set(opened[CATALOG_HEAD_PAGES:]) <= set(slugs[CATALOG_HEAD_PAGES:])
    assert offset == (2 + min(take, 3)) % 3


async def test_a_catalogue_page_that_is_gone_does_not_stop_the_run(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The sitemap is a snapshot and professions are retired."""
    serve(http, vacancies=["100"], catalog={"programmist": ["100"]})
    http.get(f"https://{HOST}/vacancies/programmist").mock(return_value=httpx.Response(404))

    postings = await collect(hh)

    assert walked(postings) == ["100"]


async def test_the_directory_being_unavailable_leaves_the_keywords_to_choose(
    hh: HHSource, http: respx.MockRouter, store: StateStore
) -> None:
    """A crawl does not stop because a dictionary did.

    Without the directory the families name nothing, so the plan falls back to
    the profile's own words against the slug list — worse than hh's own
    vocabulary, and much better than the date order it would otherwise be.
    """
    serve(http, vacancies=["100"], catalog={"python-razrabotchik": ["100"], "buhgalter": []})
    http.get(ROLES_URL).mock(return_value=httpx.Response(403))

    postings = await collect(hh)

    assert walked(postings) == ["100"]
    assert store.saved[f"catalog:{HOST}"]["slugs"] == ["python-razrabotchik"]


async def test_a_challenge_on_a_catalogue_page_stops_the_run(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """A check for robots is a decision, wherever on the host it arrives.

    Nothing distinguishes a catalogue page from a vacancy page here: hh has
    decided something about this crawler, and the answer is to stop walking it,
    not to try the next twenty pages first.
    """
    serve(http, vacancies=["100"], catalog={"programmist": ["100"]})
    http.get(f"https://{HOST}/vacancies/programmist").mock(
        return_value=httpx.Response(302, headers={"location": f"https://{HOST}/account/captcha"})
    )

    with capture_logs() as logs, pytest.raises(Exception, match="проверкой на робота"):
        await collect(hh)

    assert any(
        entry["event"] == "sources.hh.challenged" and entry.get("stage") == "catalog"
        for entry in logs
    )


# -- the position ------------------------------------------------------


async def test_a_scattered_walk_is_recorded_and_not_re_bought(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The role pass leaves holes in a file ordered by date, and holes are the risk.

    The recorded position used to be capped at a number of stretches that a
    contiguous walk could never exceed; a walk that takes the professions first
    leaves dozens per run. If that cap bit, the oldest coverage would be dropped
    every run and the crawl would pay for the same pages forever — which is
    exactly what an incremental source silently failing looks like.
    """
    serve(
        http,
        vacancies=[str(100 + index) for index in range(12)],
        catalog={"programmist": ["103", "107", "111"]},
    )

    first = await collect(hh)
    second = await collect(hh)

    assert walked(first)[:3] == ["103", "107", "111"]
    assert len(first) == 12
    assert second == []


# -- the ways the plan can fail --------------------------------------


async def test_a_stored_plan_that_no_longer_parses_is_replaced_rather_than_fatal(
    hh: HHSource, http: respx.MockRouter, store: StateStore
) -> None:
    """The cost is one re-resolve; the alternative is a source that needs a DBA.

    Same trade the crawl position makes: a stored value we cannot read is
    treated as no value at all, because the work it describes is idempotent and
    a connector that cannot start until somebody deletes a row by hand is not.
    """
    store.saved[f"catalog:{HOST}"] = {"resolved_at": "not a date", "slugs": 5}
    serve(http, vacancies=["100"], catalog={"programmist": ["100"]})

    with capture_logs() as logs:
        postings = await collect(hh)

    assert walked(postings) == ["100"]
    assert any(entry["event"] == "sources.hh.catalog_plan_unreadable" for entry in logs)
    assert store.saved[f"catalog:{HOST}"]["slugs"] == ["programmist"]


async def test_a_host_with_no_catalogue_falls_back_to_the_date_order(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """hh retiring the file family must not take the source down with it.

    The whole way in by profession rests on a family of sitemap files somebody
    else publishes. If it goes, this connector is the one it was before — which
    collects the wrong corpus, and says so in the log rather than looking fine.
    """
    http.get(INDEX_URL).mock(
        return_value=httpx.Response(
            200,
            text=(
                '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                f"<sitemap><loc>{VACANCY0_URL}</loc></sitemap></sitemapindex>"
            ),
        )
    )
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(["100", "101"])))
    for vacancy_id in ("100", "101"):
        http.get(f"https://{HOST}/vacancy/{vacancy_id}").mock(
            return_value=httpx.Response(200, text=vacancy_page(vacancy_id))
        )

    with capture_logs() as logs:
        postings = await collect(hh)

    assert walked(postings) == ["100", "101"]
    assert any(entry["event"] == "sources.hh.no_catalog_sitemaps" for entry in logs)
    assert any(entry["event"] == "sources.hh.catalog_not_resolved" for entry in logs)


async def test_a_keyword_that_matches_half_the_site_is_capped(
    hh: HHSource, http: respx.MockRouter, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wide is wanted; unbounded is not.

    A profile term is matched against ten thousand slugs, and one that hits
    thousands of them would push the professions hh itself named to the back of
    a rotation that takes twenty pages a run.
    """
    monkeypatch.setattr("app.sources.hh.MAX_CATALOG_SLUGS", 2)
    serve(
        http,
        vacancies=["100"],
        catalog={f"python-{index}": [] for index in range(5)},
    )

    with capture_logs() as logs:
        await collect(hh)

    capped = next(entry for entry in logs if entry["event"] == "sources.hh.catalog_slugs_capped")
    assert capped["matched"] == 5
    assert len(store.saved[f"catalog:{HOST}"]["slugs"]) == 2


# -- intent -------------------------------------------------------------


#: A resume that lists Go beside Python, headed by what it is looking for.
POLYGLOT = ("python", "go", "docker")
HEADLINE = "Python Developer — Backend / AI-интеграции"


async def test_the_headline_decides_which_pages_the_run_opens(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The check the owner asked for: ``opened`` starts with the python page.

    Both languages are on this resume and both catalogue pages belong to the
    same hh role, so nothing in the keyword list can separate them. On the live
    run of 2026-09-08 nothing did: it opened Go, C, JavaScript, Linux and C#
    pages and no Python one.
    """
    serve(
        http,
        vacancies=["100"],
        catalog={"go-razrabotchik": [], "python-razrabotchik": ["100"], "linux-administrator": []},
    )

    with capture_logs() as logs:
        await collect(hh, keywords=POLYGLOT, headline=HEADLINE)

    read = next(entry for entry in logs if entry["event"] == "sources.hh.catalog_read")
    assert read["opened"][0] == "python-razrabotchik"


async def test_without_a_headline_the_same_profile_opens_the_wrong_page(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """The control, so that deleting the weight fails here rather than in a run.

    A crawl that lost the headline would look exactly like this one, and the
    only visible difference would be a corpus of Go vacancies noticed a week
    later by a scoring pass.
    """
    serve(
        http,
        vacancies=["100"],
        catalog={"go-razrabotchik": [], "python-razrabotchik": ["100"]},
    )

    with capture_logs() as logs:
        await collect(hh, keywords=POLYGLOT)

    read = next(entry for entry in logs if entry["event"] == "sources.hh.catalog_read")
    assert read["opened"][0] == "go-razrabotchik"


async def test_a_retitled_resume_re_resolves_the_plan(
    hh: HHSource, http: respx.MockRouter, store: StateStore
) -> None:
    """The headline reorders the whole plan while leaving the families alone.

    Comparing only the families would leave a candidate who retitled themselves
    crawling the previous title's pages until the refresh interval ran out.
    """
    serve(
        http,
        vacancies=["100"],
        catalog={"go-razrabotchik": [], "python-razrabotchik": ["100"]},
    )
    await collect(hh, keywords=POLYGLOT, headline=HEADLINE)

    await collect(hh, keywords=POLYGLOT, headline="Go Developer")

    plan = store.saved[f"catalog:{HOST}"]
    assert plan["headline"] == "Go Developer"
    assert plan["slugs"][0] == "go-razrabotchik"


async def test_the_log_says_which_families_the_headline_named(
    hh: HHSource, http: respx.MockRouter
) -> None:
    """Breadth is kept and the reason for the order is visible.

    ``qa`` and ``frontend`` get in on a listed skill and are crawled; only the
    families the headline named outrank them, and a person reading the log has
    to be able to tell which is which without reading this module.
    """
    serve(http, vacancies=["100"], catalog={"python-razrabotchik": ["100"]})

    with capture_logs() as logs:
        await collect(hh, keywords=("python", "pytest", "react"), headline=HEADLINE)

    families = next(entry for entry in logs if entry["event"] == "sources.hh.role_families")
    assert set(families["families"]) >= {"backend", "qa", "frontend"}
    assert families["focus"] == ["backend"]
