"""Remotive: fetch the whole feed once, unfiltered, and filter it here.

The obvious connector sends the caller's keywords upstream and lets the source
rank them. This one deliberately sends no parameters at all, and the reason is
measured rather than assumed: the free endpoint returns a fixed set of 18
postings and ignores ``search``, ``category`` and ``limit`` entirely —
``category=software-dev`` and ``category=design`` came back with byte-identical
id sets spanning nine categories. Sending those parameters would buy nothing and
cost the illusion that the feed arrives filtered, so the feed is fetched once
per run and :meth:`RemotiveSource.search` matches ``SearchQuery.keywords``
against the payload locally. Do not "fix" the parameters back in.

The feed is small by design, not under-read. Re-measured 2026-09-16 with one
unparameterised call: ``job-count`` and ``total-job-count`` were both 13, spread
over nine categories, four of them software. A run that stores six of those is
the keyword filter doing its job; the rest were writers, sales and support.

The second shape-defining fact is the request budget. Remotive's legal notice
asks for at most about four calls a day and says excessive requests will be
blocked, so ``min_interval`` and ``cache_ttl`` are both six hours: one run every
six hours is four a day by construction. That is also why the cap is expressed
as an interval rather than as ``daily_quota`` — this connector makes exactly one
request per run, and counting the same request in two places only creates two
numbers that can disagree.

Descriptions arrive inline, so ``needs_detail_fetch`` stays False and a run
costs one request no matter how many postings it yields.
"""

import re
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.core.exceptions import SourceError
from app.core.logging import get_logger
from app.schemas.crawl import SavedState, SearchPreview
from app.sources.base import (
    AccessMode,
    BaseSource,
    RateLimit,
    RawPosting,
    SearchQuery,
    mentions,
)
from app.sources.registry import register_source

logger = get_logger(__name__)

FEED_URL = "https://remotive.com/api/remote-jobs"

#: Where the timezone-corrected publication timestamp is carried in ``raw``.
#: Normalisation must read this key, never the source's own
#: ``publication_date``: that one is naive, and reading it into a
#: timezone-aware column would silently shift every posting by the local offset.
PUBLISHED_AT_KEY = "publication_date_utc"

#: Mirrors of the ``RawPosting`` limits, which mirror the DB columns. Applied
#: here so an over-long value is trimmed (or the posting dropped) with the field
#: named, rather than failing validation at the connector boundary.
MAX_TITLE = 300
MAX_COMPANY = 200
MAX_URL = 1000

#: Descriptions are HTML. Keyword matching strips the markup first, otherwise
#: ordinary words like "table", "form" or "style" match tag names and
#: attributes instead of anything the posting actually says.
_TAG_RE = re.compile(r"<[^>]+>")


def _collapse(value: str | None, limit: int) -> str | None:
    """Whitespace-collapsed text that fits ``limit``, or None when nothing is left.

    Remotive's company names carry trailing spaces ("Coalition Technologies ")
    often enough that trimming is not defensive coding but the normal case.
    """
    if value is None:
        return None
    text = " ".join(value.split())
    return text[:limit] or None


class RemotiveJob(BaseModel):
    """One posting as the list endpoint returns it.

    ``id``, ``url`` and ``title`` are required: without them there is nothing to
    store and nothing to link to, so a malformed entry fails here and is skipped
    rather than reaching the database as a stub.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    url: str
    title: str
    company_name: str | None = None
    category: str | None = None
    tags: tuple[str, ...] = ()
    job_type: str | None = None
    #: Naive in the payload, aware after validation. See :meth:`_as_utc`.
    publication_date: datetime | None = None
    candidate_required_location: str | None = None
    #: Free text: "$20k -$35k", "$120 - $170 /hour", "Pay per task", "". Never
    #: parsed into numbers here — that is the normalisation layer's job, and
    #: guessing a currency and a period in a connector hides the guess.
    salary: str | None = None
    description: str | None = None

    @field_validator("tags", mode="before")
    @classmethod
    def _no_tags_is_empty(cls, value: Any) -> Any:
        """Accept a missing or null ``tags`` as "no tags"."""
        return () if value is None else value

    @field_validator("publication_date")
    @classmethod
    def _as_utc(cls, value: datetime | None) -> datetime | None:
        """Attach UTC to Remotive's naive timestamps, explicitly.

        The payload carries ``"2026-09-02T19:59:53"`` with no offset. Every
        timestamp column in this project is timezone-aware, so passing the naive
        value on would either raise on write or be read as local time — a silent
        several-hour shift in "posted at", which is exactly the field freshness
        filtering and sorting depend on. Remotive publishes in UTC, so the
        assumption is stated here once instead of being made implicitly further
        down the pipeline.
        """
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class RemotiveFeed(BaseModel):
    """The response envelope; only ``jobs`` is ours to use.

    The other keys are the vendor's warning and legal notice, which are prose we
    have encoded as behaviour (attribution, interval, cache) rather than data.
    """

    model_config = ConfigDict(extra="ignore")

    #: Required rather than defaulted to empty: a response without ``jobs`` is
    #: an endpoint that changed under us, and defaulting would report it as a
    #: quiet "no vacancies today" — the failure nobody investigates.
    #:
    #: ``Any`` because each entry is validated on its own by
    #: :class:`RemotiveJob` one step later: typing the list as ``RemotiveJob``
    #: would make one malformed entry discard the other seventeen, and a feed
    #: this small cannot afford that. The dicts never leave this module except
    #: as ``RawPosting.raw``, which is declared as a payload dict by contract.
    jobs: list[dict[str, Any]]


@register_source
class RemotiveSource(BaseSource):
    """Remote-only job board with a free public JSON feed, used under its terms.

    Attribution is a condition of access, not a courtesy: Remotive requires a
    link back to the posting's own remotive.com URL and a visible mention of
    Remotive as the source, and states plainly that access is terminated
    otherwise. That is what :attr:`attribution` is for; the dashboard shows it
    next to every posting from here, and dropping it breaks the terms.

    They ask for no more than about four requests a day and warn that excessive
    requests are blocked, which :attr:`min_interval` and :attr:`cache_ttl`
    encode. Postings reach this feed 24 hours after publication by design, so a
    vacancy missing today is not evidence of a broken connector.

    Forbidden by the same notice: re-publishing these postings to other job
    boards (Jooble, Neuvoo, Google Jobs, LinkedIn Jobs are named), and using
    them to collect signups or email addresses. The private API that lifts the
    limits starts at $5k/month. Terms: https://remotive.com/api-documentation

    The feed also ignores every filter parameter it documents — see the module
    docstring before adding ``search`` or ``category`` back.
    """

    slug = "remotive"
    name = "Remotive"
    regions = ("remote",)
    #: A documented public endpoint governed by the terms summarised above, so
    #: robots.txt is not what applies to it.
    access_mode = AccessMode.API
    terms_url = "https://remotive.com/api-documentation"
    attribution = "Источник: Remotive (remotive.com)"
    rate_limit = RateLimit(requests_per_second=1.0, burst=1)
    #: Four runs a day, the vendor's stated ceiling, spread evenly.
    min_interval = timedelta(hours=6)
    cache_ttl = timedelta(hours=6)
    #: The list endpoint already carries the full HTML description.
    needs_detail_fetch = False

    async def search_batch(self, queries: Sequence[SearchQuery]) -> AsyncIterator[RawPosting]:
        """Fetch the feed ONCE for the whole plan.

        The base implementation runs ``search`` per query, which is right for a
        source that can be asked a question and wrong here: this endpoint takes
        no search parameters, so eight plan entries meant eight identical
        downloads of the same eighteen postings. A live run did exactly that and
        spent eight requests against a published allowance of about four a day —
        a breach of the terms this connector exists to honour, not merely waste.

        One fetch, then every query's keywords are matched against it, and a
        posting matching more than one is still yielded once.
        """
        payload = await self.http.get_json(FEED_URL)
        feed = self._parse(payload)
        needles = tuple({word.casefold() for query in queries for word in query.keywords})
        limit = max((query.limit for query in queries), default=0)
        logger.info(
            "sources.remotive_fetched",
            slug=self.slug,
            jobs=len(feed.jobs),
            queries=len(queries),
            keywords=len(needles),
        )

        seen: set[str] = set()
        for item in feed.jobs:
            if len(seen) >= limit:
                break
            job = self._validate(item)
            if job is None:
                continue
            if needles and not _matches(job, needles):
                continue
            posting = self._posting(job, item)
            if posting is None or posting.external_id in seen:
                continue
            seen.add(posting.external_id)
            yield posting

    async def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]:
        """Yield the postings matching one query.

        Kept for the single-query case and for the contract; a whole plan goes
        through :meth:`search_batch`, which fetches once.
        """
        payload = await self.http.get_json(FEED_URL)
        feed = self._parse(payload)
        needles = tuple(word.casefold() for word in query.keywords)
        logger.info(
            "sources.remotive_fetched",
            slug=self.slug,
            jobs=len(feed.jobs),
            keywords=len(needles),
        )

        emitted = 0
        for item in feed.jobs:
            if emitted >= query.limit:
                break
            job = self._validate(item)
            if job is None:
                continue
            if needles and not _matches(job, needles):
                continue
            posting = self._posting(job, item)
            if posting is None:
                continue
            emitted += 1
            yield posting

    def preview_search(
        self, queries: Sequence[SearchQuery], stored: Sequence[SavedState]
    ) -> SearchPreview:
        """The local filter, with the size of what it filters stated."""
        preview = super().preview_search(queries, stored)
        return preview.model_copy(
            update={
                "note": (
                    "Бесплатная лента Remotive — 13–18 вакансий на все профессии, "
                    "запрос не чаще раза в 6 часов; фильтр по этим словам применяется здесь."
                )
            }
        )

    def _parse(self, payload: Any) -> RemotiveFeed:
        """Validate the envelope, or fail this source with a readable reason."""
        try:
            return RemotiveFeed.model_validate(payload)
        except ValidationError as exc:
            raise SourceError(
                f"{self.slug}: неожиданная структура ответа, поле jobs не найдено",
                source_slug=self.slug,
            ) from exc

    def _validate(self, item: dict[str, Any]) -> RemotiveJob | None:
        """One entry, or None when it is unusable.

        A single bad entry is logged and skipped rather than raised: the feed is
        eighteen postings long, and losing seventeen good ones to one broken
        record would be the worse failure.
        """
        try:
            return RemotiveJob.model_validate(item)
        except ValidationError as exc:
            logger.warning(
                "sources.remotive_item_invalid",
                slug=self.slug,
                external_id=str(item.get("id")),
                errors=exc.error_count(),
            )
            return None

    def _posting(self, job: RemotiveJob, item: dict[str, Any]) -> RawPosting | None:
        """Map one validated entry onto the pipeline's contract.

        ``raw`` keeps the payload as it arrived — free-text ``salary``,
        ``tags``, ``category``, ``candidate_required_location`` and all — plus
        the UTC-explicit timestamp, so normalisation can be re-run without
        spending another of the four daily requests.
        """
        url = job.url.strip()
        title = _collapse(job.title, MAX_TITLE)
        if not url or len(url) > MAX_URL or title is None:
            # Truncating a URL would produce a link that resolves to nothing,
            # and a posting with no title cannot be shown, so both are dropped.
            logger.warning(
                "sources.remotive_item_unusable",
                slug=self.slug,
                external_id=str(job.id),
                url_length=len(url),
                has_title=title is not None,
            )
            return None

        raw = dict(item)
        raw[PUBLISHED_AT_KEY] = (
            job.publication_date.isoformat() if job.publication_date is not None else None
        )
        return RawPosting(
            source_slug=self.slug,
            external_id=str(job.id),
            url=url,
            title=title,
            company=_collapse(job.company_name, MAX_COMPANY),
            description=job.description,
            raw=raw,
        )


def _haystack(job: RemotiveJob) -> str:
    """The text a keyword is matched against, casefolded."""
    parts: list[str] = [job.title, job.category or "", *job.tags]
    if job.description:
        parts.append(_TAG_RE.sub(" ", job.description))
    return " ".join(parts).casefold()


def _matches(job: RemotiveJob, needles: tuple[str, ...]) -> bool:
    """Whether any keyword occurs in the posting.

    Deliberately permissive matching over title, tags, category and
    description — a word as a substring, a job title by all of its words
    (``base.mentions``): this filter only has to cut the obviously unrelated half of a
    mixed feed, and deciding how well a posting actually fits the profile is the
    scoring layer's job, on the full text, with weights.
    """
    haystack = _haystack(job)
    return any(mentions(haystack, needle) for needle in needles)
