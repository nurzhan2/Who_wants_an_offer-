"""arbeitnow.com: a free German/EU board whose API takes no search parameters.

The endpoint is one unfiltered, hourly-refreshed feed of the newest postings,
paginated with ``?page=N``. That single fact shapes everything below.

**Filtering happens on our side.** ``job-board-api`` accepts no query, no
location and no tag parameter, so ``SearchQuery.keywords`` is matched here
against the title, the tags and the description. It is a real limitation of the
source rather than an oversight: the alternative is to yield every posting on
the feed and let the matcher pay for it. The other ``SearchQuery`` fields
(area, country, remote, salary) are deliberately not applied — the feed carries
``location`` and ``remote`` as free text and a bool, and turning those into a
filter is normalisation's job, done once, for every source.

**Pages overlap, so a run deduplicates by slug.** Measured on the live feed:
pages two and three shared 17 of their 175 entries, and 450 postings fetched
across three pages were 433 distinct ones. ``BaseSource.search_batch`` already
drops repeats *across* queries, but that does not help a single ``search`` call
paginating on its own, which is where the overlap actually happens. The same
measurement supplies a genuine terminator: a page that contributes no new slug
at all is the feed shifting under us, not progress, and the loop stops there.

robots.txt allows everything on the host, so this stays a CRAWL source and the
shared client checks that file for us. No credential, no terms endpoint, and no
metering: the feed's own note asks only that we link back, which is what
``attribution`` is for.
"""

import re
from collections.abc import AsyncIterator, Sequence
from datetime import timedelta
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.exceptions import SourceError
from app.core.logging import get_logger
from app.sources.base import BaseSource, RateLimit, RawPosting, SearchQuery, mentions
from app.sources.registry import register_source

logger = get_logger(__name__)

API_URL = "https://www.arbeitnow.com/api/job-board-api"

#: Hard ceiling on pages per query, because the stop conditions all depend on
#: what the server sends back and a wedged feed must not become an endless
#: loop. At the measured 175 entries a page this covers the largest
#: ``SearchQuery.limit`` (5000) with room to spare.
MAX_PAGES = 30

#: Mirrors of the ``RawPosting`` limits, which mirror the database columns.
#: Applied here so an over-long value is cut or dropped on purpose, with the
#: reason logged, instead of failing validation halfway through a page.
MAX_EXTERNAL_ID = 200
MAX_URL = 1000
MAX_TITLE = 300
MAX_COMPANY = 200

#: Descriptions are HTML. Tags are stripped for keyword matching only — the
#: stored description keeps its markup — so that a keyword like "a" or "strong"
#: cannot match an element name instead of the prose.
_TAG_RE = re.compile(r"<[^>]+>")


class _Job(BaseModel):
    """One ``data`` entry, as the live response actually shapes it.

    Only the four fields a posting cannot exist without are required. The rest
    default, so a vendor dropping an optional key costs us that key rather than
    the whole run — while a genuine change of shape still fails loudly.
    """

    model_config = ConfigDict(frozen=True)

    #: Stable per posting, and the identifier used in the posting's own URL.
    slug: str
    title: str
    url: str
    company_name: str = ""
    #: HTML.
    description: str = ""
    location: str = ""
    remote: bool = False
    tags: tuple[str, ...] = ()
    job_types: tuple[str, ...] = ()
    #: Unix seconds. ``RawPosting`` has no published_at field, so this reaches
    #: normalisation inside ``raw`` rather than on the posting itself.
    created_at: int | None = None


class _Page(BaseModel):
    """The envelope: ``{"data": [...], "links": {...}, "meta": {...}}``.

    ``data`` stays a sequence of raw mappings rather than of :class:`_Job`, for
    two reasons: each entry is validated on its own so one bad row is not a
    whole lost page, and ``RawPosting.raw`` is contracted to hold the untouched
    payload.
    """

    model_config = ConfigDict(frozen=True)

    # Any: the entry's shape belongs to arbeitnow and is validated by _Job one
    # line later; declaring it here would be the same model written twice.
    data: tuple[dict[str, Any], ...]


def _matches(job: _Job, keywords: Sequence[str]) -> bool:
    """Whether any keyword occurs in the title, the tags or the description.

    Case-insensitive, and *any* keyword rather than *all*; a multi-word
    keyword — a job title — matches by all of its words (``base.mentions``): the
    planner crosses skill groups into one query, and requiring every term would
    empty a feed that has no relevance ranking to fall back on. No keywords
    means no filter — the caller asked for the feed itself.
    """
    if not keywords:
        return True
    haystack = " ".join(
        [job.title, " ".join(job.tags), _TAG_RE.sub(" ", job.description)]
    ).casefold()
    return any(mentions(haystack, word) for word in keywords)


@register_source
class ArbeitnowSource(BaseSource):
    """The arbeitnow.com job board feed."""

    slug: ClassVar[str] = "arbeitnow"
    name: ClassVar[str] = "Arbeitnow"
    regions: ClassVar[tuple[str, ...]] = ("DE", "EU")
    rate_limit: ClassVar[RateLimit] = RateLimit(requests_per_second=1.0)
    #: The feed asks for a link back in return for being free.
    attribution: ClassVar[str | None] = "arbeitnow.com"
    #: The list endpoint already carries the full description.
    needs_detail_fetch: ClassVar[bool] = False
    #: The feed states it refreshes hourly, so a shorter dev cache would only
    #: re-fetch pages that cannot have changed.
    cache_ttl: ClassVar[timedelta] = timedelta(hours=1)

    async def search_batch(self, queries: Sequence[SearchQuery]) -> AsyncIterator[RawPosting]:
        """Walk the feed ONCE for the whole plan.

        The base implementation runs ``search`` per query, which is right for a
        source you can ask a question and wrong here: this endpoint takes no
        search parameters, so every plan entry re-paginated the same feed and
        filtered it again locally. A live run made 182 requests for 309
        postings; one walk gets the same postings for an eighth of that.

        The keywords of every query are unioned, and a posting matching more
        than one is yielded once — which is what the runner stores anyway, since
        a vacancy row has no notion of which search found it.
        """
        keywords = tuple({word for query in queries for word in query.keywords})
        limit = max((query.limit for query in queries), default=0)
        combined = SearchQuery(keywords=keywords, limit=limit) if limit else None
        if combined is None:
            return
        async for posting in self.search(combined):
            yield posting

    async def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]:
        """Yield matching postings, paginating until a stop condition fires.

        Three of them, any one of which ends the walk: an empty page, the
        query's ``limit`` reached, or a page that adds no slug we have not
        already seen. ``MAX_PAGES`` bounds the loop regardless.
        """
        seen: set[str] = set()
        yielded = 0
        page = 0
        while page < MAX_PAGES and yielded < query.limit:
            page += 1
            entries = self._page(await self.http.get_json(API_URL, params={"page": page}), page)
            if not entries:
                break
            fresh = 0
            for entry in entries:
                job = self._job(entry, page)
                if job is None or job.slug in seen:
                    continue
                seen.add(job.slug)
                fresh += 1
                if not _matches(job, query.keywords):
                    continue
                posting = self._posting(job, entry)
                if posting is None:
                    continue
                yield posting
                yielded += 1
                if yielded >= query.limit:
                    break
            if fresh == 0:
                # Every entry was one we had already yielded or skipped: the
                # feed is serving us the same window, so the next page will not
                # be new either.
                logger.debug("sources.arbeitnow.page_exhausted", page=page, seen=len(seen))
                break
        logger.info(
            "sources.arbeitnow.search_finished",
            pages=page,
            unique=len(seen),
            yielded=yielded,
            keywords=list(query.keywords),
        )

    def _page(self, payload: Any, page: int) -> tuple[dict[str, Any], ...]:
        """The page's ``data`` array, or a failure that names what changed.

        ``Any`` because ``get_json`` hands back whatever the server sent; this
        method exists precisely to turn that into something typed.
        """
        try:
            return _Page.model_validate(payload).data
        except ValidationError as exc:
            logger.error("sources.arbeitnow.envelope_changed", page=page, errors=exc.errors())
            raise SourceError(
                f"{self.slug}: ответ job-board-api не содержит списка data (страница {page})",
                source_slug=self.slug,
            ) from exc

    def _job(self, entry: dict[str, Any], page: int) -> _Job | None:
        """One validated entry, or None when this single row is unusable.

        A row that fails validation is dropped with its errors logged rather
        than aborting the page: it costs one posting. A whole page failing is a
        different thing, and :meth:`_page` raises for it.
        """
        try:
            return _Job.model_validate(entry)
        except ValidationError as exc:
            logger.warning(
                "sources.arbeitnow.entry_invalid",
                page=page,
                slug=str(entry.get("slug", ""))[:MAX_EXTERNAL_ID],
                errors=exc.errors(),
            )
            return None

    def _posting(self, job: _Job, entry: dict[str, Any]) -> RawPosting | None:
        """A ``RawPosting``, or None when no honest one can be built.

        Titles and company names are truncated, because a clipped display
        string is still the right posting. Identifiers and URLs are not: a cut
        slug is a different key and a cut URL leads nowhere, so those rows are
        skipped instead.
        """
        external_id = job.slug.strip()
        url = job.url.strip()
        title = " ".join(job.title.split())
        if not external_id or len(external_id) > MAX_EXTERNAL_ID:
            logger.warning("sources.arbeitnow.skipped", reason="slug", length=len(external_id))
            return None
        if not url or len(url) > MAX_URL:
            logger.warning("sources.arbeitnow.skipped", reason="url", slug=external_id)
            return None
        if not title:
            logger.warning("sources.arbeitnow.skipped", reason="title", slug=external_id)
            return None
        company = " ".join(job.company_name.split())[:MAX_COMPANY]
        return RawPosting(
            source_slug=self.slug,
            external_id=external_id,
            url=url,
            title=title[:MAX_TITLE],
            company=company or None,
            description=job.description or None,
            # The entry as it arrived, so normalisation can re-read created_at,
            # location, remote and job_types without another request.
            raw=dict(entry),
        )
