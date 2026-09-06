"""The one HTTP client every connector goes through: the ceiling, the guards, the cache.

Nothing here reaches the network. Every request is answered by ``respx``, and
every second that passes is a number a fake clock holds, so the file proves a
rate ceiling in milliseconds instead of spending the seconds it is asserting on.
That is not a convenience. A limiter test that really waited would be the
slowest thing in the suite, would go flaky on a loaded runner, and would end up
loosened or deleted -- and the limiter is the part of this module that keeps an
API key alive.

Four things are defended below, in the order they can hurt.

**The rate ceiling, including under concurrency.** A connector paginates with
``asyncio.gather``; if the bucket releases its lock across the wait, every
waiter wakes against the same stale token count and the whole batch leaves at
once. That is a 429 from a vendor whose terms we agreed to, so the concurrent
test matters more than the sequential one.

**The retry policy as a number of attempts.** Four attempts is three retries.
Reading it the other way is a 25% larger bill on every failing source and one
more request against a server that is already saying stop.

**What is never asked for at all.** robots.txt gates crawling and does not gate
a documented API; a handful of hosts are refused at the transport whatever a
connector declares.

**That a key never reaches disk or a log line.** ``redact`` is asserted on both
shapes that exist in this project -- a query parameter, and jooble's key in a
path segment -- and the cache test greps the actual file on disk for the secret.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Protocol

import httpx
import pytest
import pytest_asyncio
import respx

from app.core.config import settings
from app.core.exceptions import RateLimitError, SourceError
from app.sources.base import AccessMode, BaseSource, RateLimit, RawPosting, SearchQuery
from app.sources.http import (
    MAX_ATTEMPTS,
    MAX_RETRY_AFTER_SECONDS,
    REDACTED,
    HHChallengedError,
    ResponseCache,
    RetryableResponseError,
    RobotsCache,
    SourceClient,
    SourceHTTP,
    TokenBucket,
    parse_retry_after,
    redact,
    worth_retrying,
)

pytestmark = pytest.mark.unit

#: A host that cannot resolve, so a request that escaped respx would fail rather
#: than quietly succeed against something real.
HOST = "https://jobs.example.invalid"
URL = f"{HOST}/search"
OTHER_URL = f"{HOST}/search-again"
ROBOTS_URL = f"{HOST}/robots.txt"
PAGE_URL = f"{HOST}/jobs/1"

#: Deliberately not the configured default, so an assertion on a sent header
#: proves the value came from settings rather than from httpx's own default.
USER_AGENT = "wwao-test-agent/9.9 (+https://example.invalid/bot)"

#: The hh host and the exact page of the live run of 2026-09-06 whose 302 into
#: a captcha was reported as a robots.txt violation. Real values, so the tests
#: below describe the incident rather than an invented one.
HH_HOST = "https://almaty.hh.kz"
HH_VACANCY_URL = f"{HH_HOST}/vacancy/136284790"
HH_CAPTCHA_URL = f"{HH_HOST}/account/captcha?backurl=/vacancy/136284790&state=7f3c1a"

#: Key-shaped: long, opaque, no dot. Both redaction rules should catch it.
SECRET = "0f9c2b7a41d84e6f8a3b5c7d9e1f2a3b"

#: The bucket every timing test uses: five a second, one in hand.
RATE = 5.0
BURST = 1

#: A fixed instant, so a Retry-After date is arithmetic rather than a race.
NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# -- fakes -------------------------------------------------------------


class FakeClock:
    """A clock that only moves when something sleeps on it.

    ``sleep`` still yields to the event loop for one tick. Skipping that would
    make the fake change task interleaving, and a bucket that releases its lock
    across the wait could then pass a concurrency test it should fail.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.moment = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        """The current fake monotonic reading."""
        return self.moment

    async def sleep(self, delay: float) -> None:
        """Record the wait, advance the clock by it, and let other tasks run."""
        self.sleeps.append(delay)
        self.moment += delay
        await asyncio.sleep(0)


class FakeCalendar:
    """Wall-clock time the test moves by hand, for the two TTLs."""

    def __init__(self, start: datetime) -> None:
        self.moment = start

    def now(self) -> datetime:
        """The current fake instant."""
        return self.moment

    def advance(self, delta: timedelta) -> None:
        """Jump forward, as an expiring entry needs."""
        self.moment += delta


class ApiSource(BaseSource):
    """The smallest connector the transport will accept: a documented API.

    Fast and burst-heavy so its bucket never sleeps -- in the retry tests the
    only recorded waits must be the retry policy's, not the limiter's.
    """

    slug = "fake-api"
    name = "Fake API"
    access_mode = AccessMode.API
    terms_url = "https://example.invalid/terms"
    rate_limit = RateLimit(requests_per_second=50.0, burst=100)
    cache_ttl = timedelta(hours=1)

    def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]:
        """Never called: these tests drive the transport, not a connector."""
        raise NotImplementedError


class CrawlSource(ApiSource):
    """Pages authored for people. robots.txt governs this one."""

    slug = "fake-crawl"
    name = "Fake crawler"
    access_mode = AccessMode.CRAWL


class PoliteCrawlSource(CrawlSource):
    """A crawler with one token in hand, so a Crawl-delay is visible in the wait."""

    slug = "fake-polite"
    rate_limit = RateLimit(requests_per_second=50.0, burst=1)


class MeteredSource(ApiSource):
    """One request a second, so spending a token costs a measurable wait."""

    slug = "fake-metered"
    rate_limit = RateLimit(requests_per_second=1.0, burst=1)


class ClientFactory(Protocol):
    """Builds clients wired to the test's clock and closes the ones it owns."""

    def __call__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        cache: ResponseCache | None = None,
        robots: RobotsCache | None = None,
    ) -> SourceClient:
        """One client, registered for teardown."""
        ...


# -- fixtures ----------------------------------------------------------


@pytest.fixture(autouse=True)
def ambient(monkeypatch: pytest.MonkeyPatch) -> None:
    """No dev cache and a recognisable agent, whatever the developer's .env says."""
    monkeypatch.setattr(settings, "http_cache_dir", None)
    monkeypatch.setattr(settings, "user_agent", USER_AGENT)


@pytest.fixture
def http() -> Iterator[respx.MockRouter]:
    """Every request intercepted before it leaves; an unmocked one raises."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def clock() -> FakeClock:
    """The clock and the sleeper the client under test is built with."""
    return FakeClock()


@pytest_asyncio.fixture
async def clients(clock: FakeClock) -> AsyncIterator[ClientFactory]:
    """A client factory that closes what it built when the test ends."""
    built: list[SourceClient] = []

    def factory(
        client: httpx.AsyncClient | None = None,
        *,
        cache: ResponseCache | None = None,
        robots: RobotsCache | None = None,
    ) -> SourceClient:
        made = SourceClient(client, cache=cache, robots=robots, clock=clock.now, sleep=clock.sleep)
        built.append(made)
        return made

    yield factory
    for made in built:
        await made.aclose()


@pytest_asyncio.fixture
async def borrowed() -> AsyncIterator[httpx.AsyncClient]:
    """A client the caller owns, handed to SourceClient as a seam."""
    async with httpx.AsyncClient() as client:
        yield client


# -- helpers -----------------------------------------------------------


def make_bucket(clock: FakeClock, *, rate: float = RATE, burst: int = BURST) -> TokenBucket:
    """A bucket wired to the fake clock and the fake sleeper."""
    return TokenBucket(
        RateLimit(requests_per_second=rate, burst=burst), clock=clock.now, sleep=clock.sleep
    )


def busiest_second(grants: Sequence[float]) -> int:
    """The most grants any one-second window holds.

    The epsilon is for the float drift of adding 0.2 ten times, not for slack in
    the limit: a grant that lands a hair before the window closes still counts.
    """
    return max(
        sum(1 for grant in grants if start <= grant <= start + 1.0 + 1e-9) for start in grants
    )


def bind(client: SourceClient, source: BaseSource) -> SourceHTTP:
    """The per-source facade, holding that source's own bucket."""
    return client.bind(source)


# -- the rate ceiling --------------------------------------------------


async def test_eleven_requests_at_five_a_second_cost_two_fake_seconds_and_no_real_ones(
    clock: FakeClock,
) -> None:
    """Both halves of the deal this module is built on, in one test.

    The fake seconds are the rate ceiling: eleven grants from a bucket holding
    one is ten refills at 0.2s, and if that number drifts the connector walks
    into the 429 the limiter exists to prevent. The real seconds are why the
    ceiling can be tested at all -- a limiter proved by waiting is a suite that
    takes minutes, so it gets loosened, so it stops proving anything.
    """
    bucket = make_bucket(clock)

    started = time.perf_counter()
    for _ in range(11):
        await bucket.acquire()
    real_elapsed = time.perf_counter() - started

    assert clock.sleeps == pytest.approx([0.2] * 10)
    assert clock.now() == pytest.approx(2.0)
    assert real_elapsed < 0.1


async def test_no_one_second_window_ever_holds_more_than_burst_plus_rate(
    clock: FakeClock,
) -> None:
    """The ceiling stated the way a vendor states it, rather than as a total.

    A limiter can spend the right number of seconds overall and still hand out
    every grant in one clump at the end, which is exactly the shape that trips
    a sliding-window limiter on the other side. Sampling every window, not just
    the whole run, is what makes the assertion mean "never faster than this".
    """
    bucket = make_bucket(clock)
    grants: list[float] = []

    for _ in range(11):
        await bucket.acquire()
        grants.append(clock.now())

    assert busiest_second(grants) == BURST + int(RATE) == 6


async def test_ten_concurrent_acquires_are_still_held_to_the_same_rate(
    clock: FakeClock,
) -> None:
    """The test that catches releasing the lock across the wait.

    Connectors paginate with ``asyncio.gather``. If the bucket sleeps outside
    its lock, all ten waiters refill against the same stale token count, wake
    together and leave as one burst -- the run looks fine locally and gets the
    key rate-limited in production. Serialised properly, one grant is free and
    the other nine are 0.2s apart.
    """
    bucket = make_bucket(clock)
    grants: list[float] = []

    async def one() -> None:
        await bucket.acquire()
        grants.append(clock.now())

    await asyncio.gather(*(one() for _ in range(10)))

    assert len(grants) == 10
    assert clock.sleeps == pytest.approx([0.2] * 9)
    assert clock.now() == pytest.approx(1.8)
    # Clumped grants, not the total, are the signature of the bug.
    assert busiest_second(grants) <= BURST + int(RATE)
    spacing = [later - earlier for earlier, later in pairwise(sorted(grants))]
    assert min(spacing) == pytest.approx(0.2)


async def test_widen_only_ever_slows_the_bucket_down(clock: FakeClock) -> None:
    """A host's Crawl-delay is a floor on politeness, never a licence to speed up.

    robots.txt is attacker-adjacent input in the sense that matters here: it is
    written by someone else and applied to our limiter. If ``widen`` took the
    stated delay as the new rate, a site answering ``Crawl-delay: 0.1`` would
    lift our own configured ceiling tenfold on its say-so.
    """
    bucket = make_bucket(clock)

    bucket.widen(0.5)  # 2 rps: slower than our 5, so it applies
    await bucket.acquire()  # the burst token, free
    await bucket.acquire()
    assert clock.sleeps == [0.5]

    bucket.widen(0.1)  # 10 rps: faster than 2, must be ignored
    await bucket.acquire()
    assert clock.sleeps == [0.5, 0.5]

    bucket.widen(0.0)  # no delay stated: a no-op, not a division by zero
    await bucket.acquire()
    assert clock.sleeps == [0.5, 0.5, 0.5]


# -- reading the headers -----------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("2", 2.0),
        ("  7 ", 7.0),
        ("0", 0.0),
        # A count in the past is now, not a negative sleep.
        ("-5", 0.0),
        ("Thu, 01 Jan 2026 12:00:30 GMT", 30.0),
        ("Thu, 01 Jan 2026 11:59:00 GMT", 0.0),
        ("soon", None),
        ("", None),
        (None, None),
    ],
)
def test_retry_after_is_read_in_both_forms_rfc_7231_allows(
    header: str | None, expected: float | None
) -> None:
    """The date form is the half that gets dropped, and dropping it is silent.

    ``int()`` alone raises on an HTTP date, and the obvious repair -- catch
    ValueError, return None -- discards exactly the header we promised to
    honour, so a source asking for a two-minute pause gets hammered with
    exponential backoff instead. Junk must still be None: a header we cannot
    read is not a licence to invent a delay.
    """
    assert parse_retry_after(header, now=NOW) == expected


def test_redact_removes_a_key_from_a_query_parameter_and_from_a_path_segment() -> None:
    """A URL is written to logs and into cache envelopes, so it must never carry a key.

    Both shapes exist in this project: adzuna and friends put the key in the
    query string, jooble puts it in the path. Covering only the query leaves
    jooble's live key sitting in a plain file on a developer's disk and in
    every log line about that request.
    """
    assert (
        redact(f"{HOST}/search?api_key={SECRET}&q=python")
        == f"{HOST}/search?api_key={REDACTED}&q=python"
    )
    assert redact(f"{HOST}/api/{SECRET}/jobs") == f"{HOST}/api/{REDACTED}/jobs"
    # Ordinary route segments survive, or the redacted URL says nothing at all.
    assert redact(f"{HOST}/api/v2/search?q=python") == f"{HOST}/api/v2/search?q=python"
    # httpx.URL is the type the call sites actually hold.
    assert redact(httpx.URL(f"{HOST}/search?token={SECRET}")) == f"{HOST}/search?token={REDACTED}"


# -- retries -----------------------------------------------------------


async def test_a_429_is_retried_after_exactly_the_delay_the_server_asked_for(
    clock: FakeClock, clients: ClientFactory, http: respx.MockRouter
) -> None:
    """Honouring Retry-After is the difference between a pause and a ban.

    Exponential backoff would wait its own number and try again too early; the
    source's answer is the only number that is known to be long enough. The
    assertion is on the exact list of waits because "it slept sometime" would
    pass with the header ignored.
    """
    route = http.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "2"}, text="slow down"),
            httpx.Response(200, text="the body we came for"),
        ]
    )

    body = await bind(clients(), ApiSource()).get_text(URL)

    assert body == "the body we came for"
    assert route.call_count == 2
    assert clock.sleeps == [2.0]


async def test_an_absurd_retry_after_ends_the_call_instead_of_hanging_the_run(
    clock: FakeClock, clients: ClientFactory, http: respx.MockRouter
) -> None:
    """An hour-long pause inside a pipeline run is indistinguishable from a hang.

    So the wait is refused and the source's own number is handed back in the
    problem document, which is what lets the scheduler reschedule against it
    rather than guess. Losing that field turns a precise "come back in an hour"
    into an opaque failure the run has no way to act on.
    """
    route = http.get(URL).mock(return_value=httpx.Response(429, headers={"retry-after": "3600"}))

    with pytest.raises(RateLimitError) as excinfo:
        await bind(clients(), ApiSource()).get_text(URL)

    assert MAX_RETRY_AFTER_SECONDS < 3600.0
    assert route.call_count == 1
    assert clock.sleeps == []
    problem = excinfo.value.to_problem()
    assert problem["retry_after"] == 3600.0
    assert problem["status"] == 429
    assert problem["source_slug"] == ApiSource.slug


async def test_a_server_error_is_tried_at_most_four_times_in_total(
    clock: FakeClock, clients: ClientFactory, http: respx.MockRouter
) -> None:
    """Four attempts, not four retries. Read the other way it is five requests.

    That is 25% more load on a server already returning 500, 25% more spend on
    a metered source, and a failing run that takes a quarter longer to admit it.
    The count of waits is asserted for the same reason: three gaps, four tries.
    """
    route = http.get(URL).mock(return_value=httpx.Response(500, text="boom"))

    with pytest.raises(RetryableResponseError) as excinfo:
        await bind(clients(), ApiSource()).get_text(URL)

    assert route.call_count == MAX_ATTEMPTS == 4
    assert len(clock.sleeps) == MAX_ATTEMPTS - 1
    assert excinfo.value.response_status == 500


async def test_a_404_is_not_retried_because_it_is_our_own_mistake(
    clock: FakeClock, clients: ClientFactory, http: respx.MockRouter
) -> None:
    """Retrying a 404 repeats the mistake three more times and hides it.

    A wrong path or a dropped credential is a bug in the connector, and the
    only signal that it is one is that the failure comes back immediately
    instead of looking like a flaky upstream.
    """
    route = http.get(URL).mock(return_value=httpx.Response(404, text="gone"))

    with pytest.raises(SourceError) as excinfo:
        await bind(clients(), ApiSource()).get_text(URL)

    assert route.call_count == 1
    assert clock.sleeps == []
    assert not isinstance(excinfo.value, RetryableResponseError)
    assert excinfo.value.extra["response_status"] == 404


# -- who owns the connection pool --------------------------------------


async def test_an_injected_client_survives_the_call_and_is_not_closed_by_aclose(
    borrowed: httpx.AsyncClient, clients: ClientFactory, http: respx.MockRouter
) -> None:
    """The ollama bug, kept out of the module that does thousands of requests.

    Closing a client somebody else handed us makes the seam single-use: httpx
    refuses to reopen it with a RuntimeError no caller's ``except`` catches, so
    the second request in a run explodes somewhere unrelated to the code that
    closed it. Two requests through one injected client is the whole assertion.
    """
    route = http.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    client = clients(borrowed)
    api = bind(client, ApiSource())

    assert await api.get_text(URL) == "ok"
    assert borrowed.is_closed is False
    # The call that used to raise RuntimeError.
    assert await api.get_text(URL) == "ok"
    assert borrowed.is_closed is False

    await client.aclose()

    assert borrowed.is_closed is False
    assert route.call_count == 2


async def test_a_client_we_built_ourselves_is_closed_by_aclose(
    clients: ClientFactory, http: respx.MockRouter
) -> None:
    """The other half: borrowed must not mean leaked.

    Nobody else holds a reference to a pool this class built, so shutdown is
    the only chance to close it. Left open it keeps sockets and TLS sessions
    alive for the life of the process and httpx warns about it.
    """
    http.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    client = clients()
    built = client.http
    assert built is client.http  # built once, lazily, and reused

    await bind(client, ApiSource()).get_text(URL)
    await client.aclose()

    assert built.is_closed is True


async def test_the_configured_user_agent_reaches_the_wire(
    clients: ClientFactory, http: respx.MockRouter
) -> None:
    """An anonymous crawler is the one a site blocks first, and cannot be contacted.

    The agent string carries the project URL so an operator with a complaint
    has somewhere to send it instead of a firewall rule. Asserting it on the
    sent request is what proves the value came from settings rather than from
    httpx's own default.
    """
    route = http.get(URL).mock(return_value=httpx.Response(200, text="ok"))

    await bind(clients(), ApiSource()).get_text(URL)

    assert route.calls[0].request.headers["user-agent"] == USER_AGENT


# -- robots.txt --------------------------------------------------------


async def test_a_crawler_is_stopped_before_the_request_when_robots_says_no(
    clients: ClientFactory, http: respx.MockRouter
) -> None:
    """Asking and then fetching anyway is worse than not asking at all.

    The refusal has to happen before anything leaves, so the assertion that
    matters is the one on the page route: zero calls. A check that logged the
    disallow and fetched regardless would pass any test that only looked at the
    raised error.
    """
    robots = http.get(ROBOTS_URL).mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /jobs\n")
    )
    page = http.get(PAGE_URL).mock(return_value=httpx.Response(200, text="must not be read"))

    with pytest.raises(SourceError) as excinfo:
        await bind(clients(), CrawlSource()).get_text(PAGE_URL)

    assert page.call_count == 0
    assert robots.call_count == 1
    assert "robots.txt" in excinfo.value.detail
    # The refusal is logged and returned, so the URL in it must already be safe.
    assert SECRET not in excinfo.value.detail


async def test_an_api_source_is_not_gated_by_the_websites_robots_file(
    clients: ClientFactory, http: respx.MockRouter
) -> None:
    """Calling a documented endpoint under its own terms is not crawling.

    A vendor's robots.txt scopes crawlers on their website; it is not the
    contract for the API they issued us a key for, and many of them disallow
    everything. Reading it as a veto would switch off every paid source we
    have. The robots route is asserted never to be fetched at all -- not merely
    ignored -- because an unnecessary request is still a request.
    """
    robots = http.get(ROBOTS_URL).mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    )
    page = http.get(PAGE_URL).mock(return_value=httpx.Response(200, text="ok"))

    assert await bind(clients(), ApiSource()).get_text(PAGE_URL) == "ok"

    assert page.call_count == 1
    assert robots.call_count == 0


async def test_a_missing_robots_file_means_allowed(
    clients: ClientFactory, http: respx.MockRouter
) -> None:
    """RFC 9309 is explicit that 4xx means no restrictions, and hosts rely on it.

    Plenty of API hosts serve no robots.txt at all. A "no file, no permission"
    rule would disable every host that never asked for anything, and the
    symptom would be a source that is silently always empty.
    """
    robots = http.get(ROBOTS_URL).mock(return_value=httpx.Response(404, text="Not Found"))
    page = http.get(PAGE_URL).mock(return_value=httpx.Response(200, text="ok"))

    assert await bind(clients(), CrawlSource()).get_text(PAGE_URL) == "ok"

    assert robots.call_count == 1
    assert page.call_count == 1


async def test_robots_is_fetched_once_per_host_per_day(
    clients: ClientFactory, http: respx.MockRouter
) -> None:
    """Re-reading robots.txt before every page doubles the load on the host.

    A crawl is thousands of pages against one host, so an uncached check is
    thousands of extra requests -- rude, slow, and the fastest way to be
    blocked by the site being asked for permission. The day-long expiry is the
    other half: a host that changes its mind must be heard within a day.
    """
    calendar = FakeCalendar(datetime(2026, 1, 1, tzinfo=UTC))
    robots = http.get(ROBOTS_URL).mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )
    page = http.get(PAGE_URL).mock(return_value=httpx.Response(200, text="ok"))
    crawler = bind(clients(robots=RobotsCache(now=calendar.now)), CrawlSource())

    for _ in range(3):
        await crawler.get_text(PAGE_URL)

    assert robots.call_count == 1
    assert robots.calls[0].request.headers["user-agent"] == USER_AGENT

    calendar.advance(timedelta(hours=25))
    await crawler.get_text(PAGE_URL)

    assert robots.call_count == 2
    assert page.call_count == 4


async def test_a_stated_crawl_delay_slows_this_sources_bucket(
    clock: FakeClock, clients: ClientFactory, http: respx.MockRouter
) -> None:
    """A Crawl-delay nobody applies is a promise broken in the politest possible way.

    The host has written down the rate it wants; reading the file and then
    ignoring the number is the behaviour that gets a crawler banned by an
    operator who can prove we read their terms.
    """
    http.get(ROBOTS_URL).mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\nCrawl-delay: 2\n")
    )
    http.get(PAGE_URL).mock(return_value=httpx.Response(200, text="ok"))
    crawler = bind(clients(), PoliteCrawlSource())

    await crawler.get_text(PAGE_URL)
    await crawler.get_text(PAGE_URL)

    # Two a second is what the source declares; the host asked for one per two.
    assert clock.sleeps == [2.0]


# -- hosts that are refused whatever a connector says -------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://linkedin.com/jobs/view/1",
        "https://www.linkedin.com/jobs/view/1",
    ],
)
async def test_a_blocked_host_is_refused_at_the_transport(
    clients: ClientFactory, http: respx.MockRouter, url: str
) -> None:
    """The ban has to be code, not a convention a new connector can forget.

    LinkedIn's terms forbid automated collection, so the check runs before
    robots.txt, before the limiter and before anything leaves -- a connector
    declaring itself an API with a permissive robots.txt must still be refused,
    and the subdomain form must be refused with the bare one.
    """
    robots = http.get("https://www.linkedin.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )
    page = http.get(url).mock(return_value=httpx.Response(200, text="must not be read"))

    with pytest.raises(SourceError) as excinfo:
        await bind(clients(), ApiSource()).get_text(url)

    assert page.call_count == 0
    assert robots.call_count == 0
    assert "linkedin.com" in excinfo.value.detail


# -- the dev cache -----------------------------------------------------


async def test_a_second_identical_get_is_served_from_disk(
    tmp_path: Path, clients: ClientFactory, http: respx.MockRouter
) -> None:
    """Development means running the same query fifty times against a metered source.

    Without the cache that is fifty of a daily allowance of a few hundred spent
    before lunch, and the source is dark for the rest of the day.
    """
    route = http.get(URL).mock(return_value=httpx.Response(200, text="from the network"))
    api = bind(clients(cache=ResponseCache(tmp_path)), ApiSource())

    first = await api.get_text(URL)
    second = await api.get_text(URL)

    assert first == second == "from the network"
    assert route.call_count == 1


async def test_an_entry_past_its_ttl_is_refetched(
    tmp_path: Path, clients: ClientFactory, http: respx.MockRouter
) -> None:
    """A cache with no expiry is a source that stops returning new vacancies.

    The failure is invisible: the pipeline runs, the connector yields, every
    posting is yesterday's. The TTL is the only thing that makes a stale entry
    a miss rather than an answer.
    """
    calendar = FakeCalendar(NOW)
    route = http.get(URL).mock(
        side_effect=[httpx.Response(200, text="yesterday"), httpx.Response(200, text="today")]
    )
    api = bind(clients(cache=ResponseCache(tmp_path, now=calendar.now)), ApiSource())

    assert await api.get_text(URL) == "yesterday"

    calendar.advance(ApiSource.cache_ttl * 2)

    assert await api.get_text(URL) == "today"
    assert route.call_count == 2


async def test_a_cache_hit_does_not_spend_a_token(
    clock: FakeClock, tmp_path: Path, clients: ClientFactory, http: respx.MockRouter
) -> None:
    """A cache that still waits for the limiter is a cache that saves nothing.

    A one-a-second source replayed from disk would take a second per entry, so
    the fast local re-run the cache exists to give would be exactly as slow as
    the network. The contrast at the end is the control: the same bucket does
    charge for a request that actually goes out.
    """
    http.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    http.get(OTHER_URL).mock(return_value=httpx.Response(200, text="ok"))
    api = bind(clients(cache=ResponseCache(tmp_path)), MeteredSource())

    await api.get_text(URL)  # spends the only token in the burst
    assert clock.now() == 0.0

    await api.get_text(URL)  # served from disk

    assert clock.now() == 0.0
    assert clock.sleeps == []

    await api.get_text(OTHER_URL)  # a miss: this is what a token costs

    assert clock.sleeps == [1.0]


async def test_the_disk_cache_is_off_until_a_directory_is_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clients: ClientFactory,
    http: respx.MockRouter,
) -> None:
    """Caching responses in production would serve last week's vacancies forever.

    So the default is off and turning it on is a deliberate line in a developer's
    .env. Both halves are asserted here because "off" that is really on writes
    files on a server nobody expected to be writing files.
    """
    route = http.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    unconfigured = bind(clients(), ApiSource())

    await unconfigured.get_text(URL)
    await unconfigured.get_text(URL)

    assert route.call_count == 2
    assert list(tmp_path.rglob("*")) == []

    monkeypatch.setattr(settings, "http_cache_dir", tmp_path)
    configured = bind(clients(), ApiSource())

    await configured.get_text(URL)
    await configured.get_text(URL)

    assert route.call_count == 3
    assert [path.name for path in tmp_path.rglob("*.json")] != []


async def test_a_key_in_the_url_never_reaches_the_cache_file(
    tmp_path: Path, clients: ClientFactory, http: respx.MockRouter
) -> None:
    """A cache entry is a plain file on a laptop, and it outlives the run that wrote it.

    Storing the URL verbatim writes a live credential to disk, where it is
    backed up, synced and grepped by anything looking for one. The envelope
    keeps a URL only so a person can tell what the file holds, so the redacted
    form has to be the only form that is ever written.
    """
    http.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    api = bind(clients(cache=ResponseCache(tmp_path)), ApiSource())

    await api.get_text(URL, params={"key": SECRET, "q": "python"})

    files = list(tmp_path.rglob("*.json"))
    assert len(files) == 1
    blob = files[0].read_text(encoding="utf-8")
    assert SECRET not in blob
    assert REDACTED in blob
    # Not in the file name either: the key is a digest, not the URL.
    assert all(SECRET not in str(path) for path in tmp_path.rglob("*"))


# -- what hh's robots.txt forbids and robotparser cannot ---------------


HH_ROBOTS = (
    "User-agent: *\nAllow: *?u*\nAllow: *?currencyCode*\nDisallow: *?*\nDisallow: /resume$\n"
)


async def test_robotparser_really_does_allow_the_url_hh_forbids(
    clients: ClientFactory, http: respx.MockRouter
) -> None:
    """The premise of the guard below, pinned so nobody deletes it as paranoia.

    hh closes its search with ``Disallow: *?*``. CPython's robotparser matches a
    rule as a literal path prefix and has no wildcards at all, so that rule
    matches nothing and this URL comes back allowed. Should a future CPython
    learn wildcards, this test fails and the transport guard becomes belt and
    braces rather than the only thing standing there -- either way somebody
    reads the reasoning before changing it.
    """
    http.get("https://almaty.hh.kz/robots.txt").mock(
        return_value=httpx.Response(200, text=HH_ROBOTS)
    )
    robots = RobotsCache()
    client = clients()

    allowed = await robots.allows(
        client.http,
        httpx.URL("https://almaty.hh.kz/search/vacancy?text=python"),
        user_agent=USER_AGENT,
    )

    assert allowed is True


@pytest.mark.parametrize(
    "url",
    [
        "https://almaty.hh.kz/search/vacancy?text=python",
        "https://hh.kz/vacancy/1?utm_source=x",
        "https://hh.ru/vacancy/1?hhtmFrom=vacancy_search_list",
    ],
)
async def test_a_query_string_on_hh_is_refused_before_anything_leaves(
    clients: ClientFactory, http: respx.MockRouter, url: str
) -> None:
    """The rule robots states and robotparser cannot apply is applied here.

    Any query string, not only a search: the file says ``Disallow: *?*`` and the
    three ``Allow`` exceptions are narrower than anything a connector would
    build. Refused before robots.txt is even fetched, so a connector cannot
    reach the search results by any route.
    """
    robots = http.get("https://almaty.hh.kz/robots.txt").mock(
        return_value=httpx.Response(200, text=HH_ROBOTS)
    )
    page = http.get(url).mock(return_value=httpx.Response(200, text="must not be read"))

    with pytest.raises(SourceError) as excinfo:
        await bind(clients(), CrawlSource()).get_text(url)

    assert page.call_count == 0
    assert robots.call_count == 0
    assert "строкой запроса" in excinfo.value.detail


@pytest.mark.parametrize(
    "url",
    [
        "https://hh.kz/search/vacancy",
        "https://almaty.hh.kz/search/vacancy",
        "https://api.hh.ru/vacancies/123",
    ],
)
async def test_hh_search_and_the_closed_api_are_refused_by_path(
    clients: ClientFactory, http: respx.MockRouter, url: str
) -> None:
    """Named again without their parameters, so no spelling of them is reachable.

    The jobseeker half of api.hh.ru has answered 403 to every programmatic
    client since April 2026; the search pages are what robots forbids. Neither
    is a host-wide ban, because the rest of both hosts is what the connector
    legitimately reads.
    """
    page = http.get(url).mock(return_value=httpx.Response(200, text="must not be read"))

    with pytest.raises(SourceError) as excinfo:
        await bind(clients(), CrawlSource()).get_text(url)

    assert page.call_count == 0
    assert "закрыт" in excinfo.value.detail


@pytest.mark.parametrize(
    "url",
    [
        "https://api.hh.ru/areas/40",
        "https://api.hh.ru/professional_roles",
        "https://almaty.hh.kz/vacancy/136390570",
        "https://almaty.hh.kz/sitemap/main.xml",
    ],
)
async def test_the_open_parts_of_hh_stay_reachable(
    clients: ClientFactory, http: respx.MockRouter, url: str
) -> None:
    """The point of a path ban rather than a host ban.

    hh's dictionaries answer 200 and are the reference this project normalises
    against; the sitemap and the vacancy pages are what robots.txt opens. A
    blunter rule would have to give up one of them to forbid the other.
    """
    http.get("https://api.hh.ru/robots.txt").mock(return_value=httpx.Response(404))
    http.get("https://almaty.hh.kz/robots.txt").mock(
        return_value=httpx.Response(200, text=HH_ROBOTS)
    )
    page = http.get(url).mock(return_value=httpx.Response(200, text="{}"))

    body = await bind(clients(), CrawlSource()).get_text(url)

    assert body == "{}"
    assert page.call_count == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://almaty.hh.kz/sitemap/resumes0.xml",
        "https://hh.kz/resume/abcdef",
        "https://hh.ru/resume",
    ],
)
async def test_other_peoples_resumes_are_refused_by_the_transport(
    clients: ClientFactory, http: respx.MockRouter, url: str
) -> None:
    """The one prohibition that must not depend on a connector's regex.

    hh's sitemap index lists ``resumes0..14.xml`` in the same file the crawler
    reads for vacancies, and those are living people's CVs. Nothing else in the
    stack refuses them: robots.txt says ``Disallow: /resume$``, robotparser
    quotes that into the literal prefix ``/resume%24``, and no real path starts
    with it -- so the file we are obeying answers "allowed", as the second half
    of this test shows. The connector picks its sitemaps with an allow-list, but
    an allow-list is one edit away from being a substring test, and this is not
    a mistake anybody should be able to make twice.
    """
    # Every hh host, because the second half of this test asks robots.txt what
    # it makes of the same URL.
    http.get(url__regex=r"https://[^/]+/robots\.txt").mock(
        return_value=httpx.Response(200, text=HH_ROBOTS + "Disallow: /resume$\n")
    )
    page = http.get(url).mock(return_value=httpx.Response(200, text="must not be read"))

    with pytest.raises(SourceError) as excinfo:
        await bind(clients(), CrawlSource()).get_text(url)

    assert page.call_count == 0
    assert "закрыт" in excinfo.value.detail

    permitted = await RobotsCache().allows(clients().http, httpx.URL(url), user_agent=USER_AGENT)
    assert permitted is True, "the point: robots.txt does not close this for us"


async def test_a_dictionary_call_with_parameters_still_reaches_api_hh_ru(
    clients: ClientFactory, http: respx.MockRouter
) -> None:
    """The suffix match must not lend hh.kz's rules to a host that has none.

    ``api.hh.ru`` ends with ``hh.ru``, so a naive queryless rule refuses
    ``/professional_roles?locale=RU`` -- and blames a robots.txt that host
    answers 404 for, which RFC 9309 reads as no restrictions at all. Those
    dictionaries are the reference this project normalises hh vacancies
    against, and they take parameters.
    """
    http.get("https://api.hh.ru/robots.txt").mock(return_value=httpx.Response(404))
    route = http.get("https://api.hh.ru/professional_roles").mock(
        return_value=httpx.Response(200, json={"categories": []})
    )

    payload = await bind(clients(), CrawlSource()).get_json(
        "https://api.hh.ru/professional_roles", params={"locale": "RU"}
    )

    assert payload == {"categories": []}
    assert dict(route.calls[0].request.url.params) == {"locale": "RU"}


# -- the cache salt ----------------------------------------------------


async def test_a_changed_salt_is_a_cache_miss_and_an_unchanged_one_is_a_hit(
    tmp_path: Path, clients: ClientFactory, http: respx.MockRouter
) -> None:
    """Invalidation by "the thing changed", not by "an hour passed".

    hh publishes a lastmod per vacancy in its sitemap, so a crawler knows before
    it asks whether a page can have moved. Passing that as the salt makes an
    edited posting a miss and an untouched one a hit for the whole TTL, which is
    what a thirty-day cache over fourteen thousand pages needs in order to be
    both cheap and correct.
    """
    cache = ResponseCache(tmp_path)
    route = http.get(PAGE_URL).mock(
        side_effect=[httpx.Response(200, text=body) for body in ("first", "second")]
    )
    http.get(ROBOTS_URL).mock(return_value=httpx.Response(404))
    client = clients(cache=cache)
    source = bind(client, ApiSource())

    first = await source.get_text(PAGE_URL, cache_salt="2026-09-06T10:00:00+03:00")
    again = await source.get_text(PAGE_URL, cache_salt="2026-09-06T10:00:00+03:00")
    moved = await source.get_text(PAGE_URL, cache_salt="2026-09-06T11:00:00+03:00")

    assert (first, again) == ("first", "first")
    assert route.call_count == 2, "the repeat came from disk, the changed salt did not"
    assert moved == "second"


# -- redirects, which the remote server chooses ------------------------


@pytest.mark.parametrize(
    ("destination", "expected"),
    [
        ("https://almaty.hh.kz/search/vacancy?text=python", "запроса"),
        ("https://almaty.hh.kz/sitemap/resumes0.xml", "закрыт"),
        ("https://www.linkedin.com/jobs/view/1", "linkedin.com"),
    ],
)
async def test_a_redirect_cannot_carry_a_request_somewhere_it_may_not_go(
    clients: ClientFactory, http: respx.MockRouter, destination: str, expected: str
) -> None:
    """The bans have to survive a hop the remote server picked.

    The client follows redirects and httpx resolves the chain internally, so
    checking only the URL a connector passed in leaves every ban and robots.txt
    behind on the first hop. A 302 from a page we are allowed to read is then
    enough to reach hh's search, somebody's resume, or LinkedIn. Refused at the
    hop, not afterwards on response.history: by then it has been fetched, and
    "we never asked for it" is the promise.
    """
    http.get("https://almaty.hh.kz/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )
    start = "https://almaty.hh.kz/vacancy/1"
    http.get(start).mock(return_value=httpx.Response(302, headers={"location": destination}))
    landing = http.get(destination).mock(return_value=httpx.Response(200, text="must not be read"))

    with pytest.raises(SourceError) as excinfo:
        await bind(clients(), CrawlSource()).get_text(start)

    assert landing.call_count == 0
    assert expected in excinfo.value.detail


async def test_an_ordinary_redirect_still_works(
    clients: ClientFactory, http: respx.MockRouter
) -> None:
    """The guard refuses banned destinations, not redirects.

    Sources move pages, and a connector that broke on every 301 would be worse
    than one that followed them.
    """
    http.get(ROBOTS_URL).mock(return_value=httpx.Response(404))
    http.get(PAGE_URL).mock(
        return_value=httpx.Response(301, headers={"location": f"{HOST}/jobs/1-moved"})
    )
    moved = http.get(f"{HOST}/jobs/1-moved").mock(return_value=httpx.Response(200, text="here"))

    assert await bind(clients(), CrawlSource()).get_text(PAGE_URL) == "here"
    assert moved.call_count == 1


async def test_a_borrowed_client_is_guarded_too(
    borrowed: httpx.AsyncClient, clients: ClientFactory, http: respx.MockRouter
) -> None:
    """A client handed in from outside must not be a way around the bans.

    The hook is appended rather than assigned, because the client belongs to
    whoever built it and may carry hooks of its own.
    """
    page = http.get("https://indeed.com/viewjob").mock(
        return_value=httpx.Response(200, text="must not be read")
    )

    with pytest.raises(SourceError):
        await bind(clients(borrowed), ApiSource()).get_text("https://indeed.com/viewjob")

    assert page.call_count == 0


# -- a check for robots is not a rule we broke -------------------------
#
# Both of these end in a refused redirect, and both refusals are correct. The
# whole of this section is that they are not the same event and must not read
# as if they were. On the live run of 2026-09-06 the captcha was reported as a
# robots.txt violation -- "this host forbids any URL with a query string" -- for
# a request that had no query string in it, which sends whoever is on call to
# fix URL construction: an afternoon spent looking for a bug that does not
# exist, while the actual answer is to crawl that host more slowly or later.


def serve_hh_robots(http: respx.MockRouter) -> respx.Route:
    """hh's wildcard group, for the host these tests crawl."""
    return http.get(f"{HH_HOST}/robots.txt").mock(return_value=httpx.Response(200, text=HH_ROBOTS))


async def test_a_redirect_into_hhs_captcha_is_a_challenge_and_says_so(
    clients: ClientFactory, http: respx.MockRouter
) -> None:
    """The measured case, with the message it should have carried.

    Request 173 of 184 was this URL, with no query string and entirely within
    the rules, and hh answered a 302 into its captcha. Refusing to follow is
    right and unchanged; what the refusal is called is the fix. The query
    string belongs to hh's captcha, not to anything we built, so naming
    robots.txt describes a URL this process never constructed.
    """
    serve_hh_robots(http)
    page = http.get(HH_VACANCY_URL).mock(
        return_value=httpx.Response(302, headers={"location": HH_CAPTCHA_URL})
    )
    captcha = http.get(url__startswith=f"{HH_HOST}/account/captcha").mock(
        return_value=httpx.Response(200, text="докажите, что вы не робот")
    )

    with pytest.raises(HHChallengedError) as excinfo:
        await bind(clients(), CrawlSource()).get_text(HH_VACANCY_URL)

    assert captcha.call_count == 0, "the captcha page itself is never fetched"
    assert page.call_count == 1
    detail = excinfo.value.detail
    assert "проверкой на робота" in detail
    assert "robots.txt" not in detail, "we did not break robots.txt; hh challenged us"
    assert "строкой запроса" not in detail
    assert excinfo.value.challenge_path == "/account/captcha"
    # backurl and hh's opaque state say nothing to anybody and would then live
    # in pipeline_run.errors and in an HTTP response.
    assert "backurl" not in detail
    assert "state=" not in detail


async def test_a_redirect_to_what_robots_really_forbids_still_blames_robots(
    clients: ClientFactory, http: respx.MockRouter
) -> None:
    """The other half, unchanged and asserted so it cannot be lost to the fix.

    A hop into hh's search is a URL nobody may request, the message says which
    rule closes it, and it is not a challenge -- so a report cannot start
    calling every refused redirect an antibot decision either.
    """
    serve_hh_robots(http)
    http.get(HH_VACANCY_URL).mock(
        return_value=httpx.Response(
            302, headers={"location": f"{HH_HOST}/search/vacancy?text=python"}
        )
    )
    search = http.get(f"{HH_HOST}/search/vacancy?text=python").mock(
        return_value=httpx.Response(200, text="must not be read")
    )

    with pytest.raises(SourceError) as excinfo:
        await bind(clients(), CrawlSource()).get_text(HH_VACANCY_URL)

    assert search.call_count == 0
    assert not isinstance(excinfo.value, HHChallengedError)
    assert "строкой запроса" in excinfo.value.detail


async def test_a_302_is_not_by_itself_a_challenge(
    clients: ClientFactory, http: respx.MockRouter
) -> None:
    """The decision is the target, never the status code.

    hh moves a posting to its successor with a 302 often enough that the walk
    depends on following them. Reading the status as the signal would stop this
    connector on an ordinary Tuesday.
    """
    serve_hh_robots(http)
    http.get(HH_VACANCY_URL).mock(
        return_value=httpx.Response(302, headers={"location": f"{HH_HOST}/vacancy/136284791"})
    )
    successor = http.get(f"{HH_HOST}/vacancy/136284791").mock(
        return_value=httpx.Response(200, text="the posting that replaced it")
    )

    body = await bind(clients(), CrawlSource()).get_text(HH_VACANCY_URL)

    assert body == "the posting that replaced it"
    assert successor.call_count == 1


async def test_a_challenge_is_never_retried(
    clients: ClientFactory, http: respx.MockRouter, clock: FakeClock
) -> None:
    """Four attempts is the policy for a server having a bad minute.

    A captcha is a decision, not a bad minute. Repeating a request a host has
    just refused gains nothing and is the rudest thing a crawler can do, so the
    page is asked for exactly once and no backoff is ever waited.
    """
    serve_hh_robots(http)
    page = http.get(HH_VACANCY_URL).mock(
        return_value=httpx.Response(302, headers={"location": HH_CAPTCHA_URL})
    )

    with pytest.raises(HHChallengedError):
        await bind(clients(), CrawlSource()).get_text(HH_VACANCY_URL)

    assert MAX_ATTEMPTS > 1, "otherwise this test proves nothing about the policy"
    assert page.call_count == 1
    assert clock.sleeps == [], "a challenge must not even wait a backoff"


@pytest.mark.parametrize(
    ("failure", "retried"),
    [
        (HHChallengedError("stopped", host="hh.kz", path="/account/captcha"), False),
        (RetryableResponseError("503", status_code=503), True),
        (httpx.ConnectError("connection reset"), True),
        (SourceError("HTTP 404"), False),
    ],
)
def test_the_retry_policy_names_the_challenge_rather_than_omitting_it(
    failure: BaseException, retried: bool
) -> None:
    """Why the predicate is written out instead of a tuple of types.

    The challenge is a SourceError, and SourceError is the obvious thing for
    somebody to widen the retryable tuple to. That widening would turn one
    antibot decision into four requests against a host that has just said no,
    and nothing in the code would have objected. Naming it makes the widening
    fail here instead.
    """
    assert worth_retrying(failure) is retried


async def test_a_url_this_process_built_into_account_is_our_own_mistake(
    clients: ClientFactory, http: respx.MockRouter
) -> None:
    """The invariant the challenge check rests on, asserted from the other side.

    ``/account`` is closed to anything this process constructs, so a ``/account``
    URL arriving at the redirect hook can only have come from a hop the remote
    server chose. That is what lets the challenge say "we were pushed here" as a
    fact. Asked for directly, it is refused as the ordinary closed path it is,
    and the message says we built it.
    """
    page = http.get(url__startswith=f"{HH_HOST}/account").mock(
        return_value=httpx.Response(200, text="must not be read")
    )

    with pytest.raises(SourceError) as excinfo:
        await bind(clients(), CrawlSource()).get_text(f"{HH_HOST}/account/captcha")

    assert page.call_count == 0
    assert not isinstance(excinfo.value, HHChallengedError)
    assert "закрыт" in excinfo.value.detail


async def test_a_path_that_merely_starts_with_account_is_not_called_a_captcha(
    clients: ClientFactory, http: respx.MockRouter
) -> None:
    """``/accountancy`` is a word, not a challenge.

    The prefix test used for the blocked paths is loose on purpose --
    ``/sitemap/resumes`` has to catch ``/sitemap/resumes0.xml`` -- and reusing
    it here would report a page as an antibot decision, which is the exact class
    of wrong message this whole section exists to stop making.
    """
    serve_hh_robots(http)
    http.get(HH_VACANCY_URL).mock(
        return_value=httpx.Response(302, headers={"location": f"{HH_HOST}/accountancy"})
    )

    with pytest.raises(SourceError) as excinfo:
        await bind(clients(), CrawlSource()).get_text(HH_VACANCY_URL)

    assert not isinstance(excinfo.value, HHChallengedError)
