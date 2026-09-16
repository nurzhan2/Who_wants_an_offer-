"""JSearch (RapidAPI): Google for Jobs, reachable under a contract.

Every rule below came from four live calls against the Basic tier, not from the
documentation, and several of them contradict what the documentation implies.
They are written down as behaviour because each one, left out, produces a
connector that looks like it works.

``date_posted`` breaks the result set. ``query=Python developer Almaty,
country=kz, date_posted=week`` returned nothing; the identical call with
``date_posted=all`` returned ten postings. Seven of those ten carry a null
``job_posted_at``, and the date filter drops everything it cannot date. So the
parameter is pinned to ``all`` and freshness is decided on our side from
``first_seen_at``.

``job_id`` is not stable. The same CPI Card Group posting came back with two
different ``job_id`` values across two runs, because the request context is
encoded into it, while ``job_uid`` was identical both times. Using ``job_id``
as the external id would mean the upsert never matches and every run inserts
the whole page again — and at roughly 400 characters it also overflows
``vacancy_source.external_id``, which would fail the entire batch rather than
one row.

The payload is incomplete and, in places, wrong: ``job_highlights`` was empty
for all ten, ``job_salary`` null for nine, ``job_city``/``job_country`` null for
four while ``job_location`` was populated, and one posting with "(Remote)" in
its title reported ``job_is_remote=false``. Location and remoteness are
therefore derived from ``job_location`` and the title, not trusted from the
flags. ``job_description`` is the one field that is reliably complete.

**The allowance is monthly, and it was never being spent.** Measured
2026-09-16: a response carries ``x-ratelimit-requests-limit: 200`` with a reset
eighteen days out — two hundred requests a month, not the hundred a day this
class used to declare. At a hundred a day the month would have gone in two
runs. It went in none: the key sat in ``RAPIDAPI_KEY`` while this class looked
only in ``SOURCE_CREDENTIALS``, so the source reported itself unconfigured, and
the 18 "jsearch" rows in the database were ``scripts/seed.py``'s. Now the key is
read from either place, and a run spends a fixed slice of the month:

* :data:`PAGES_PER_RUN` pages, at most once every :data:`RUN_EVERY`, which is
  :data:`DAILY_ALLOWANCE` a day and about 180 of the 200 a month;
* one page per planned search, taking the searches in turn across runs, so a
  plan of eight searches is covered in a few days instead of its first search
  being paginated ten deep while the other seven are never asked;
* each search keeps its cursor, so when its turn comes again it reads the page
  after the one it read last — breadth first, then depth.

JSearch has no area parameter and defaults ``country`` to ``us``, so the place
goes into the query text and the country comes from ``jsearch_countries.yaml``.
"""

import hashlib
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import settings
from app.core.exceptions import SourceError
from app.core.logging import get_logger
from app.db.enums import RemoteType
from app.schemas.crawl import SavedState, SearchPreview, SearchUse
from app.sources.base import (
    PREVIEW_TERMS,
    AccessMode,
    BaseSource,
    RateLimit,
    RawPosting,
    SearchQuery,
)
from app.sources.registry import register_source

if TYPE_CHECKING:  # pragma: no cover - the runtime import would be a cycle
    from app.sources.http import SourceHTTP

logger = get_logger(__name__)

BASE_URL = "https://jsearch.p.rapidapi.com/search-v2"
API_HOST = "jsearch.p.rapidapi.com"

#: Pinned. See the module docstring: any other value silently drops every
#: posting whose date the upstream does not know, which was seven in ten.
DATE_POSTED = "all"

#: Countries where an English query returns nothing. ``country=kz`` with no
#: language produced zero results; with ``language=ru`` it produced ten.
LANGUAGE_BY_COUNTRY: Mapping[str, str] = {
    "kz": "ru",
    "ru": "ru",
    "by": "ru",
    "kg": "ru",
    "uz": "ru",
    "tj": "ru",
    "am": "ru",
    "az": "ru",
}
DEFAULT_LANGUAGE = "en"

#: Stop paginating a query once this share of a page is already ours. Without a
#: usable date filter the feed mixes old postings into every page, so listing on
#: would spend credits to rediscover what we hold.
KNOWN_SHARE_STOP = 0.7
#: Pages are ten postings each and each page costs one credit, so a runaway loop
#: is a runaway bill. This is the backstop under any per-query limit.
MAX_PAGES = 10

#: The plan's allowance, read off ``x-ratelimit-requests-limit`` on 2026-09-16.
#: A month, not a day: the same response reset eighteen days later.
MONTHLY_QUOTA = 200
#: The most any one day may spend. Thirty-one days of it stay under
#: :data:`MONTHLY_QUOTA`, with a few requests left for a probe by hand.
DAILY_ALLOWANCE = MONTHLY_QUOTA // 31
#: The shortest gap between two runs, and so how the day is sliced.
RUN_EVERY = timedelta(hours=12)
#: Pages one run may buy. Runs this size, no closer than :data:`RUN_EVERY`,
#: add up to the daily allowance.
PAGES_PER_RUN = max(1, DAILY_ALLOWANCE * int(RUN_EVERY.total_seconds()) // 86_400)

#: Where the rotation lives in ``source_state``.
ROTATION_KEY = "rotation"
#: The longest request text sent. A title and a city fit many times over.
MAX_QUERY_CHARS = 200

#: Place-to-country table; see the file's own header.
COUNTRIES_FILE = Path(__file__).with_name("jsearch_countries.yaml")

#: Words that mean the posting is remote regardless of what ``job_is_remote``
#: says. Checked against the title, which is where the truth was.
REMOTE_MARKERS = ("remote", "anywhere", "удалён", "удален", "удалённо")

#: ``job_location`` arrives as "Almaty, Kazakhstan • via LinkedIn"; everything
#: from the bullet on is the publisher, not the place.
LOCATION_TAIL = "•"


class JSearchJob(BaseModel):
    """One posting as JSearch returns it.

    Extras are allowed through on purpose: the payload carries dozens of fields
    that phase 4 will want, and they travel to ``vacancy_source.raw`` untouched.
    """

    model_config = ConfigDict(extra="allow")

    #: The stable identifier. ``job_id`` is deliberately not used; see above.
    job_uid: str = Field(min_length=1)
    job_title: str = Field(min_length=1)
    employer_name: str | None = None
    job_apply_link: str | None = None
    job_description: str | None = None
    job_location: str | None = None
    job_city: str | None = None
    job_country: str | None = None
    job_is_remote: bool | None = None


class JSearchPage(BaseModel):
    """One page of results plus the cursor for the next.

    Pagination is by cursor, not by page number: there is no ``page``
    parameter. An empty cursor asks for the first page and a null one means
    there are no more.
    """

    model_config = ConfigDict(extra="allow")

    jobs: list[JSearchJob] = Field(default_factory=list)
    cursor: str | None = None


def looks_remote(title: str, location: str | None, flag: bool | None) -> RemoteType:
    """Decide remoteness from the text, with the flag as the weakest witness.

    A posting titled "... (Remote)" arrived with ``job_is_remote=false``, so the
    flag can only ever add remoteness here, never remove it.
    """
    haystack = f"{title} {location or ''}".casefold()
    if any(marker in haystack for marker in REMOTE_MARKERS):
        return RemoteType.FULL
    return RemoteType.FULL if flag else RemoteType.NO


def clean_location(value: str | None) -> str | None:
    """The place part of ``job_location``, without the publisher tail."""
    if not value:
        return None
    head = value.split(LOCATION_TAIL, 1)[0].strip()
    return head or None


class CountryAliases(BaseModel):
    """One country and the spellings of places inside it."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=2, max_length=2)
    aliases: tuple[str, ...] = ()


def load_countries(path: Path | None = None) -> tuple[CountryAliases, ...]:
    """The place-to-country table, read on demand rather than at import."""
    path = path or COUNTRIES_FILE
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return tuple(CountryAliases.model_validate(entry) for entry in raw.get("countries", []))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise SourceError(
            f"jsearch: не читается {path.name} со списком стран: {exc}", source_slug="jsearch"
        ) from exc


def country_for(area: str | None, countries: Sequence[CountryAliases]) -> str | None:
    """The ISO code for a free-text place, or None when no entry names it."""
    if not area:
        return None
    place = area.casefold()
    for country in countries:
        if any(alias.casefold() in place for alias in country.aliases):
            return country.code.lower()
    return None


class PlannedSearch(BaseModel):
    """One request text and the country it is asked about."""

    model_config = ConfigDict(frozen=True)

    text: str
    country: str | None = None

    @property
    def key(self) -> str:
        """A short stable id for the rotation record.

        A digest rather than the text: the text is the owner's words, and the
        state table is read by the overview.
        """
        digest = hashlib.sha256(f"{self.text}|{self.country or ''}".casefold().encode())
        return digest.hexdigest()[:16]

    @property
    def label(self) -> str:
        """How a person reads this search."""
        return f"{self.text} · {self.country.upper()}" if self.country else self.text


class SearchCursor(BaseModel):
    """Where one search got to: the cursor of its next page, and that page's number."""

    cursor: str | None = None
    page: int = Field(default=1, ge=1)


class Rotation(BaseModel):
    """Which search takes the next turn, and where each search got to."""

    offset: int = Field(default=0, ge=0)
    cursors: dict[str, SearchCursor] = Field(default_factory=dict)


def planned_searches(
    queries: Sequence[SearchQuery], countries: Sequence[CountryAliases]
) -> tuple[PlannedSearch, ...]:
    """The plan as JSearch will see it: one text per distinct search, in plan order.

    The place goes into the text, since there is no area parameter, and a
    remote-only slot says so in words. Two plan entries that render to the same
    text and country are one request.
    """
    searches: dict[PlannedSearch, None] = {}
    for query in queries:
        terms = " ".join(query.keywords).strip()
        if not terms:
            continue
        if query.area:
            text = f"{terms} {query.area}"
        elif query.remote is RemoteType.FULL:
            text = f"{terms} remote"
        else:
            text = terms
        country = (query.country or "").lower() or country_for(query.area, countries)
        searches.setdefault(PlannedSearch(text=text[:MAX_QUERY_CHARS], country=country), None)
    return tuple(searches)


def in_turn(searches: Sequence[PlannedSearch], offset: int) -> list[PlannedSearch]:
    """The searches, starting from the one whose turn it is."""
    if not searches:
        return []
    start = offset % len(searches)
    return [*searches[start:], *searches[:start]]


def parse_rotation(value: dict[str, Any] | None) -> Rotation:
    """A stored rotation, or a fresh one when there is none or it does not parse.

    Starting the turn order over is the whole cost of a record this class can
    no longer read, so it is logged and not raised.
    """
    if not value:
        return Rotation()
    try:
        return Rotation.model_validate(value)
    except ValidationError:
        logger.warning("sources.jsearch.rotation_unreadable")
        return Rotation()


def stored_rotation(stored: Sequence[SavedState]) -> Rotation:
    """The rotation among this source's saved rows, or a fresh one."""
    return parse_rotation(next((row.value for row in stored if row.key == ROTATION_KEY), None))


@register_source
class JSearchSource(BaseSource):
    """Google for Jobs through RapidAPI, under the Basic plan.

    Limits: 200 requests a month (measured; see the module docstring), five per
    second, ten postings per page, and every page costs one request. This class
    spends at most :data:`DAILY_ALLOWANCE` a day, counted in ``source_quota``;
    when that is spent the source reports itself unavailable rather than
    failing the run.

    Attribution is not required by the plan, but the postings are third-party
    listings republished from their original boards, so the apply link is always
    preserved and always the one the user is sent to.

    Restrictions: results may not be redistributed as a competing job board.
    This project shows them to the one candidate whose profile produced them,
    which is the use the plan is sold for. Terms: see ``terms_url``.
    """

    slug = "jsearch"
    name = "JSearch (Google for Jobs)"
    regions = ("global",)
    requires_auth = True
    required_credentials = ("jsearch.rapidapi_key",)
    access_mode = AccessMode.API
    terms_url = "https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch"
    # Two, though the plan allows five. The daily budget is the binding
    # constraint, not the per-second one, and leaving headroom costs nothing.
    rate_limit = RateLimit(requests_per_second=2.0, burst=2)
    daily_quota = DAILY_ALLOWANCE
    min_interval = RUN_EVERY

    def __init__(self, *, http: "SourceHTTP | None" = None) -> None:
        super().__init__(http=http)
        self._countries: tuple[CountryAliases, ...] | None = None

    @property
    def countries(self) -> tuple[CountryAliases, ...]:
        """The place-to-country table, read once per instance."""
        if self._countries is None:
            self._countries = load_countries()
        return self._countries

    def missing_credentials(self) -> tuple[str, ...]:
        """Accept the key where ``.env.example`` has always told people to put it.

        ``RAPIDAPI_KEY`` predates ``SOURCE_CREDENTIALS``; a key set there and
        ignored here is how this source never ran once.
        """
        if settings.rapidapi_key is not None:
            return ()
        return super().missing_credentials()

    def _headers(self) -> dict[str, str]:
        """Auth headers, read at call time so a test can set the credential."""
        key = settings.source_credentials.get("jsearch.rapidapi_key") or settings.rapidapi_key
        if key is None:  # pragma: no cover - guarded by is_configured upstream
            raise SourceError("jsearch: не настроен ключ RapidAPI", source_slug=self.slug)
        return {"X-RapidAPI-Key": key.get_secret_value(), "X-RapidAPI-Host": API_HOST}

    def publisher_of(self, raw: dict[str, Any]) -> str | None:
        """The board Google found the posting on: LinkedIn, a regional site, a company."""
        value = raw.get("job_publisher")
        if not isinstance(value, str):
            return None
        return value.strip()[:200] or None

    def preview_search(
        self, queries: Sequence[SearchQuery], stored: Sequence[SavedState]
    ) -> SearchPreview:
        """The request texts, in the order the next runs will send them."""
        ordered = in_turn(planned_searches(queries, self.countries), stored_rotation(stored).offset)
        hours = int(RUN_EVERY.total_seconds() // 3600)
        return SearchPreview(
            use=SearchUse.QUERY,
            terms=[search.label for search in ordered[:PREVIEW_TERMS]],
            more=max(0, len(ordered) - PREVIEW_TERMS),
            note=(
                f"Тариф — {MONTHLY_QUOTA} запросов в месяц. Прогон тратит {PAGES_PER_RUN}: "
                f"по странице на запрос, по очереди, не чаще раза в {hours} ч — "
                f"до {DAILY_ALLOWANCE} в сутки."
            ),
        )

    async def search_batch(self, queries: Sequence[SearchQuery]) -> AsyncIterator[RawPosting]:
        """Spend this run's pages across the plan: one page per search, in turn.

        The base implementation paginates each query in full before starting the
        next, which on a metered source hands the first search the whole month.
        Here each search gets one page per turn, the turn order carries over
        between runs, and a search with more pages continues from its saved
        cursor when its turn comes again.
        """
        searches = planned_searches(queries, self.countries)
        limit = max((query.limit for query in queries), default=0)
        if not searches or not limit:
            return
        rotation = parse_rotation(await self.state_get(ROTATION_KEY))
        headers = self._headers()
        seen: set[str] = set()
        turn = rotation.offset

        for _ in range(PAGES_PER_RUN):
            search = searches[turn % len(searches)]
            turn += 1
            position = rotation.cursors.get(search.key, SearchCursor())
            payload = await self.http.get_json(
                BASE_URL, params=self._params(search, position.cursor), headers=headers
            )
            page = self._parse(payload)
            postings = [posting for job in page.jobs if (posting := self.to_posting(job))]
            known = await self.already_known([posting.external_id for posting in postings])
            logger.info(
                "sources.jsearch.page",
                search=search.key,
                page=position.page,
                jobs=len(page.jobs),
                known=len(known),
            )
            # A search that ran out starts again from the top next time: with no
            # usable date filter, the head of the results is where new postings
            # appear.
            if not page.jobs or not page.cursor or position.page >= MAX_PAGES:
                rotation.cursors[search.key] = SearchCursor()
            else:
                rotation.cursors[search.key] = SearchCursor(
                    cursor=page.cursor, page=position.page + 1
                )
            for posting in postings:
                if posting.external_id in seen:
                    continue
                seen.add(posting.external_id)
                yield posting
                if len(seen) >= limit:
                    break
            if len(seen) >= limit:
                break

        wanted = {search.key for search in searches}
        await self.state_set(
            ROTATION_KEY,
            Rotation(
                offset=turn % len(searches),
                cursors={key: value for key, value in rotation.cursors.items() if key in wanted},
            ).model_dump(mode="json"),
        )

    def _params(self, search: PlannedSearch, cursor: str | None) -> dict[str, str]:
        """The query string for one page of one planned search."""
        return self.build_params(
            SearchQuery(keywords=(search.text,), country=search.country), cursor
        )

    def build_params(self, query: SearchQuery, cursor: str | None) -> dict[str, str]:
        """The query string for one page.

        ``work_from_home`` is deliberately absent: with "remote" already in the
        query text it returned the same ten postings in a different order, so it
        bought nothing and cost a credit.
        """
        params: dict[str, str] = {
            "query": " ".join(query.keywords) or "developer",
            # Pinned, never derived from query.posted_within_days.
            "date_posted": DATE_POSTED,
        }
        country = (query.country or "").lower()
        if country:
            params["country"] = country
        # Language is not optional for the CIS: without it the upstream assumes
        # English and returns nothing for a Russian-language market.
        params["language"] = query.language or LANGUAGE_BY_COUNTRY.get(country, DEFAULT_LANGUAGE)
        if cursor:
            params["cursor"] = cursor
        return params

    def to_posting(self, job: JSearchJob) -> RawPosting | None:
        """Convert one payload entry, or skip it if it cannot be stored."""
        url = job.job_apply_link
        if not url:
            # No link is no vacancy: the user could not act on it, and
            # vacancy_source.url is NOT NULL.
            return None
        location = clean_location(job.job_location)
        raw = job.model_dump()
        # Derived rather than trusted; the flags were wrong in the sample.
        raw["_derived"] = {
            "location": location,
            "remote": looks_remote(job.job_title, location, job.job_is_remote).value,
        }
        return RawPosting(
            source_slug=self.slug,
            external_id=job.job_uid[:200],
            url=url[:1000],
            title=job.job_title[:300],
            company=(job.employer_name or None) and job.employer_name[:200],
            description=job.job_description,
            raw=raw,
        )

    async def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]:
        """Walk the cursor, stopping early once a page is mostly old news."""
        headers = self._headers()
        cursor: str | None = None
        yielded = 0

        for page_number in range(1, MAX_PAGES + 1):
            payload = await self.http.get_json(
                BASE_URL, params=self.build_params(query, cursor), headers=headers
            )
            page = self._parse(payload)
            if not page.jobs:
                break

            postings = [posting for job in page.jobs if (posting := self.to_posting(job))]
            if await self._mostly_known(postings, query, page_number):
                break

            for posting in postings:
                yield posting
                yielded += 1
                if yielded >= query.limit:
                    return

            cursor = page.cursor
            if not cursor:
                break

    async def _mostly_known(
        self, postings: Sequence[RawPosting], query: SearchQuery, page_number: int
    ) -> bool:
        """Whether this page is old enough that listing on wastes credits."""
        if not postings:
            return False
        known = await self.already_known([posting.external_id for posting in postings])
        if not known:
            return False
        share = len(known) / len(postings)
        if share < KNOWN_SHARE_STOP:
            return False
        logger.info(
            "sources.jsearch.early_exit",
            page=page_number,
            known=len(known),
            of=len(postings),
            keywords=list(query.keywords),
        )
        return True

    def _parse(self, payload: Any) -> JSearchPage:
        """Validate a response, and say plainly when the shape has moved.

        The shape is pinned to what live calls returned. A silent ``.get("data",
        {})`` here would turn an upstream change into an empty run that looks
        like a market with no jobs in it.
        """
        data = payload.get("data") if isinstance(payload, dict) else None
        if data is None:
            raise SourceError(
                "jsearch: в ответе нет поля data — форма ответа изменилась",
                source_slug=self.slug,
            )
        if isinstance(data, list):
            # Tolerated because v1 shaped it this way; the cursor is then absent
            # and pagination stops after one page, which is visible in the logs
            # rather than silent.
            return JSearchPage(jobs=data, cursor=None)
        return JSearchPage.model_validate(data)
