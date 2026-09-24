"""The night crawl: a budget in hours, several cities, and what a captcha costs.

Four things are asserted here and each one is a rule somebody could undo by
accident while making the crawl "better":

* **The pace is 0.25 requests a second and nothing may raise it.** Measured on
  live hh: 1.02 rps met a captcha at the 50th vacancy, 0.73 at the 172nd, 0.25
  walked 961 vacancies over 88 minutes and met none. Going faster collects
  LESS, because the run dies and the recorded position does not move. So this
  file holds the number in the code, holds the absence of any setting that
  could raise it, and holds the observed spacing of a real walk — three places,
  because the first two are a constant somebody can edit and the third is what
  hh actually experiences.
* **The defaults change nothing.** Every value in the ``crawl:`` block is
  absent by default, and a connector reading an empty block has to behave
  exactly as it did before the block existed.
* **An interrupted run keeps what it collected.** The position is written as
  the pipeline confirms each batch, so a run cut at the seventh hour — a
  sleeping laptop, a dropped network — leaves every confirmed posting recorded
  and the next run resumes below them rather than at the top.
* **A pause after a check for robots is a pause and not a way around one.** The
  captcha is never answered, nothing about the client changes, the return is
  slower than the refusal, and a second challenge ends the run.

The clock is fake and the sleeper advances it, so a walk that takes four hours
of hh's time takes milliseconds here — and it takes them THROUGH the real token
bucket, which is what makes the spacing assertion an assertion about the
shipped rate limiter rather than about a number copied into a test.
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
from app.pipeline.runner import UPSERT_BATCH
from app.schemas.crawl import CrawlStop, SavedState
from app.sources.base import RawPosting, SearchQuery
from app.sources.hh import (
    MAX_PAGES_PER_RUN,
    PAUSE_SLOWDOWN,
    POSITION_PREFIX,
    RUN_KEY,
    CrawlSettings,
    FileWatermark,
    HHSource,
    SitemapEntry,
    load_crawl,
)
from app.sources.http import HHChallengedError, SourceClient

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "sources"

ALMATY = "almaty.hh.kz"
ASTANA = "astana.hh.kz"

HH_ROBOTS = "User-agent: *\nAllow: *?u*\nDisallow: *?*\nDisallow: /resume$\n"

#: One captured page, rewritten per id. The subject here is the arithmetic of a
#: long run, not the parsing of a field, and the parsing has its own file.
TEMPLATE = "hh_vacancy_empty_description"

WHEN = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)

#: Requests a walk spends before it reaches its first vacancy page: the sitemap
#: index, then each of the two vacancy files. Named because every budget in this
#: file is "this many pages plus the overhead", and the sum written out is a
#: number somebody has to re-derive on every edit.
OVERHEAD = 3


# -- a clock that only moves when somebody waits ------------------------


class Clock:
    """Monotonic time that advances only when something sleeps.

    The whole point of the night work is a budget measured in hours, and a test
    that measured it against the wall clock could only be written by waiting
    eight hours. Handed to ``SourceClient``, so the connector's deadline, the
    token bucket and the pause after a challenge all read the same fake time —
    and a walk therefore advances this clock at exactly the rate the shipped
    rate limiter imposes on hh.
    """

    def __init__(self) -> None:
        self.now = 0.0
        #: Every wait, in order. The spacing assertion reads this.
        self.waits: list[float] = []

    def __call__(self) -> float:
        """Read the clock, as ``time.monotonic`` would be read."""
        return self.now

    async def sleep(self, seconds: float) -> None:
        """Wait, instantly, by moving time instead."""
        self.waits.append(seconds)
        self.now += seconds


# -- serving hh ---------------------------------------------------------


def _page(payload: dict[str, Any]) -> str:
    """A vacancy page carrying that state, escaped the way hh escapes it."""
    return (
        '<!doctype html><html><body><template style="display:none" id="HH-Lux-InitialState">'
        + html.escape(json.dumps(payload, ensure_ascii=False))
        + "</template></body></html>"
    )


def _sitemap(host: str, ids: Sequence[str]) -> str:
    """A vacancy sitemap listing those ids newest first, one minute apart."""
    body = "".join(
        f"<url><loc>https://{host}/vacancy/{vacancy_id}</loc>"
        f"<lastmod>{(WHEN - timedelta(minutes=index)).isoformat()}</lastmod></url>"
        for index, vacancy_id in enumerate(ids)
    )
    return (
        "<?xml version='1.0' encoding='utf-8'?>"
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + body + "</urlset>"
    )


def ids_for(host: str, count: int) -> list[str]:
    """That many nine-digit ids, distinct per host so two cities never collide."""
    start = 900_000_000 if host == ALMATY else 800_000_000
    return [str(start + index) for index in range(count)]


def serve_host(http: respx.MockRouter, host: str, ids: Sequence[str]) -> dict[str, respx.Route]:
    """robots.txt, the index, the two vacancy files and every page of them."""
    http.get(f"https://{host}/robots.txt").mock(return_value=httpx.Response(200, text=HH_ROBOTS))
    http.get(f"https://{host}/sitemap/main.xml").mock(
        return_value=httpx.Response(
            200,
            text=(FIXTURES / "hh_sitemap_index.xml")
            .read_text(encoding="utf-8")
            .replace(ALMATY, host),
        )
    )
    http.get(f"https://{host}/sitemap/vacancy1.xml").mock(
        return_value=httpx.Response(200, text=_sitemap(host, []))
    )
    http.get(f"https://{host}/sitemap/vacancy0.xml").mock(
        return_value=httpx.Response(200, text=_sitemap(host, ids))
    )
    payload: dict[str, Any] = json.loads(
        (FIXTURES / f"{TEMPLATE}.json").read_text(encoding="utf-8")
    )
    routes: dict[str, respx.Route] = {}
    for vacancy_id in ids:
        payload["vacancyView"]["vacancyId"] = int(vacancy_id)
        routes[vacancy_id] = http.get(f"https://{host}/vacancy/{vacancy_id}").mock(
            return_value=httpx.Response(200, text=_page(payload))
        )
    return routes


def challenge() -> HHChallengedError:
    """hh's refusal, built the way ``refuse_challenge`` builds it.

    A redirect into ``/account/captcha``, which is the one form of it that has
    ever been observed — see docs/SOURCES.md — and the reason this is a helper
    rather than a literal is that the fields are the ones the report prints.
    """
    return HHChallengedError(
        "hh: источник ответил проверкой на робота",
        host=ALMATY,
        path="/account/captcha",
        source_slug="hh",
    )


class StateStore:
    """The crawl-position store, without a database."""

    def __init__(self) -> None:
        self.saved: dict[str, dict[str, Any]] = {}

    @property
    def positions(self) -> dict[str, dict[str, Any]]:
        """Only the rows that are claims about coverage."""
        return {key: value for key, value in self.saved.items() if key.startswith(POSITION_PREFIX)}

    def rows(self) -> list[SavedState]:
        """What ``describe_last_run`` is handed by the repository."""
        return [
            SavedState(key=key, value=value, updated_at=datetime.now(UTC))
            for key, value in self.saved.items()
        ]

    async def load(self, key: str) -> dict[str, Any] | None:
        """Answer with what was stored."""
        return self.saved.get(key)

    async def save(self, key: str, value: dict[str, Any]) -> None:
        """Store it, replacing whatever was there."""
        self.saved[key] = value


def write_sites(path: Path, hosts: Sequence[str], crawl: str = "") -> Path:
    """A ``hh_sites.yaml`` naming those hosts, with that ``crawl:`` block."""
    body = "sites:\n"
    for host in hosts:
        city = "Алматы" if host == ALMATY else "Астана"
        body += f"  - host: {host}\n    city: {city}\n    country: KZ\n    default: true\n"
        body += f"    aliases: [{city.lower()}]\n"
    sites = path / "hh_sites.yaml"
    sites.write_text(body + crawl, encoding="utf-8")
    return sites


# -- fixtures -----------------------------------------------------------


@pytest.fixture
def clock() -> Clock:
    """Time that only moves when the crawl waits."""
    return Clock()


@pytest.fixture
def http() -> Iterator[respx.MockRouter]:
    """Every outbound request, intercepted before it leaves the process."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> AsyncIterator[SourceClient]:
    """The shared client, reading the fake clock and waiting on the fake sleeper."""
    monkeypatch.setattr(settings, "http_cache_dir", None)
    source_client = SourceClient(clock=clock, sleep=clock.sleep)
    try:
        yield source_client
    finally:
        await source_client.aclose()


@pytest.fixture
def store() -> StateStore:
    """Where this run's crawl position and run summary go."""
    return StateStore()


def build(client: SourceClient, store: StateStore) -> HHSource:
    """A connector bound to the mocked client and the store."""
    source = HHSource()
    return source.bind(client.bind(source)).with_state(store.load, store.save)


async def collect(source: HHSource) -> list[RawPosting]:
    """Everything one walk yields, confirmed the way ``pipeline/runner`` confirms it."""
    collected: list[RawPosting] = []
    durable = 0
    async for posting in source.search_batch([SearchQuery()]):
        collected.append(posting)
        if len(collected) - durable >= UPSERT_BATCH:
            durable = len(collected)
            await source.record_progress(durable)
    await source.record_progress(len(collected))
    return collected


async def collect_until_raised(
    source: HHSource,
) -> tuple[list[RawPosting], BaseException | None]:
    """A walk that ends in an exception, rescued the way the pipeline rescues one."""
    collected: list[RawPosting] = []
    durable = 0
    raised: BaseException | None = None
    try:
        async for posting in source.search_batch([SearchQuery()]):
            collected.append(posting)
            if len(collected) - durable >= UPSERT_BATCH:
                durable = len(collected)
                await source.record_progress(durable)
    except Exception as exc:
        raised = exc
    await source.record_progress(len(collected))
    return collected, raised


def covered(store: StateStore, host: str, ids: Sequence[str]) -> set[str]:
    """Which of those ids the stored position actually covers."""
    saved = store.positions.get(f"{POSITION_PREFIX}{host}:vacancy0")
    if saved is None:
        return set()
    mark = FileWatermark.model_validate(saved)
    return {
        vacancy_id
        for index, vacancy_id in enumerate(ids)
        if mark.is_done(
            SitemapEntry(
                external_id=vacancy_id,
                url=f"https://{host}/vacancy/{vacancy_id}",
                lastmod=WHEN - timedelta(minutes=index),
            )
        )
    }


# -- the pace, which is the constraint the whole task sits under --------


def test_the_declared_pace_is_the_one_that_was_measured() -> None:
    """0.25 requests a second, burst one, with jitter. All three matter.

    Not a tautology dressed as a test. Each of the three is a separate way to
    go faster, and two of them look harmless: a burst of two is two requests in
    the same instant however low the steady rate, and dropping the jitter turns
    a polite crawl into a metronome that spaces every request identically to the
    millisecond.

    The numbers come from live runs against almaty.hh.kz and they are the whole
    argument of ``prompts/20-night-crawl.md``: 1.02 rps was refused at the 50th
    vacancy, 0.73 at the 172nd, 0.25 walked 961 over 88 minutes untouched.
    """
    limit = HHSource.rate_limit
    assert limit.requests_per_second == 0.25, (
        "hh's tolerance for this crawler was measured, not chosen. Raising this "
        "collects fewer vacancies, not more: the run is stopped and the position "
        "does not advance. To collect more, crawl for longer — crawl.minutes."
    )
    assert limit.burst == 1
    assert limit.jitter_seconds == 1.0


def test_nothing_in_the_crawl_config_can_raise_the_pace() -> None:
    """The night is configurable; the pace is not, and there is no field for it.

    The other half of the rule above. A constant somebody has to edit in a
    reviewed file is one thing; a key in a YAML file that a tired owner can set
    at two in the morning is another, and this asserts that key does not exist.
    """
    named = set(CrawlSettings.model_fields)
    assert named == {"minutes", "pages", "pause_minutes", "cities"}
    assert not any(
        word in field for field in named for word in ("rate", "rps", "second", "delay", "fast")
    )


async def test_a_real_walk_spaces_its_requests_by_four_seconds_at_least(
    client: SourceClient, http: respx.MockRouter, store: StateStore, clock: Clock, tmp_path: Path
) -> None:
    """The pace as hh experiences it, not as a constant says it.

    Through the shipped ``TokenBucket`` and the shipped ``RateLimit``: a walk of
    twelve pages must take at least four seconds per request end to end. This is
    the assertion that survives somebody "optimising" the bucket rather than the
    number, which is the edit a constant check cannot see.
    """
    monkey = pytest.MonkeyPatch()
    monkey.setattr("app.sources.hh.SITES_FILE", write_sites(tmp_path, [ALMATY]))
    try:
        ids = ids_for(ALMATY, 12)
        serve_host(http, ALMATY, ids)
        source = build(client, store)

        postings = await collect(source)

        assert len(postings) == 12
        requests = len(ids) + OVERHEAD
        # The floor, not the mean: the bucket's interval is 4s plus 0-1s of
        # jitter, so the average lands near 4.5 and only the floor is exact.
        assert clock.now / requests >= 4.0, (
            f"{requests} requests in {clock.now:.1f}s is faster than the declared 0.25 rps"
        )
    finally:
        monkey.undo()


# -- the budget: pages by default, hours when asked ---------------------


def test_an_absent_crawl_block_asks_for_nothing(tmp_path: Path) -> None:
    """The compatibility promise, read straight off the file.

    A ``hh_sites.yaml`` with no ``crawl:`` block at all — which is every
    deployment that existed before this work — must parse to a settings object
    that changes nothing: no deadline, no page override, no pause, no rotation.
    """
    settings_ = load_crawl(write_sites(tmp_path, [ALMATY]))

    assert settings_.minutes is None
    assert settings_.pages is None
    assert settings_.pause_minutes is None
    assert settings_.cities == ()


async def test_the_default_run_is_still_bounded_by_pages_and_not_by_a_clock(
    client: SourceClient, http: respx.MockRouter, store: StateStore, clock: Clock, tmp_path: Path
) -> None:
    """With no ``minutes``, a run stops where it always stopped: the page budget.

    Proved by letting simulated time run far past any night — twelve pages at
    the declared pace is nearly a minute, and the budget here is four — and
    checking that what stopped the walk was the count.
    """
    monkey = pytest.MonkeyPatch()
    monkey.setattr("app.sources.hh.SITES_FILE", write_sites(tmp_path, [ALMATY]))
    monkey.setattr("app.sources.hh.MAX_PAGES_PER_RUN", OVERHEAD + 4)
    try:
        ids = ids_for(ALMATY, 12)
        serve_host(http, ALMATY, ids)
        source = build(client, store)

        postings = await collect(source)

        assert len(postings) == 4
        summary = source.describe_last_run(store.rows())
        assert summary is not None
        assert summary.stopped_by is CrawlStop.PAGES
        assert summary.minutes is None
    finally:
        monkey.undo()


async def test_a_timed_run_stops_when_the_night_ends_rather_than_on_a_page_count(
    client: SourceClient, http: respx.MockRouter, store: StateStore, tmp_path: Path
) -> None:
    """The night budget, which is the point of the whole change.

    ``minutes`` is short and ``pages`` is far above what the time can buy, so
    the only thing that can stop this walk is the clock. At the declared pace a
    minute buys about thirteen pages, so two minutes must stop it well short of
    the forty pages on offer — and the summary must say it was the time.
    """
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        "app.sources.hh.SITES_FILE",
        write_sites(tmp_path, [ALMATY], "crawl:\n  minutes: 2\n  pages: 400\n"),
    )
    try:
        ids = ids_for(ALMATY, 40)
        serve_host(http, ALMATY, ids)
        source = build(client, store)

        postings = await collect(source)

        # Two minutes at one request per four-to-five seconds, less the three
        # requests of overhead: somewhere in the twenties, and nowhere near 40.
        assert 15 <= len(postings) < 30, len(postings)
        summary = source.describe_last_run(store.rows())
        assert summary is not None
        assert summary.stopped_by is CrawlStop.TIME
        assert summary.minutes == 2
    finally:
        monkey.undo()


async def test_a_page_budget_that_would_bite_before_the_night_is_announced(
    client: SourceClient, http: respx.MockRouter, store: StateStore, tmp_path: Path
) -> None:
    """Setting ``minutes`` and leaving ``pages`` alone is the obvious mistake.

    Eight hours can reach about 7200 pages and the default budget is 1200, so an
    owner who sets only ``minutes`` gets ninety minutes of crawling and a report
    that says the night went fine. That must be visible from the outside before
    the night is spent, not derived from the numbers afterwards.
    """
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        "app.sources.hh.SITES_FILE",
        write_sites(tmp_path, [ALMATY], "crawl:\n  minutes: 480\n"),
    )
    try:
        serve_host(http, ALMATY, ids_for(ALMATY, 4))
        source = build(client, store)

        with capture_logs() as logs:
            await collect(source)

        warned = [line for line in logs if line["event"] == "sources.hh.page_budget_binds"]
        assert warned, "an owner who sets only crawl.minutes must be told pages will stop them"
        assert warned[0]["pages"] == MAX_PAGES_PER_RUN
        assert warned[0]["reachable"] > MAX_PAGES_PER_RUN
    finally:
        monkey.undo()


# -- an interrupted run keeps what it collected -------------------------


async def test_a_run_cut_at_the_seventh_hour_keeps_every_posting_it_confirmed(
    client: SourceClient, http: respx.MockRouter, store: StateStore, tmp_path: Path
) -> None:
    """The sleeping laptop, the closed lid, the dropped network.

    Modelled as the thing all three look like from in here: a request that
    fails partway through a long walk. What the run had confirmed must be in the
    position, the posting it died on must not be, and the next run must continue
    below the recorded stretch rather than at the top of the corpus.

    The mechanism is not new — it is ``record_progress`` — and that is exactly
    why it is pinned here: the night work made runs long enough that losing it
    would cost hours rather than minutes, and nothing about a long run had ever
    been exercised.
    """
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        "app.sources.hh.SITES_FILE",
        write_sites(tmp_path, [ALMATY], "crawl:\n  minutes: 480\n  pages: 400\n"),
    )
    try:
        ids = ids_for(ALMATY, 30)
        routes = serve_host(http, ALMATY, ids)
        # The network goes away at the eleventh page, the way a lid closing
        # looks to a client that is mid-request.
        routes[ids[10]].mock(side_effect=httpx.ConnectError("network gone"))
        source = build(client, store)

        first, raised = await collect_until_raised(source)

        assert raised is not None
        assert len(first) == 10
        assert covered(store, ALMATY, ids) == set(ids[:10]), (
            "a run interrupted at the seventh hour must leave everything it had confirmed"
        )
        # And it must say so. A night that fell over and a night that never
        # started look identical at breakfast unless the summary distinguishes
        # them, and the summary is the only thing left by then.
        summary = source.describe_last_run(store.rows())
        assert summary is not None
        assert summary.stopped_by is CrawlStop.INTERRUPTED
        assert summary.cities[0].fetched == 11, "the page it died on was still bought"
        assert summary.cities[0].stored == 10

        # The page that failed is served again, and a second run continues.
        routes[ids[10]] = http.get(f"https://{ALMATY}/vacancy/{ids[10]}").mock(
            return_value=httpx.Response(200, text=_page(_state_for(ids[10])))
        )
        second = await collect(build(client, store))

        assert [posting.external_id for posting in second][:3] == ids[10:13], (
            "the second run must continue below the first, not start again at the top"
        )
    finally:
        monkey.undo()


def _state_for(vacancy_id: str) -> dict[str, Any]:
    """The template page, answering for that id."""
    payload: dict[str, Any] = json.loads(
        (FIXTURES / f"{TEMPLATE}.json").read_text(encoding="utf-8")
    )
    payload["vacancyView"]["vacancyId"] = int(vacancy_id)
    return payload


async def test_a_run_the_clock_cuts_records_what_it_walked(
    client: SourceClient, http: respx.MockRouter, store: StateStore, tmp_path: Path
) -> None:
    """Running out of night is an ending, not a failure, and it records too.

    The deadline stops the walk inside a file rather than at a file boundary,
    which is the case a frontier-shaped position could not describe. What was
    confirmed has to be in the stretch and nothing beyond it.
    """
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        "app.sources.hh.SITES_FILE",
        write_sites(tmp_path, [ALMATY], "crawl:\n  minutes: 2\n  pages: 400\n"),
    )
    try:
        ids = ids_for(ALMATY, 40)
        serve_host(http, ALMATY, ids)
        source = build(client, store)

        postings = await collect(source)

        walked = [posting.external_id for posting in postings]
        assert walked, "a two-minute run still buys pages"
        assert covered(store, ALMATY, ids) == set(walked)
        assert len(walked) < len(ids), "the clock must have cut this run short"
    finally:
        monkey.undo()


# -- cities ------------------------------------------------------------


async def test_the_budget_is_split_between_cities_in_the_shares_the_file_gives(
    client: SourceClient, http: respx.MockRouter, store: StateStore, tmp_path: Path
) -> None:
    """Three to one is three to one, in pages.

    Equal shares were the old behaviour and they are wrong for this owner: they
    live in Almaty, whose corpus is some 14 000 pages, and Karaganda's is a
    fraction of that. The weights are what lets a night be mostly Almaty without
    the other cities being starved for a fortnight.
    """
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        "app.sources.hh.SITES_FILE",
        write_sites(
            tmp_path,
            [ALMATY, ASTANA],
            "crawl:\n"
            "  cities:\n"
            f"    - {{host: {ALMATY}, share: 3}}\n"
            f"    - {{host: {ASTANA}, share: 1}}\n",
        ),
    )
    # Overhead is per host, so sixteen pages of budget leaves ten for vacancies.
    monkey.setattr("app.sources.hh.MAX_PAGES_PER_RUN", 16)
    try:
        almaty = ids_for(ALMATY, 20)
        astana = ids_for(ASTANA, 20)
        serve_host(http, ALMATY, almaty)
        serve_host(http, ASTANA, astana)
        source = build(client, store)

        postings = await collect(source)

        from_almaty = [p for p in postings if f"//{ALMATY}/" in p.url]
        from_astana = [p for p in postings if f"//{ASTANA}/" in p.url]
        assert from_almaty, "the first city must be walked"
        assert from_astana, "a share of one is still a share"
        assert len(from_almaty) > len(from_astana), (
            f"3:1 gave Almaty {len(from_almaty)} and Astana {len(from_astana)}"
        )
    finally:
        monkey.undo()


async def test_the_rotation_decides_the_cities_and_the_resume_does_not(
    client: SourceClient, http: respx.MockRouter, store: StateStore, tmp_path: Path
) -> None:
    """An explicit list replaces the area match, and says so in the log.

    This is the change that makes several cities per run possible at all: the
    resume names Almaty, so without it the other cities are never opened however
    many nights the machine runs. It is not allowed to be quiet about it — an
    owner in Karaganda whose city the rotation omits has to be able to find out
    why from outside the process.
    """
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        "app.sources.hh.SITES_FILE",
        write_sites(tmp_path, [ALMATY, ASTANA], f"crawl:\n  cities:\n    - {{host: {ASTANA}}}\n"),
    )
    try:
        source = build(client, store)

        with capture_logs() as logs:
            chosen = source.sites_for([SearchQuery(area="Алматы")])

        assert [site.host for site in chosen] == [ASTANA]
        assert [line for line in logs if line["event"] == "sources.hh.area_outside_rotation"], (
            "a plan naming a city the rotation leaves out must be visible from outside"
        )
    finally:
        monkey.undo()


def test_a_rotation_naming_a_city_the_file_does_not_describe_is_refused(
    tmp_path: Path,
) -> None:
    """A misspelled host is a typo, and a typo must not cost a night.

    Dropped silently, it produces a run that reports success and covers three
    quarters of what was asked for — and nothing in the report would say which
    quarter was missing, because a city that was never planned leaves no row.
    """
    sites = write_sites(tmp_path, [ALMATY], "crawl:\n  cities:\n    - {host: almaty.hh.ru}\n")

    with pytest.raises(SourceError, match=r"almaty\.hh\.ru"):
        load_crawl(sites, sites=[])


async def test_the_confirmation_counter_counts_the_run_and_not_one_city(
    client: SourceClient, http: respx.MockRouter, store: StateStore, tmp_path: Path
) -> None:
    """``_Held.after`` must mean what ``record_progress`` is handed.

    The pipeline confirms with a count of the whole stream. The connector used
    to hold each entry against ``_SiteRun.stored``, which restarts at one when
    the walk moves to the second city — the same number meaning two different
    things. With one city they coincide, which is why it went unnoticed: a
    second city was configurable and nobody had configured one, and the night
    rotation makes several the ordinary case.

    **This is a contract test, not a regression test, and the difference is
    worth being exact about.** No posting was ever lost to it, because an entry
    is held only after the yield that produced it — so the confirmation that
    could have covered it early has already gone by, and the next one arrives
    with everything written. That is a property of the pipeline's loop, and
    ``BaseSource.record_progress`` says the connector may not reason about what
    the caller counts. So this asserts the invariant directly, on the held
    entries themselves, rather than staging a data loss that the caller's
    ordering currently prevents.
    """
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        "app.sources.hh.SITES_FILE",
        write_sites(
            tmp_path,
            [ALMATY, ASTANA],
            f"crawl:\n  cities:\n    - {{host: {ALMATY}}}\n    - {{host: {ASTANA}}}\n",
        ),
    )
    try:
        almaty = ids_for(ALMATY, 4)
        astana = ids_for(ASTANA, 4)
        serve_host(http, ALMATY, almaty)
        serve_host(http, ASTANA, astana)
        source = build(client, store)

        # Drained without confirming anything, so every entry the walk finished
        # with is still waiting and can be read.
        seen = [posting async for posting in source.search_batch([SearchQuery()])]

        assert len(seen) == len(almaty) + len(astana)
        held = [entry.after for entry in source._held]
        assert held == sorted(held), "the count must never go backwards between cities"
        assert held == list(range(1, len(seen) + 1)), (
            f"held against {held}; it has to be the run's posting count, which is "
            "what record_progress is handed — not each city's own tally"
        )
    finally:
        monkey.undo()


# -- a check for robots, and what waiting it out may and may not be -----


async def test_without_a_configured_pause_a_challenge_still_ends_the_run(
    client: SourceClient, http: respx.MockRouter, store: StateStore, tmp_path: Path
) -> None:
    """The default is unchanged, and the default is to stop.

    Everything about the pause is opt-in. A deployment that never opens
    ``hh_sites.yaml`` behaves exactly as this connector has behaved since the
    first live captcha: the run ends, the position stands, the pipeline records
    a challenge, and a person decides what to do about it.
    """
    monkey = pytest.MonkeyPatch()
    monkey.setattr("app.sources.hh.SITES_FILE", write_sites(tmp_path, [ALMATY]))
    try:
        ids = ids_for(ALMATY, 10)
        routes = serve_host(http, ALMATY, ids)
        routes[ids[3]].mock(side_effect=challenge())
        source = build(client, store)

        postings, raised = await collect_until_raised(source)

        assert isinstance(raised, HHChallengedError)
        assert len(postings) == 3
        assert covered(store, ALMATY, ids) == set(ids[:3])
        summary = source.describe_last_run(store.rows())
        assert summary is not None
        assert summary.stopped_by is CrawlStop.CHALLENGE
        assert [check.resumed for check in summary.challenges] == [False]
    finally:
        monkey.undo()


async def test_a_configured_pause_waits_returns_slower_and_never_twice(
    client: SourceClient, http: respx.MockRouter, store: StateStore, clock: Clock, tmp_path: Path
) -> None:
    """The whole of what a pause is allowed to be.

    One return per run, at a lower rate than the refusal, and a second challenge
    ends the run for good — because a second refusal at half the pressure is no
    longer hh describing a rate, it is hh describing us.

    The captcha is never fetched and never answered: the transport refuses
    ``/account/*`` outright, so the only thing this test can assert about the
    challenge is that nothing was sent in reply to it, which is what the request
    log below says.
    """
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        "app.sources.hh.SITES_FILE",
        write_sites(
            tmp_path, [ALMATY], "crawl:\n  minutes: 600\n  pages: 400\n  pause_minutes: 30\n"
        ),
    )
    try:
        ids = ids_for(ALMATY, 20)
        routes = serve_host(http, ALMATY, ids)
        refusal = challenge()
        routes[ids[3]].mock(side_effect=refusal)
        routes[ids[9]].mock(side_effect=refusal)
        source = build(client, store)
        before = clock.now

        postings, raised = await collect_until_raised(source)

        assert isinstance(raised, HHChallengedError), "the second challenge must end the run"
        # The wait itself: half an hour of simulated time that bought no pages.
        assert clock.now - before > 30 * 60
        # It came back and kept walking, which is the point of pausing at all.
        assert len(postings) > 3, "the run must have resumed past the first challenge"

        summary = source.describe_last_run(store.rows())
        assert summary is not None
        assert [check.resumed for check in summary.challenges] == [True, False]
        assert summary.paused_seconds == pytest.approx(30 * 60)
        assert summary.stopped_by is CrawlStop.CHALLENGE

        # The return is SLOWER than the refusal, which is the third of the
        # three lines a pause must not cross. Read off the waits the token
        # bucket actually took rather than off the constant that sets them:
        # ``slow_to`` can only lower a rate, so a mistake here looks like a
        # no-op and a constant check would pass through it.
        cut = clock.waits.index(max(clock.waits))
        waits_before = [wait for wait in clock.waits[:cut] if wait > 1.0]
        waits_after = [wait for wait in clock.waits[cut + 1 :] if wait > 1.0]
        assert waits_before and waits_after, (
            "the run must have fetched pages on both sides of the pause"
        )
        assert min(waits_after) >= 2 * max(waits_before) - 1, (
            f"came back at {min(waits_after):.1f}s a page after crawling at "
            f"{max(waits_before):.1f}s; hh has just refused this crawler and the "
            "return must be gentler, never equal"
        )

        # Nothing was sent to the challenge, and nothing on the crawler changed.
        asked = [str(call.request.url) for call in http.calls]
        assert not any("/account" in url for url in asked)
        assert not any("captcha" in url.lower() for url in asked)
        agents = {call.request.headers.get("user-agent") for call in http.calls}
        assert len(agents) == 1, "the pause must not change how this crawler identifies itself"
        assert not any(call.request.headers.get("cookie") for call in http.calls)
    finally:
        monkey.undo()


async def test_a_run_that_came_back_from_a_captcha_is_not_reported_as_stopped_by_one(
    client: SourceClient, http: respx.MockRouter, store: StateStore, tmp_path: Path
) -> None:
    """Recovering from a challenge is a recovery, and the ending says so.

    The challenge stays on the list — it is the thing the owner most wants to
    see — but it is not what ended the run, and saying it was would send them to
    change a setting that worked. Here the walk is challenged once, waits, comes
    back and finishes the whole file, so the ending is "nothing left to crawl".
    """
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        "app.sources.hh.SITES_FILE",
        write_sites(
            tmp_path, [ALMATY], "crawl:\n  minutes: 600\n  pages: 400\n  pause_minutes: 30\n"
        ),
    )
    try:
        ids = ids_for(ALMATY, 8)
        routes = serve_host(http, ALMATY, ids)
        # Refused once, served on the retry — which is what a rate-triggered
        # check looks like from here: the page was never the problem.
        routes[ids[2]].mock(
            side_effect=[challenge(), httpx.Response(200, text=_page(_state_for(ids[2])))]
        )
        source = build(client, store)

        postings, raised = await collect_until_raised(source)

        assert raised is None, "one challenge with a pause configured must not end the run"
        summary = source.describe_last_run(store.rows())
        assert summary is not None
        assert summary.stopped_by is CrawlStop.CORPUS, (
            "the run came back and finished; the captcha is not what ended it"
        )
        assert [check.resumed for check in summary.challenges] == [True]
        assert summary.cities[0].finished is True
        # The page hh refused was never recorded, so the resumed walk buys it
        # again: it is simply due, like any entry nobody has covered.
        assert ids[2] in {posting.external_id for posting in postings}
    finally:
        monkey.undo()


async def test_the_pause_is_refused_when_the_night_is_nearly_over(
    client: SourceClient, http: respx.MockRouter, store: StateStore, tmp_path: Path
) -> None:
    """A wait that does not fit is a run that sleeps through its own deadline.

    Thirty minutes of pause needs an hour of night left. With two minutes on the
    clock the challenge ends the run exactly as it would with no pause
    configured — and the summary says it was not resumed, so the morning report
    does not claim a recovery that never happened.
    """
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        "app.sources.hh.SITES_FILE",
        write_sites(
            tmp_path, [ALMATY], "crawl:\n  minutes: 2\n  pages: 400\n  pause_minutes: 30\n"
        ),
    )
    try:
        ids = ids_for(ALMATY, 20)
        routes = serve_host(http, ALMATY, ids)
        routes[ids[2]].mock(side_effect=challenge())
        source = build(client, store)

        _, raised = await collect_until_raised(source)

        assert isinstance(raised, HHChallengedError)
        summary = source.describe_last_run(store.rows())
        assert summary is not None
        assert [check.resumed for check in summary.challenges] == [False]
        assert summary.paused_seconds == 0.0
    finally:
        monkey.undo()


async def test_a_pause_without_a_night_to_bound_it_is_refused(
    client: SourceClient, http: respx.MockRouter, store: StateStore, tmp_path: Path
) -> None:
    """``pause_minutes`` without ``minutes`` does nothing, deliberately.

    A pause with no deadline behind it has nothing to say when to give up, and a
    crawler that sits on a host which refused it for as long as the process
    lives is not being polite — it is insisting more slowly.
    """
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        "app.sources.hh.SITES_FILE",
        write_sites(tmp_path, [ALMATY], "crawl:\n  pause_minutes: 30\n"),
    )
    try:
        ids = ids_for(ALMATY, 10)
        routes = serve_host(http, ALMATY, ids)
        routes[ids[2]].mock(side_effect=challenge())
        source = build(client, store)

        _, raised = await collect_until_raised(source)

        assert isinstance(raised, HHChallengedError)
        summary = source.describe_last_run(store.rows())
        assert summary is not None
        assert summary.paused_seconds == 0.0
    finally:
        monkey.undo()


def test_the_return_after_a_pause_is_slower_and_can_only_be_slower() -> None:
    """``slow_to`` is a one-way door, and that is what makes it safe to expose.

    The multiplier is checked here too: the return has to be slower than the
    rate that was refused, and a value below one would silently make the pause
    a speed-up, which is the exact shape of the thing CLAUDE.md forbids.
    """
    assert PAUSE_SLOWDOWN > 1.0


# -- the morning after --------------------------------------------------


async def test_the_report_names_every_city_including_the_one_that_was_stopped(
    client: SourceClient, http: respx.MockRouter, store: StateStore, tmp_path: Path
) -> None:
    """The summary is read at breakfast, and the interesting city is the cut one.

    A city stopped by a check for robots never reaches ``_log_site``, so without
    the ``finally`` in ``_crawl_site`` it is precisely the city missing from the
    report. The city that was never reached must be there too, as a row of
    zeros: "we did not get there" is the answer to «где остановились», and a
    missing row is not an answer.
    """
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        "app.sources.hh.SITES_FILE",
        write_sites(
            tmp_path,
            [ALMATY, ASTANA],
            f"crawl:\n  cities:\n    - {{host: {ALMATY}}}\n    - {{host: {ASTANA}}}\n",
        ),
    )
    try:
        almaty = ids_for(ALMATY, 10)
        routes = serve_host(http, ALMATY, almaty)
        serve_host(http, ASTANA, ids_for(ASTANA, 10))
        routes[almaty[2]].mock(side_effect=challenge())
        source = build(client, store)

        await collect_until_raised(source)

        summary = source.describe_last_run(store.rows())
        assert summary is not None
        rows = {city.scope: city for city in summary.cities}
        assert set(rows) == {ALMATY, ASTANA}
        assert rows[ALMATY].fetched == 3, "the challenged city must carry what it did buy"
        assert rows[ALMATY].stored == 2
        assert rows[ALMATY].finished is False
        assert rows[ASTANA].fetched == 0, "a city the run never reached is a row of zeros"
        assert rows[ASTANA].finished is False
        assert summary.challenges[0].scope == ALMATY
        assert summary.challenges[0].after_pages == 3
    finally:
        monkey.undo()


async def test_a_finished_run_reports_what_it_bought_and_what_is_left(
    client: SourceClient, http: respx.MockRouter, store: StateStore, tmp_path: Path
) -> None:
    """The ordinary morning: numbers per city, and a corpus figure beside them.

    ``outstanding`` is the number the eight-hour projection is read against, so
    it has to be the file's own count at the moment the run read it rather than
    something derived afterwards from what was fetched.
    """
    monkey = pytest.MonkeyPatch()
    monkey.setattr("app.sources.hh.SITES_FILE", write_sites(tmp_path, [ALMATY]))
    monkey.setattr("app.sources.hh.MAX_PAGES_PER_RUN", OVERHEAD + 5)
    try:
        ids = ids_for(ALMATY, 20)
        serve_host(http, ALMATY, ids)
        source = build(client, store)

        await collect(source)

        summary = source.describe_last_run(store.rows())
        assert summary is not None
        assert summary.fetched == 5
        assert summary.stored == 5
        assert summary.outstanding == 20, "20 entries were outstanding when the file was read"
        assert summary.finished_at >= summary.started_at
        assert RUN_KEY in store.saved
    finally:
        monkey.undo()


async def test_a_source_that_keeps_no_run_summary_says_so_rather_than_guessing(
    client: SourceClient, store: StateStore
) -> None:
    """The default on ``BaseSource``, which every other connector uses.

    A bounded feed fetched in twenty seconds has no night to report on, and the
    report has to be able to tell "nothing to say" from "nothing happened".
    """
    source = build(client, store)

    assert source.describe_last_run([]) is None
