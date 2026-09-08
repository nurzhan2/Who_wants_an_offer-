"""One HTTP client for every connector: rate limiting, retries, robots, dev cache.

Client ownership copies ``app/llm/providers/ollama.py`` and the bug its
docstring records: an injected client is borrowed, not owned, because closing it
makes the seam single-use and httpx then refuses to reopen it with a
``RuntimeError`` that no caller's ``except`` clause catches. The rule is kept.
The lifetime is not: Ollama builds a client per exchange, which is fine for one
localhost call and wrong for a crawl of thousands of requests, where a
per-request client throws away the connection pool and the TLS session every
time. So one client, built lazily, closed only when we built it.

Two things here are deliberately stricter than they look.

**The token bucket takes both its clock and its sleeper as arguments.** A test
that proves a rate ceiling must not spend real seconds proving it, and
injecting only the clock leaves the waiting real — which makes the test slow
and flaky on a loaded runner rather than exact.

**robots.txt is applied to crawling, and crawling is not the same thing as
calling an API.** The rule, and the reasoning, are under :class:`RobotsCache`.

And one thing is stricter than robots.txt itself, because the standard library
is weaker than the file: ``urllib.robotparser`` has no wildcard support, so a
rule like hh's ``Disallow: *?*`` parses as a literal path prefix and matches
nothing at all. Rules we can read but it cannot apply are enforced by
:meth:`SourceClient._refuse_forbidden` instead of being silently discarded.

**A refusal and a challenge are two different events and say so.** Refusing a
URL means we built one we are not allowed to ask for. Being challenged means we
asked for exactly what we were allowed to ask for and the site decided we are a
robot. They arrive at the same place — :func:`guard_redirects`, on a hop the
remote server chose — and telling them apart is the whole of
:func:`refuse_challenge`; see :class:`HHChallengedError` for what it cost to
learn that the wrong message here is expensive.
"""

import asyncio
import base64
import hashlib
import os
import random
import time
import urllib.robotparser
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.core.config import settings
from app.core.exceptions import RateLimitError, SourceError
from app.core.logging import get_logger
from app.sources.base import AccessMode, BaseSource, RateLimit

logger = get_logger(__name__)

#: docs/SOURCES.md rule 4: attempts, not retries. Four attempts is three retries.
MAX_ATTEMPTS = 4
#: A source may legitimately answer ``Retry-After: 3600``. Honouring that inside
#: a pipeline run is indistinguishable from a hang, so past this we give up and
#: hand the caller the upstream's own number to reschedule with.
MAX_RETRY_AFTER_SECONDS = 60.0
#: docs/SOURCES.md rule 1.
ROBOTS_TTL = timedelta(hours=24)
#: A host that could not be asked is retried sooner than one that answered, so a
#: flaky server does not lock a crawler out for a whole day.
ROBOTS_ERROR_TTL = timedelta(hours=1)
#: Bumped when the envelope changes, so an old file is a miss rather than a
#: misread. The same reason the embedding cache puts the model in its key.
CACHE_SCHEMA = 1

RETRYABLE_STATUS: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})

#: Refused at the transport whatever a connector declares, so the bans in
#: CLAUDE.md and in the phase brief are code rather than convention. These three
#: forbid automated collection in their terms outright, so no path on them is
#: reachable and no connector can argue otherwise.
BLOCKED_HOSTS: frozenset[str] = frozenset(
    {
        "linkedin.com",
        "indeed.com",
        "glassdoor.com",
    }
)

#: Hosts whose robots.txt forbids every URL carrying a query string, enforced
#: here because ``urllib.robotparser`` cannot enforce it.
#:
#: hh's ``User-agent: *`` group is ``Disallow: *?*`` with three narrow ``Allow``
#: exceptions. CPython's ``RuleLine`` matches a rule as a literal path prefix
#: and has no wildcard support at all, so ``*?*`` never matches anything and
#: ``can_fetch(ua, "https://almaty.hh.kz/search/vacancy?text=python")`` answers
#: True — measured against the live file on 2026-09-06. Leaving the rule to the
#: robots layer would mean it was not applied at all, so the one form of URL hh
#: actually refuses is refused here instead.
QUERYLESS_HOSTS: frozenset[str] = frozenset({"hh.kz", "hh.ru"})

#: Host to path prefixes that are closed on an otherwise reachable host. hh's
#: search pages sit behind the query-string ban above and are named again here
#: so that a connector cannot reach them even without parameters; the jobseeker
#: half of ``api.hh.ru`` has answered 403 to every programmatic client since
#: April 2026, while its dictionaries stay open and are what we call it for.
#: Resumes are here for a reason worth stating: nothing else in the stack
#: refuses them. ``robots.txt`` says ``Disallow: /resume$``, and
#: ``urllib.robotparser`` quotes that into the literal prefix ``/resume%24``,
#: which prefixes no real path — so ``can_fetch`` answers True for
#: ``/resume/…`` and for ``/sitemap/resumes0.xml``, both measured against the
#: live file on 2026-09-06. Those files are living people's CVs, and the one
#: prohibition in this phase that must not depend on a connector getting a
#: regular expression right is therefore stated at the transport.
#:
#: ``/account`` is listed for a second reason on top of the obvious one that an
#: anonymous crawler has no business on a signed-in page. It is what makes
#: :func:`refuse_challenge` able to say "we were pushed here" as a fact rather
#: than a guess: with the path closed to anything this process builds, a
#: ``/account`` URL arriving at :func:`guard_redirects` can only have come from
#: a redirect the remote server chose.
BLOCKED_PATHS: dict[str, tuple[str, ...]] = {
    "hh.kz": ("/search", "/resume", "/sitemap/resumes", "/account"),
    "hh.ru": ("/search", "/resume", "/sitemap/resumes", "/account"),
    "api.hh.ru": ("/vacancies",),
}

#: Where a host sends us once it has decided we are a robot, per host rule.
#:
#: Measured on a live run on 2026-09-06: 184 requests over 253 seconds at about
#: 0.7 rps, and on request 173 a plain ``GET https://almaty.hh.kz/vacancy/…``
#: with no query string was answered ``302`` to
#: ``/account/captcha?backurl=…&state=…``.
#:
#: The whole ``/account`` subtree is listed rather than ``/account/captcha``
#: alone, because a site that has decided to challenge an anonymous reader has
#: more than one door to push it through — a captcha interstitial and a login
#: wall are the same decision wearing different clothes — and none of them is a
#: page this crawler could do anything with if it arrived there.
CHALLENGE_PATHS: dict[str, tuple[str, ...]] = {
    "hh.kz": ("/account",),
    "hh.ru": ("/account",),
}

#: Query parameters that carry a credential. Redacted before a URL reaches a log
#: line or a cache envelope — jooble puts its key in the path, and a cache file
#: is a plain file on a developer's disk.
SECRET_PARAMS: frozenset[str] = frozenset(
    {"key", "app_key", "apikey", "api_key", "token", "access_token", "secret"}
)

REDACTED = "***"

type Clock = Callable[[], float]
type Sleeper = Callable[[float], Awaitable[None]]
#: Called with the source slug just before a request goes out. The pipeline
#: wires this to the credit ledger; a retry fires it again, which is correct
#: because a retry is another billable request.
type RequestHook = Callable[[str], Awaitable[None]]


class RetryableResponseError(SourceError):
    """A 429 or a 5xx: worth trying again, unlike a 400, which is our own bug."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int,
        retry_after: float | None = None,
        source_slug: str | None = None,
    ) -> None:
        self.response_status = status_code
        self.retry_after = retry_after
        super().__init__(detail, source_slug=source_slug, response_status=status_code)


class HHChallengedError(SourceError):
    """The source answered a permitted URL with a check for robots.

    A separate class because it means something different from every other
    refusal in this module, and the difference is expensive to blur. The others
    mean *we* built a URL we are not allowed to ask for, and the person reading
    one goes and looks at URL construction. This one means we asked for exactly
    what we were allowed to ask for and the site decided we are a robot — there
    is nothing in the connector to fix, and sending somebody to look for it
    costs an afternoon.

    That is not hypothetical. On the live run of 2026-09-06 the message read
    «robots.txt на almaty.hh.kz запрещает любой URL со строкой запроса» for a
    request that was ``GET https://almaty.hh.kz/vacancy/136284790`` — no query
    string, entirely within the rules. The query string belonged to the captcha
    hh redirected us to, and the refusal to follow it was right; only the
    explanation was wrong.

    Named for hh because hh is the only host whose challenge has actually been
    measured. :data:`CHALLENGE_PATHS` is where a second one would be added.

    Two properties are load-bearing rather than incidental. It is **never
    retried** — see :func:`worth_retrying`, which says so by name rather than by
    happening to omit it from a tuple — because a challenge is a decision and
    repeating a request a site has just refused is both useless and the rudest
    thing a crawler can do. And it **never means the source is broken**, so the
    pipeline records it as an interruption: see ``SourceOutcome.challenged``.
    """

    def __init__(
        self, detail: str, *, host: str, path: str, source_slug: str | None = None
    ) -> None:
        self.challenge_host = host
        self.challenge_path = path
        super().__init__(detail, source_slug=source_slug, challenge_host=host, challenge_path=path)


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds to wait, from either form RFC 7231 allows.

    The header is an integer count of seconds *or* an HTTP date. Parsing it with
    ``int()`` alone raises on the date form, and the obvious ``except
    ValueError: return None`` silently discards exactly the header we promised
    to honour.
    """
    if value is None:
        return None
    text = value.strip()
    try:
        return max(0.0, float(int(text)))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


#: A path segment long enough to be a credential. Shorter than this and there
#: is not enough of it to be worth hiding.
MIN_SECRET_SEGMENT = 16
#: What a key is made of once the hyphens of a GUID are removed.
HEX = set("0123456789abcdefABCDEF")


def _looks_secret(segment: str) -> bool:
    """Whether a path segment is a credential rather than a route.

    Two shapes, because keys come in two: an opaque blob with no separators,
    and a GUID, which is hex and hyphens. A job slug —
    ``senior-python-developer-remote-123`` — is neither, and redacting it took
    the useful half out of every 404 and every cache filename on a crawl source.
    """
    if len(segment) < MIN_SECRET_SEGMENT or "." in segment:
        return False
    body = segment.replace("-", "")
    if not body:
        return False
    if set(body) <= HEX:
        return True
    return "-" not in segment and "_" not in segment


def redact(url: str | httpx.URL) -> str:
    """A URL safe to log or to store, with anything key-shaped replaced.

    Both shapes are covered: a key in the query string, and jooble's, which is
    a path segment. The path rule is conservative — a long opaque segment with
    no dot in it is far more likely to be a key than a route.
    """
    parts = urlsplit(str(url))
    query = "&".join(
        f"{name}={REDACTED}" if name.lower() in SECRET_PARAMS else f"{name}={value}"
        for name, _, value in (pair.partition("=") for pair in parts.query.split("&") if pair)
    )
    segments = [
        REDACTED if _looks_secret(segment) else segment for segment in parts.path.split("/")
    ]
    cleaned = parts._replace(path="/".join(segments), query=query)
    return cleaned.geturl()


def _covers(host: str, rule: str) -> bool:
    """Whether a host rule names this host or one of its subdomains."""
    return host == rule or host.endswith(f".{rule}")


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    """Whether a path is one of these prefixes or sits below it.

    Stricter than the ``startswith`` used for :data:`BLOCKED_PATHS`, and
    deliberately not shared with it: there the loose form is the point —
    ``/sitemap/resumes`` has to catch ``/sitemap/resumes0.xml``, which is a file
    name and not a directory. Here the loose form would read ``/accountancy`` as
    ``/account`` and report a page as a captcha, which is exactly the class of
    wrong message this function exists to stop making.
    """
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)


def refuse_challenge(url: httpx.URL, *, slug: str | None = None) -> None:
    """Raise when this URL is a host's check for robots rather than a page.

    Decided by the target and never by the status code. A 302 on its own is a
    perfectly ordinary thing for a site to answer — hh moves a posting to its
    successor with one all day long, and the connector depends on that working —
    so the status says nothing. Where this particular 302 points says everything.

    Meaningful only on a hop the remote server chose, which is why
    :func:`guard_redirects` is the only caller: ``/account`` is in
    :data:`BLOCKED_PATHS`, so a URL this process built is refused before a
    request is ever constructed and cannot reach here.
    """
    prefix = f"{slug}: " if slug else ""
    host = (url.host or "").lower()
    for rule, prefixes in CHALLENGE_PATHS.items():
        if _covers(host, rule) and _under(url.path, prefixes):
            # The query string is dropped on purpose: hh's carries ``backurl``
            # and an opaque ``state``, neither of which tells anybody anything
            # and both of which would then live in pipeline_run.errors.
            raise HHChallengedError(
                f"{prefix}источник ответил проверкой на робота: {host} перенаправляет "
                f"на {url.path} — прогон остановлен",
                host=host,
                path=url.path,
                source_slug=slug,
            )


def refuse_forbidden(url: httpx.URL, *, slug: str | None = None) -> None:
    """Raise unless this exact URL may be requested.

    Three separate bans, and they are separate on purpose. A whole host is
    closed when its terms forbid automated collection; a query string is closed
    when robots.txt forbids one and the robots layer cannot say so; a path is
    closed when that part of an otherwise open host is shut. Collapsing them
    into one list would mean either losing ``api.hh.ru``'s dictionaries or
    reopening its jobseeker endpoints.

    A free function rather than a method because it has to be callable with no
    connector in hand: :func:`guard_redirects` applies it to URLs that no code
    in this process chose.
    """
    prefix = f"{slug}: " if slug else ""
    host = (url.host or "").lower()

    def covers(rule: str) -> bool:
        return _covers(host, rule)

    if any(covers(blocked) for blocked in BLOCKED_HOSTS):
        raise SourceError(
            f"{prefix}обращение к {host} запрещено на уровне транспорта", source_slug=slug
        )
    # ``covers`` matches subdomains, and api.hh.ru is one of hh.ru's — but it
    # serves no robots.txt at all (404, which RFC 9309 reads as no
    # restrictions), so hh.kz's ``Disallow: *?*`` is not its rule and applying
    # it here would refuse ``/professional_roles?locale=RU`` while blaming a
    # file that host does not have. Its jobseeker endpoints are closed by
    # BLOCKED_PATHS; its dictionaries take parameters and stay open.
    if url.query and host != "api.hh.ru" and any(covers(rule) for rule in QUERYLESS_HOSTS):
        raise SourceError(
            f"{prefix}robots.txt на {host} запрещает любой URL со строкой запроса — "
            "запрос отклонён транспортом",
            source_slug=slug,
        )
    for rule, prefixes in BLOCKED_PATHS.items():
        if covers(rule) and url.path.startswith(prefixes):
            raise SourceError(
                f"{prefix}путь {url.path} на {host} закрыт на уровне транспорта",
                source_slug=slug,
            )


def worth_retrying(exc: BaseException) -> bool:
    """Whether this failure is worth spending another request on.

    Written as a named predicate rather than left to
    ``retry_if_exception_type((RetryableResponseError, httpx.TransportError))``
    for one case: :class:`HHChallengedError` must never be retried, and "it
    happens not to be in that tuple" is not a guarantee. Somebody widening the
    tuple later — to ``SourceError``, say, which is the obvious widening and
    which the challenge is a subclass of — would turn one antibot decision into
    four requests against a site that has just said no, and nothing in the code
    would have objected.
    """
    if isinstance(exc, HHChallengedError):
        return False
    return isinstance(exc, RetryableResponseError | httpx.TransportError)


async def guard_redirects(request: httpx.Request) -> None:
    """Apply the bans to every request httpx makes, not only to the first.

    The client follows redirects, and httpx resolves the chain internally: the
    URL actually fetched is chosen by the remote server. Checking only what a
    connector passed in means a single 302 reaches ``/search/vacancy?text=…``,
    ``/sitemap/resumes0.xml`` or linkedin.com with every ban and robots.txt
    bypassed — measured, not theorised. A request event hook is the one place
    that sees each hop, and raising from it stops the hop before it is sent.

    Installed on the client rather than checked afterwards on
    ``response.history``, because by then the request has already been made,
    and "we never asked for it" is the whole promise.

    The challenge check runs first, and the order is the fix rather than a
    detail. hh's captcha URL carries a query string, so ``refuse_forbidden``
    has a true thing to say about it — «robots.txt запрещает любой URL со
    строкой запроса» — and that true thing describes a URL *we* built, which
    this one is not. Asked second, it never gets the chance.
    """
    refuse_challenge(request.url)
    refuse_forbidden(request.url)


class TokenBucket:
    """Async token bucket. Both time sources are injected, and that is the point."""

    def __init__(
        self,
        limit: RateLimit,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._rate = limit.requests_per_second
        self._burst = float(limit.burst)
        self._jitter = limit.jitter_seconds
        self._clock = clock
        self._sleep = sleep
        # Injected like the clock and the sleeper, and for the same reason: a
        # test that cannot pin the randomness can only assert that the delay is
        # somewhere in a range, which is not an assertion about this code.
        self._rng = rng or random.Random()
        self._tokens = float(limit.burst)
        self._updated = clock()
        self._lock = asyncio.Lock()

    def widen(self, min_delay_seconds: float) -> None:
        """Slow to at most one request per ``min_delay_seconds``.

        Only ever slower: a source's Crawl-delay is a floor on politeness, not
        a licence to speed up past our own configured rate.
        """
        if min_delay_seconds <= 0:
            return
        self._rate = min(self._rate, 1.0 / min_delay_seconds)

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._updated) * self._rate)
        self._updated = now

    async def acquire(self) -> None:
        """Block until one request may be made.

        The lock is held *across* the wait on purpose. Releasing it while
        sleeping lets every waiter wake against the same stale token count and
        fire at once, which is precisely the bug this class exists to prevent
        and precisely what a concurrency test must catch.
        """
        async with self._lock:
            self._refill()
            if self._tokens < 1.0:
                await self._sleep((1.0 - self._tokens) / self._rate)
                self._refill()
            self._tokens -= 1.0
            # After the token, not instead of it: the bucket sets the floor on
            # the rate and this only ever adds to the wait. Drawing it inside the
            # lock keeps the spacing of concurrent callers, which is the property
            # the lock is held across the sleep for in the first place.
            if self._jitter:
                await self._sleep(self._rng.uniform(0.0, self._jitter))
                # The jitter earns no tokens. Without this line the bucket
                # refills across the extra wait, so a long pause is repaid by a
                # short next interval and the gaps end up spread around the
                # configured rate instead of above it — measured: gaps of 3.2s
                # under a 4s floor. The point is a floor with variance on top.
                self._updated = self._clock()


class CachedResponse(BaseModel):
    """A response stored on disk, plus when it was stored."""

    model_config = ConfigDict(frozen=True)

    status_code: int
    headers: dict[str, str]
    body: str
    is_base64: bool = False
    stored_at: AwareDatetime
    #: Redacted. Present only so a person can tell what a cache file holds.
    url: str
    schema_version: int = CACHE_SCHEMA

    def content(self) -> bytes:
        """The stored body as bytes."""
        return base64.b64decode(self.body) if self.is_base64 else self.body.encode("utf-8")


class ResponseCache:
    """Dev-only disk cache. Off in CI because ``HTTP_CACHE_DIR`` defaults to unset.

    Copies what the embedding cache learned: the digest key, the same-directory
    temp file plus ``os.replace`` so a crash cannot leave a half-written entry
    readable, and the rule that a corrupt or stale file is a miss to be deleted
    rather than an error to be raised. Every ``OSError`` degrades to no cache
    with a warning, because a broken cache must never break a run.
    """

    def __init__(self, directory: Path, *, now: Callable[[], datetime] | None = None) -> None:
        self._dir = directory
        self._now = now or (lambda: datetime.now(UTC))

    @staticmethod
    def key(
        method: str,
        url: httpx.URL,
        *,
        slug: str,
        body: bytes | None = None,
        salt: str | None = None,
    ) -> str:
        """Digest of everything that changes the answer.

        The slug is in the key so two sources hitting one public URL cannot
        collide. Query parameters are sorted, so ``?a=1&b=2`` and ``?b=2&a=1``
        are one entry. Credentials in headers are deliberately *not* included:
        the same request returns the same body whoever signs it, and keying on
        them would throw the whole cache away on a key rotation.

        ``salt`` is a version the caller knows and the URL does not carry. It
        exists for a source whose pages have a stable address and changing
        content, where a TTL is the wrong question: hh publishes a ``lastmod``
        per vacancy in its sitemap, and passing that here makes an edited
        posting a cache miss and an untouched one a hit for as long as the entry
        survives — which is what "invalidate on change, not on a clock" means
        when the cache is keyed rather than validated.
        """
        params = "&".join(sorted(f"{name}={value}" for name, value in url.params.multi_items()))
        material = "\x00".join(
            [
                str(CACHE_SCHEMA),
                slug,
                method.upper(),
                f"{url.scheme}://{url.netloc.decode()}{url.path}",
                params,
                (body or b"").hex(),
                salt or "",
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _path(self, key: str, slug: str) -> Path:
        # Two-level fan-out: a full crawl would otherwise put tens of thousands
        # of files in one directory.
        return self._dir / slug / key[:2] / f"{key}.json"

    def read(self, key: str, *, slug: str, ttl: timedelta) -> CachedResponse | None:
        """A fresh entry, or None. A stale or unreadable one is deleted."""
        path = self._path(key, slug)
        try:
            if not path.is_file():
                return None
            entry = CachedResponse.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            return None
        if entry.schema_version != CACHE_SCHEMA or self._now() - entry.stored_at > ttl:
            path.unlink(missing_ok=True)
            return None
        return entry

    def write(self, key: str, response: httpx.Response, *, slug: str) -> None:
        """Store a response, or give up quietly and say so."""
        try:
            body = response.text
            entry = CachedResponse(
                status_code=response.status_code,
                headers={"content-type": response.headers.get("content-type", "")},
                body=body,
                stored_at=self._now(),
                url=redact(response.request.url),
            )
        except UnicodeDecodeError:
            entry = CachedResponse(
                status_code=response.status_code,
                headers={"content-type": response.headers.get("content-type", "")},
                body=base64.b64encode(response.content).decode("ascii"),
                is_base64=True,
                stored_at=self._now(),
                url=redact(response.request.url),
            )
        path = self._path(key, slug)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(entry.model_dump_json(), encoding="utf-8")
            os.replace(temp, path)
        except OSError as exc:
            logger.warning("sources.cache_write_failed", slug=slug, error=str(exc))


class RobotsCache:
    """robots.txt per host, cached for a day, parsed with the standard library.

    ``RobotFileParser.read()`` is never called: it does synchronous urllib I/O
    and would block the event loop. We fetch with httpx and feed ``parse()``.

    **A missing file means allowed.** RFC 9309 §2.3.1 is explicit that a 4xx
    means no restrictions, and ``api.hh.ru`` serves no robots.txt at all — a
    naive "no file, no permission" rule would disable hosts that never asked
    for anything.

    **An unreachable file means blocked, for an hour.** A 5xx is not consent,
    so a crawler waits; the shorter negative lifetime keeps a flaky server from
    locking us out for a full day.
    """

    def __init__(
        self,
        *,
        ttl: timedelta = ROBOTS_TTL,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._ttl = ttl
        self._now = now or (lambda: datetime.now(UTC))
        self._entries: dict[str, tuple[urllib.robotparser.RobotFileParser | None, datetime]] = {}

    async def _parser(
        self, client: httpx.AsyncClient, url: httpx.URL, *, user_agent: str
    ) -> urllib.robotparser.RobotFileParser | None:
        host = f"{url.scheme}://{url.netloc.decode()}"
        cached = self._entries.get(host)
        if cached is not None and self._now() < cached[1]:
            return cached[0]

        parser: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
        expires = self._now() + self._ttl
        try:
            response = await client.get(
                f"{host}/robots.txt", headers={"user-agent": user_agent}, timeout=10.0
            )
        except httpx.HTTPError as exc:
            logger.warning("sources.robots_unreachable", host=host, error=str(exc))
            parser, expires = None, self._now() + ROBOTS_ERROR_TTL
        else:
            if response.status_code >= 500:
                logger.warning("sources.robots_error", host=host, status=response.status_code)
                parser, expires = None, self._now() + ROBOTS_ERROR_TTL
            elif response.status_code >= 400:
                # No file: unrestricted. Parsing an empty body says exactly that.
                assert parser is not None
                parser.parse([])
            else:
                assert parser is not None
                parser.parse(response.text.splitlines())

        self._entries[host] = (parser, expires)
        return parser

    async def allows(self, client: httpx.AsyncClient, url: httpx.URL, *, user_agent: str) -> bool:
        """Whether this URL may be fetched."""
        parser = await self._parser(client, url, user_agent=user_agent)
        if parser is None:
            return False
        return parser.can_fetch(user_agent, str(url))

    async def crawl_delay(
        self, client: httpx.AsyncClient, url: httpx.URL, *, user_agent: str
    ) -> float | None:
        """The host's requested delay between requests, when it states one."""
        parser = await self._parser(client, url, user_agent=user_agent)
        if parser is None:
            return None
        delay = parser.crawl_delay(user_agent)
        return float(delay) if delay is not None else None


class SourceHTTP:
    """What a connector actually calls. One per source, holding that source's bucket."""

    def __init__(
        self,
        source: BaseSource,
        client: "SourceClient",
        *,
        on_request: RequestHook | None = None,
    ) -> None:
        self._source = source
        self._client = client
        self._bucket = TokenBucket(source.rate_limit, clock=client.clock, sleep=client.sleep)
        self._on_request = on_request

    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        cache_ttl: timedelta | None = None,
    ) -> Any:
        """A GET whose body is JSON.

        ``Any`` with the comment CLAUDE.md asks for: the payload's shape belongs
        to the source, and declaring it here would be a guess. The connector's
        own model validates it one line later.
        """
        response = await self.request(
            "GET", url, params=params, headers=headers, cache_ttl=cache_ttl
        )
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(
                f"{self._source.slug}: ответ не является JSON", source_slug=self._source.slug
            ) from exc

    async def get_text(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        cache_ttl: timedelta | None = None,
        cache_salt: str | None = None,
    ) -> str:
        """A GET whose body is text."""
        response = await self.request(
            "GET",
            url,
            params=params,
            headers=headers,
            cache_ttl=cache_ttl,
            cache_salt=cache_salt,
        )
        return response.text

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        cache_ttl: timedelta | None = None,
        cache_salt: str | None = None,
    ) -> httpx.Response:
        """One request, with every guard the connector contract promises."""
        return await self._client.send(
            self._source,
            self._bucket,
            method,
            url,
            params=params,
            headers=headers,
            json_body=json_body,
            cache_ttl=cache_ttl if cache_ttl is not None else self._source.cache_ttl,
            cache_salt=cache_salt,
            on_request=self._on_request,
        )


class SourceClient:
    """Process-wide owner of the connection pool, the robots cache and the disk cache."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        cache: ResponseCache | None = None,
        robots: RobotsCache | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        # Borrowed versus owned, exactly as ollama.py decided it.
        self._owns_client = client is None
        self._client = client
        if client is not None:
            # Appended rather than assigned: the client belongs to the caller
            # and may carry hooks of its own. A borrowed client that skipped
            # this would be a way to reach a banned host by construction.
            client.event_hooks.setdefault("request", []).append(guard_redirects)
        self._cache = cache if cache is not None else _default_cache()
        self._robots = robots if robots is not None else RobotsCache()
        self.clock = clock
        self.sleep = sleep

    def bind(self, source: BaseSource, *, on_request: RequestHook | None = None) -> SourceHTTP:
        """A per-source facade holding that source's own bucket."""
        return SourceHTTP(source, self, on_request=on_request)

    @property
    def http(self) -> httpx.AsyncClient:
        """The pool, built on first use."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=settings.http_timeout,
                headers={"user-agent": settings.user_agent},
                follow_redirects=True,
                # Retries are tenacity's below, so the policy is ours, is
                # logged and is testable — the same reason the Anthropic client
                # is built with max_retries=0.
                transport=httpx.AsyncHTTPTransport(retries=0),
                event_hooks={"request": [guard_redirects]},
            )
        return self._client

    async def aclose(self) -> None:
        """Close the pool — only when we built it. A borrowed client is left open."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def send(
        self,
        source: BaseSource,
        bucket: TokenBucket,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        cache_ttl: timedelta | None = None,
        cache_salt: str | None = None,
        on_request: RequestHook | None = None,
    ) -> httpx.Response:
        """The whole request path, in the order the guards have to run."""
        target = httpx.URL(url)
        if params:
            target = target.copy_merge_params(params)
        self._refuse_forbidden(source, target)

        if source.access_mode is AccessMode.CRAWL:
            await self._check_robots(source, target, bucket)

        cached = self._read_cache(source, method, target, cache_ttl, cache_salt)
        if cached is not None:
            return cached

        response = await self._send_with_retries(
            source, bucket, method, target, headers, json_body, on_request
        )
        # cache_ttl gates the write as well as the read. Without it a connector
        # that passes zero to opt out still had files written that nothing could
        # ever read back — a cache that only grows.
        if (
            response.status_code == 200
            and self._cache is not None
            and method.upper() == "GET"
            and cache_ttl
        ):
            key = ResponseCache.key(method, target, slug=source.slug, salt=cache_salt)
            self._cache.write(key, response, slug=source.slug)
        return response

    def _refuse_forbidden(self, source: BaseSource, url: httpx.URL) -> None:
        """Refuse what no connector is allowed to ask for, before anything else runs."""
        refuse_forbidden(url, slug=source.slug)

    async def _check_robots(self, source: BaseSource, url: httpx.URL, bucket: TokenBucket) -> None:
        allowed = await self._robots.allows(self.http, url, user_agent=settings.user_agent)
        if not allowed:
            raise SourceError(
                f"{source.slug}: robots.txt запрещает {redact(url)}", source_slug=source.slug
            )
        delay = await self._robots.crawl_delay(self.http, url, user_agent=settings.user_agent)
        if delay:
            bucket.widen(delay)

    def _read_cache(
        self,
        source: BaseSource,
        method: str,
        url: httpx.URL,
        cache_ttl: timedelta | None,
        cache_salt: str | None = None,
    ) -> httpx.Response | None:
        if self._cache is None or method.upper() != "GET" or not cache_ttl:
            return None
        key = ResponseCache.key(method, url, slug=source.slug, salt=cache_salt)
        entry = self._cache.read(key, slug=source.slug, ttl=cache_ttl)
        if entry is None:
            return None
        logger.debug("sources.cache_hit", slug=source.slug, url=redact(url))
        # Returned before the bucket is touched: a cache hit is not a request,
        # and spending a token on one would make the dev cache pointless.
        return httpx.Response(
            status_code=entry.status_code,
            headers=entry.headers,
            content=entry.content(),
            request=httpx.Request(method, url),
        )

    async def _send_with_retries(
        self,
        source: BaseSource,
        bucket: TokenBucket,
        method: str,
        url: httpx.URL,
        headers: Mapping[str, str] | None,
        json_body: Any,
        on_request: RequestHook | None,
    ) -> httpx.Response:
        backoff = wait_exponential_jitter(initial=1, max=30)

        def wait_policy(state: RetryCallState) -> float:
            """Prefer the server's own number, capped."""
            exc = state.outcome.exception() if state.outcome else None
            if isinstance(exc, RetryableResponseError) and exc.retry_after is not None:
                return min(exc.retry_after, MAX_RETRY_AFTER_SECONDS)
            return backoff(state)

        retrying = AsyncRetrying(
            retry=retry_if_exception(worth_retrying),
            stop=stop_after_attempt(MAX_ATTEMPTS),
            wait=wait_policy,
            sleep=self.sleep,
            reraise=True,
        )
        last: httpx.Response | None = None
        async for attempt in retrying:
            with attempt:
                # Inside the attempt, never around the loop: a 429 means we are
                # going too fast, and letting the retries bypass the limiter
                # would be exactly the wrong response.
                await bucket.acquire()
                if on_request is not None:
                    # Before the request leaves, not after it succeeds: a 500
                    # has already cost a credit, and counting successes walks
                    # past the limit into a 429 that looks unexplainable.
                    await on_request(source.slug)
                # Set per request rather than only as a default on the client
                # we build: with a borrowed client, robots.txt was being asked
                # for under our contactable name while the page itself went out
                # as python-httpx — exactly the mismatch a site operator flags.
                sent = {"user-agent": settings.user_agent, **dict(headers or {})}
                last = await self.http.request(method, url, headers=sent, json=json_body)
                self._raise_for_status(source, last)
        assert last is not None
        return last

    def _raise_for_status(self, source: BaseSource, response: httpx.Response) -> None:
        if response.status_code in RETRYABLE_STATUS:
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            if retry_after is not None and retry_after > MAX_RETRY_AFTER_SECONDS:
                # Too long to wait inside a run, but the number is the source's
                # own and the caller can reschedule against it.
                raise RateLimitError(
                    f"{source.slug}: просит подождать {retry_after:.0f} с",
                    source_slug=source.slug,
                    retry_after=retry_after,
                )
            raise RetryableResponseError(
                f"{source.slug}: HTTP {response.status_code}",
                status_code=response.status_code,
                retry_after=retry_after,
                source_slug=source.slug,
            )
        if response.status_code >= 400:
            # Our request, our bug. Retrying it just repeats the mistake.
            raise SourceError(
                f"{source.slug}: HTTP {response.status_code} на {redact(response.request.url)}",
                source_slug=source.slug,
                response_status=response.status_code,
            )


def _default_cache() -> ResponseCache | None:
    """The dev cache, when one is configured. Off in CI by doing nothing."""
    directory = settings.http_cache_dir
    return ResponseCache(directory) if directory is not None else None


_client: SourceClient | None = None


def get_client() -> SourceClient:
    """The process-wide client, for the same reason ``get_router`` is a singleton."""
    global _client
    if _client is None:
        _client = SourceClient()
    return _client


def reset_client() -> None:
    """Drop the singleton without closing it. For tests."""
    global _client
    _client = None


async def close_client() -> None:
    """Close the pool at shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
