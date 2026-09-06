"""What every job source looks like from the outside.

Deliberately shaped like ``app/llm/base.py``: a small declarative surface, the
contracts as Pydantic models, and behaviour the pipeline can drive without
knowing which connector it is holding. Adding a source means adding one file to
this package. Nothing outside it changes — that is the requirement the whole
module is arranged around, and every alternative that failed it is recorded in
``registry.py``.

Two decisions here are load-bearing and look like mistakes if you skim them.

``SearchQuery.keywords`` is a tuple, not a list. The planner crosses skill
groups with placements and produces the same query more than once; collapsing
those needs the model to be hashable, and a list field makes ``hash()`` raise
even under ``frozen=True``.

``BaseSource.search`` is not declared ``async def``. Implementations are async
generators, so calling one returns an ``AsyncIterator`` directly; an
``async def`` signature would type the call as a coroutine and force every
caller to write ``async for posting in await source.search(query)``.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.core.config import settings
from app.db.enums import EmploymentType, RemoteType
from app.schemas.common import CountryCode, LanguageCode
from app.schemas.vacancy import MAX_POSTED_WITHIN_DAYS

if TYPE_CHECKING:  # pragma: no cover - the runtime import would be a cycle
    from app.sources.http import SourceHTTP


#: Answers "which of these external ids do we already hold for this source?".
#: Installed by the pipeline; see :meth:`BaseSource.with_known_ids`.
type KnownIds = Callable[[Sequence[str]], Awaitable[set[str]]]

#: Reads back what this source last stored under a key of its own choosing.
#: Installed by the pipeline; see :meth:`BaseSource.with_state`.
type StateLoad = Callable[[str], Awaitable[dict[str, Any] | None]]
#: Stores it. Called at a point the connector chooses, which is the whole
#: design: only the connector knows when its position is safe to advance.
type StateSave = Callable[[str, dict[str, Any]], Awaitable[None]]


class AccessMode(StrEnum):
    """How a source is reached, which decides whether robots.txt applies to it."""

    #: A documented endpoint, called under its published terms, normally with a
    #: credential its operator issued to us. Those terms govern, not the
    #: crawler-scope file that sits at the root of the vendor's website.
    API = "api"
    #: Pages authored for people to read in a browser. robots.txt governs.
    CRAWL = "crawl"


class SourceUnavailable(StrEnum):
    """Why a registered source is not going to run right now."""

    MISSING_CREDENTIALS = "missing_credentials"
    DISABLED_BY_CONFIG = "disabled_by_config"
    #: The upstream closed a door we used to have.
    UPSTREAM_CLOSED = "upstream_closed"
    #: Ran too recently for this source's published terms. Not an error.
    COOLING_DOWN = "cooling_down"
    #: Today's metered allowance is spent. Resets at midnight UTC.
    QUOTA_EXHAUSTED = "quota_exhausted"


class Unavailable(BaseModel):
    """A machine-readable reason plus a sentence a person can act on."""

    model_config = ConfigDict(frozen=True)

    code: SourceUnavailable
    detail: str
    #: Names of the credentials that are missing. Names only — never a value, a
    #: prefix or a length, all three of which narrow a key for whoever reads
    #: the API response or the log line.
    missing_credentials: tuple[str, ...] = ()
    #: When it is worth asking again. Only the two waiting states fill this.
    retry_after: AwareDatetime | None = None


class RateLimit(BaseModel):
    """Steady rate and burst allowance for one source, enforced in ``http.py``."""

    model_config = ConfigDict(frozen=True)

    requests_per_second: float = Field(default=1.0, gt=0, le=50)
    burst: int = Field(default=1, ge=1, le=100)


class SearchQuery(BaseModel):
    """One search a source is asked to run.

    Frozen and hashable on purpose; see the module docstring.
    """

    model_config = ConfigDict(frozen=True)

    keywords: tuple[str, ...] = ()
    area: str | None = Field(default=None, max_length=100)
    country: CountryCode | None = None
    remote: RemoteType | None = None
    salary_min: Decimal | None = Field(default=None, ge=0)
    posted_within_days: int | None = Field(default=None, ge=1, le=MAX_POSTED_WITHIN_DAYS)
    employment_type: EmploymentType | None = None
    language: LanguageCode | None = None
    #: Ceiling on the postings this one query may yield, so a single broad
    #: search cannot consume a whole run's budget.
    limit: int = Field(default=200, ge=1, le=5000)

    @field_validator("keywords", mode="before")
    @classmethod
    def _clean_keywords(cls, value: Any) -> Any:
        """Accept a list, store a tuple, drop blanks and repeats.

        Order is preserved rather than sorted: these words become the query
        string a source ranks by relevance against, and relevance is not
        symmetric in the order of the terms.
        """
        if not isinstance(value, list | tuple):
            return value
        seen: set[str] = set()
        cleaned: list[str] = []
        for item in value:
            word = str(item).strip()
            if not word or word.casefold() in seen:
                continue
            seen.add(word.casefold())
            cleaned.append(word)
        return tuple(cleaned)


class RawPosting(BaseModel):
    """One posting exactly as a source returned it.

    Every length here mirrors the column it will be written to. A source that
    emits a 400-character identifier then fails at the connector boundary with
    the field named, rather than a hundred postings later inside ``bulk_upsert``
    with a truncation error that loses the whole batch.

    Frozen, but never hashed: ``raw`` is a dict, so ``hash()`` raises.
    Deduplicate on :attr:`key`.
    """

    model_config = ConfigDict(frozen=True)

    source_slug: str = Field(min_length=1, max_length=50)
    external_id: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=1000)
    title: str = Field(min_length=1, max_length=300)
    company: str | None = Field(default=None, max_length=200)
    description: str | None = None
    #: The untouched payload, so normalisation can be re-run without refetching.
    #: ``Any`` because the shape belongs to the source and is not ours to declare
    #: until the connector's own model has validated it.
    raw: dict[str, Any] = Field(default_factory=dict)
    fetched_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def key(self) -> tuple[str, str]:
        """``(source_slug, external_id)`` — the natural key of a vacancy_source row."""
        return (self.source_slug, self.external_id)

    def with_detail(self, *, description: str | None, raw: dict[str, Any] | None = None) -> Self:
        """A copy carrying the detail page's body. Never mutates in place."""
        return self.model_copy(
            update={"description": description, "raw": raw if raw is not None else self.raw}
        )


class BaseSource(ABC):
    """A job source, as a plugin.

    Subclass it, set the class attributes, implement :meth:`search`, decorate
    with ``@register_source``. The pipeline only ever holds the registry.
    """

    slug: ClassVar[str]
    name: ClassVar[str]
    regions: ClassVar[tuple[str, ...]] = ()
    requires_auth: ClassVar[bool] = False
    #: Keys looked up in ``settings.source_credentials``. Declaring them is all
    #: a connector has to do for :meth:`is_configured` to answer correctly.
    required_credentials: ClassVar[tuple[str, ...]] = ()
    rate_limit: ClassVar[RateLimit] = RateLimit()
    needs_detail_fetch: ClassVar[bool] = False
    #: CRAWL by default, so a new connector is checked against robots.txt unless
    #: its author states in code that it is calling a documented API.
    access_mode: ClassVar[AccessMode] = AccessMode.CRAWL
    #: Mandatory when access_mode is API; the registry refuses to register
    #: without it. Pairs with a class docstring summarising the terms, which the
    #: registry also insists on — a link nobody read is the same blindness the
    #: robots.txt exemption was meant to avoid, just facing the other way.
    terms_url: ClassVar[str | None] = None
    #: Credit line the vendor requires next to a posting from this source, shown
    #: by the dashboard. Not decoration: for remotive it is a condition of use.
    attribution: ClassVar[str | None] = None
    #: Shortest gap between two runs, taken from the source's published terms.
    min_interval: ClassVar[timedelta] = timedelta(0)
    #: Metered requests permitted per UTC day, or None when the source is not
    #: metered. Counted in ``source_quota``.
    daily_quota: ClassVar[int | None] = None
    #: Dev-only disk cache lifetime for this source's responses.
    cache_ttl: ClassVar[timedelta] = timedelta(hours=1)

    def __init__(self, *, http: "SourceHTTP | None" = None) -> None:
        self._http = http
        self._known_ids: KnownIds | None = None
        self._state_load: StateLoad | None = None
        self._state_save: StateSave | None = None

    @property
    def http(self) -> "SourceHTTP":
        """The bound client."""
        if self._http is None:  # pragma: no cover - a wiring mistake, not a path
            raise RuntimeError(f"source {self.slug!r} was built without an HTTP client")
        return self._http

    def bind(self, http: "SourceHTTP") -> Self:
        """Attach a client. Returns self so the registry can build in one line."""
        self._http = http
        return self

    def with_known_ids(self, lookup: "KnownIds | None") -> Self:
        """Install the hook that answers "which of these have we already got?".

        A source with no usable date filter serves old postings mixed with new
        ones, so the only way to stop paying for pages of things we already hold
        is to recognise them mid-pagination. The answer lives in the database
        and a connector must not reach for it, so the pipeline passes a closure
        instead. Absent, a connector simply paginates to its own limit.
        """
        self._known_ids = lookup
        return self

    async def already_known(self, external_ids: Sequence[str]) -> set[str]:
        """Which of these this source has given us before. Empty without the hook."""
        if self._known_ids is None or not external_ids:
            return set()
        return await self._known_ids(list(external_ids))

    def with_state(self, load: "StateLoad | None", save: "StateSave | None") -> Self:
        """Install the hooks that remember where a crawl got to.

        Same seam as :meth:`with_known_ids`, for the same reason: the answer
        lives in the database, a connector must not reach for it, so the
        pipeline passes closures instead. Absent — in a unit test, or in a
        connector that has no position to remember — a source simply starts
        from the beginning every time, which is correct for every source that
        fetches one bounded feed.

        A source large enough to need slicing cannot work that way. hh's corpus
        is roughly fourteen thousand pages per city and its sitemap dates every
        entry, so a run covers the slice that changed and has to record, per
        sitemap file, how far it actually got.
        """
        self._state_load = load
        self._state_save = save
        return self

    async def state_get(self, key: str) -> dict[str, Any] | None:
        """What this source stored under ``key`` last time, or None."""
        if self._state_load is None:
            return None
        return await self._state_load(key)

    async def state_set(self, key: str, value: dict[str, Any]) -> None:
        """Store this source's position. A no-op when no store is installed."""
        if self._state_save is None:
            return
        await self._state_save(key, value)

    # ── availability ──────────────────────────────────────────────────

    def missing_credentials(self) -> tuple[str, ...]:
        """Declared credential keys that are absent from the settings."""
        configured = settings.source_credentials
        return tuple(key for key in self.required_credentials if key not in configured)

    def is_configured(self) -> bool:
        """Whether every declared credential is present. Makes no request."""
        return not self.missing_credentials()

    def unavailable(self) -> Unavailable | None:
        """Why this source will not run, or None when it will.

        Computed rather than stored: there is no ``source`` table, and adding
        one would put a connector's on/off state in a row that no code review
        ever sees.
        """
        missing = self.missing_credentials()
        if missing:
            return Unavailable(
                code=SourceUnavailable.MISSING_CREDENTIALS,
                detail=(
                    f"Источник «{self.name}» не настроен: не хватает ключей "
                    f"({', '.join(missing)}). Добавь их в SOURCE_CREDENTIALS."
                ),
                missing_credentials=missing,
            )
        return None

    # ── scheduling ────────────────────────────────────────────────────

    def cooldown_until(self, last_started_at: datetime | None) -> datetime | None:
        """When this source may next be asked, or None when it may be now."""
        if last_started_at is None or not self.min_interval:
            return None
        return last_started_at + self.min_interval

    def is_due(self, last_started_at: datetime | None, *, now: datetime | None = None) -> bool:
        """Whether ``min_interval`` has elapsed since the last *attempt*.

        Attempts, not successes: a run that failed still spent the request. A
        source permitted four calls a day would otherwise be retried at full
        speed for as long as it kept failing, which is the opposite of what a
        429 or a daily cap is asking for.

        Takes the timestamp as an argument rather than querying for it, so the
        policy is testable with no database — which matters here, because a
        skipped test fails the build.
        """
        until = self.cooldown_until(last_started_at)
        return until is None or (now or datetime.now(UTC)) >= until

    # ── fetching ──────────────────────────────────────────────────────

    @abstractmethod
    def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]:
        """Yield the postings this query finds.

        NOT ``async def`` — see the module docstring. Do not "fix" it.
        """

    async def fetch_detail(self, posting: RawPosting) -> RawPosting:
        """Fill in what the list endpoint left out. Identity unless overridden."""
        return posting

    async def search_batch(self, queries: Sequence[SearchQuery]) -> AsyncIterator[RawPosting]:
        """Run several queries, yielding each posting exactly once.

        Both deduplications are ordinary behaviour rather than defence against
        a broken source: the planner emits the same query from two different
        skills, and a paginated feed serves overlapping pages — arbeitnow's
        pages two and three were measured sharing 17 of 175 entries.
        """
        seen: set[tuple[str, str]] = set()
        for query in dict.fromkeys(queries):
            async for posting in self.search(query):
                if posting.key in seen:
                    continue
                seen.add(posting.key)
                yield posting
