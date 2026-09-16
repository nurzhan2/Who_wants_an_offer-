"""HeadHunter through its own sitemap — the one door its robots.txt leaves open.

The jobseeker half of ``api.hh.ru`` is shut: ``/vacancies`` answers 403 to every
programmatic client and the application form on dev.hh.ru issues keys only to a
verified employer account. That is settled and this connector does not argue
with it. What is still open is the site's own crawl surface, and everything
below follows from reading it rather than from the API we cannot have.

**What robots.txt actually says.** In the ``User-agent: *`` group hh allows the
site and then forbids one shape of URL: ``Disallow: *?*``, every address
carrying a query string, with three narrow exceptions (``*?u*``,
``*?currencyCode*``, ``*?vacancyId*``) that a search URL does not match — its
first parameter is ``text``. So ``/sitemap/*.xml`` and ``/vacancy/{id}`` are
open, ``/search/vacancy?text=...`` is closed, and this connector implements the
first and not the second. Not "not yet": the search page answers 200 with a
megabyte of ready-made JSON to anyone who asks, and we do not ask.

That rule is enforced in ``app/sources/http.py`` rather than here, because
``urllib.robotparser`` cannot enforce it. CPython matches a rule as a literal
path prefix and has no wildcards, so ``*?*`` matches nothing and the robots
layer answers "allowed" for the search URL — measured against the live file on
2026-09-06. A ban that only the connector honours is a ban one refactor away
from being gone, so the transport refuses a query string on hh outright, along
with ``/search`` and ``api.hh.ru/vacancies``.

**Being challenged is not the same as breaking a rule, and the crawl says
which.** On a live run of 2026-09-06 — 184 requests over 253 seconds, 172
vacancies read — request 173 was a plain ``GET /vacancy/136284790`` with no
query string, and hh answered ``302`` to ``/account/captcha?…``. Refusing to
follow that is right and stays. What changed is what it is called: a redirect
into ``/account`` raises :class:`~app.sources.http.HHChallengedError` and stops
the run for this source, instead of reporting that we built a URL robots.txt
forbids, which we had not. Nothing is retried, nothing already fetched is
thrown away, and the position is written down as far as it is provably safe to
write it — never onto the page we were refused — so the next run resumes at
that page rather than at the top of the file. What it does not do is solve the
captcha, slow down and try again inside the same run, or come back wearing a
browser's User-Agent.

Corrected 2026-09-07, and again 2026-09-08. It first said a challenged run
leaves the position where it was, which was true and useless, because where it
was was nowhere. It then said the position was recorded up to a fixed lag, which
recorded nothing either: the lag was a guess at how much the pipeline was
holding unwritten, and no run ever ran long enough to clear it. The pipeline now
says what it has written — ``BaseSource.record_progress`` — and the position
follows that. See ``MAX_HELD``.

**The walk takes the newest outstanding entry first**, across every file of the
site, and that is a change of 2026-09-08 too. It used to buy a slice of the
newest and then walk the rest oldest-first, because the position was a frontier
through time and could only move one way. It is a set of finished stretches now
(``FileWatermark``), so the walk can simply take the freshest thing it has not
covered. Which matters because a run is short: hh tolerates about one page every
four to five seconds — measured, see ``rate_limit`` — and both live runs were
stopped inside a hundred pages. The whole corpus is not the goal; some 13 557
postings for one city, most long filled. Older entries are what the remaining
budget reaches.

**And within that, the professions the profile asked for come first** — the
change of the evening of 2026-09-08, and the one that decided whether this
source is worth its rate limit at all. Date order is honest and blind: a walk of
Almaty returned 294 postings, none of them in a development role, and scoring
them put zero in "apply". hh publishes a page per profession under
``/vacancies/{slug}`` — 50 vacancy ids on ``/vacancies/programmist``, measured —
and the ids those pages name are walked before the ids they do not. Nothing is
dropped for not being named; the second half of the walk is the corpus in the
order it always had. See ``_crawl_site`` for why the ids are intersected with the
sitemap rather than fetched directly, and ``hh_roles.py`` for how a resume
becomes a list of professions without this repository holding a list of them.

Only that redirect is recognised, and the gap is written down rather than
papered over: a challenge delivered as a status code — a 403 whose body holds
the captcha — has never been served to this repository, and a marker guessed
for one would be worse than the gap. The ordinary page captured on 2026-09-06
already carries ``captcha`` in its translations dictionary, under
``error.signup.captcha.invalid``, so a body test written today would report
every page it read successfully as a challenge. docs/SOURCES.md records what
such a run looks like until somebody measures the real answer.

**No account is involved, and that is the point.** The objection this connector
had to answer was not robots but the user's own hh profile, where their working
resume lives: an automated login there risks a ban that costs more than any
coverage is worth. Nothing here signs in. There is no cookie, no session
header, no OAuth, no ``HH_*`` credential, and the User-Agent is the project's
own contactable one — hh can identify us and, if they would rather we stopped,
name us in the file we already read. ``resumes*.xml`` in the sitemap index is
other people's resumes and is never fetched.

Worth writing down because it is a real signal: hh gives ten AI crawlers
(GPTBot, ClaudeBot, CCBot, PerplexityBot, Google-Extended and the rest) their
own groups, each ``Disallow: /vacancy/*``. We are not one of them, we do not
present ourselves as one, and the wildcard group we do match allows those pages.
This is a personal job search reading postings addressed to job seekers, one at
a time, slowly.

**The shape of a run.** ``main.xml`` lists the per-file sitemaps; the
``vacancy{N}.xml`` ones carry a ``<loc>`` and a ``<lastmod>`` per vacancy and
nothing else — measured at 1387 entries in one file, about fourteen thousand for
a city. There is no title in the sitemap and no search to run — the one hh has
is behind a query string its robots.txt forbids — so this source walks a corpus,
which is why it overrides ``search_batch``, keeps its own page budget, and
remembers per sitemap file how far it got. A first crawl takes several runs. Each
of them logs what it did not reach.

The ``vacancies{N}.xml`` files in the same index are the other half of the run:
15 of them for Almaty holding 10 435 catalogue slugs, no ``lastmod`` on any
entry, and a page per slug listing the postings in that profession. A run reads
``CATALOG_PAGES_PER_RUN`` of those pages, rotating through the slug list across
runs, and the plan behind that rotation lives in ``source_state`` beside the
crawl position. Paging inside a catalogue page exists only as ``?page=0..3`` and
is therefore closed to us, so depth comes from the breadth of the slug list; the
overlap that produces costs nothing, because the same posting reached through two
professions is one row after the fingerprint.

**A vacancy page is a JSON document wearing HTML.** The state the frontend boots
from sits in ``<template style="display:none" id="HH-Lux-InitialState">`` as
escaped JSON; ``vacancyView`` is the posting and ``vacancyFieldsDictionary`` is
the decoding table for its enums, shipped with the page, which is why none of
those vocabularies are hardcoded here. We are parsing somebody else's internal
state and it will change without warning, so a missing marker raises
:class:`HHMarkupError` rather than returning nothing — once it has happened
three times in a run, because one odd page is not a redesign — and
``test_hh_canary.py`` asks the live site the same question on a schedule. An
empty ``vacancyView`` is the one quiet case, because that is what a posting
taken down looks like: hh answers 404 with the marker still in place. The
alternative to all of this is finding out from a dashboard that has quietly
shown zero new hh postings for a week.

Three measurements that shaped the parsing, all from 22 live pages on
2026-09-06 and none of them guessable from the API documentation:

*The page is not the search payload.* ``creationTime``, ``publicationTime`` and
``lastChangeTime`` do not exist on it. What exists is ``publicationDate``, and
auto-renewal bumps that — one sampled posting carried ``HH_AUTO_RENEWAL`` with
``intervalMinutes = 4320``, so it re-publishes itself every 72 hours and its
``lastmod`` moves with it. Freshness therefore cannot come from this source at
all; it comes from our own ``first_seen_at``, and anything that alerts a person
must key on the first ``(source, external_id)`` we ever saw. Otherwise the user
is told about the same job every three days forever.

*Collections arrive in three forms and the form varies per page.* ``keySkills``
was ``{"keySkill": [...]}`` on 16 pages and ``null`` on 6; ``driverLicenseTypes``
was ``null`` on 20 and ``{"driverLicenseType": ["B"]}`` on 2; ``languages`` was
absent entirely. Hence one :func:`unwrap` applied to every collection rather
than a rule per field.

Two consequences of this source's size land outside it, and are recorded here
rather than fixed quietly in somebody else's module.

``pipeline/runner.py`` hashes a vacancy from its company, its title and no city,
deliberately, because most sources report a place as free text and a guessed
city produces a wrong key. hh does not guess — the page carries one, and it
reaches ``_derived.city`` — but the fingerprint is a UNIQUE column shared with
every row already written under the old rule, so populating it is a
``FINGERPRINT_VERSION`` bump and a backfill, not a parameter. Until then two hh
postings from one employer with the same title collapse into one vacancy row,
which is a real loss on a corpus with chain employers in it.

``pipeline/embedding.py`` embeds at most 500 rows a run, newest first. A city
slice writes more than that, so on a first crawl a share of the corpus keeps no
vector until the whole thing is walked again. Semantic ranking sees the rows it
has, and that is fewer than the dashboard shows.

*Salary has more shapes than any list of them.* Six distinct key sets in 22
pages, including ``perModeFrom`` as well as ``perModeTo``, so
:class:`HHCompensation` declares optional fields instead of enumerating forms.
``{"noCompensation": {}}`` is a non-empty dict, so ``if compensation:`` is true
for a posting with no salary — the check has to be for the key.
"""

import html as html_lib
import json
import re
from bisect import bisect_left, bisect_right
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlsplit

import yaml  # type: ignore[import-untyped]
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from app.core.exceptions import SourceError
from app.core.logging import get_logger
from app.db.enums import RemoteType, SalaryPeriod
from app.schemas.crawl import CrawlPosition, SavedState, SearchPreview, SearchUse
from app.sources.base import (
    PREVIEW_TERMS,
    AccessMode,
    BaseSource,
    RateLimit,
    RawPosting,
    SearchQuery,
)
from app.sources.hh_roles import (
    DirectoryRole,
    RoleFamily,
    carries,
    families_for,
    load_families,
    rank_slugs,
    read_directory,
    roles_for,
    slugs_for,
)
from app.sources.http import HHChallengedError
from app.sources.registry import register_source

if TYPE_CHECKING:  # pragma: no cover - imported for the annotation only
    from app.sources.http import SourceHTTP

logger = get_logger(__name__)

#: Which hh sites this deployment reads. A file rather than constants because
#: the city is a property of the deployment, not of the code — CLAUDE.md's rule
#: against hardcoding Almaty — and a file inside the package because rule 5 says
#: a source may not add settings to ``app/core/config.py``.
SITES_FILE = Path(__file__).with_name("hh_sites.yaml")

#: The index. Fetched per host, because ``hh.kz/sitemap`` redirects by the
#: requester's geography and cannot be trusted to answer for a chosen city.
SITEMAP_INDEX_PATH = "/sitemap/main.xml"

#: Sitemaps of individual vacancy pages, which is all we read. ``vacancies{N}``
#: (landing pages by profession, 5716 entries with no lastmod) is a different
#: file and still not used: whether it is a way into the corpus by profession —
#: which is what this walk's blindness to the profession costs us, see
#: docs/SOURCES.md § «Обход по профессиям» — turns on a measurement nobody has
#: taken, and ``hh_probe.py`` is the instrument for taking it. ``employers`` is
#: companies; and ``resumes{N}`` is living people's resumes, which is why the
#: selection here is an allow-list matched on the whole name rather than a
#: substring test.
VACANCY_SITEMAP = re.compile(r"/sitemap/(vacancy\d+)\.xml$")

#: The frontend's boot state, escaped inside a hidden template element.
STATE_MARKER = re.compile(
    r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', re.DOTALL
)

#: Sitemap XML is read with a regex rather than an XML parser on purpose: the
#: files run to hundreds of kilobytes, the two fields wanted are adjacent, and
#: an entity-expanding parser pointed at a third party's document is a liability
#: we have no reason to take on.
SITEMAP_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
SITEMAP_ENTRY = re.compile(
    r"<url>\s*<loc>\s*([^<\s]+)\s*</loc>\s*<lastmod>\s*([^<\s]+)\s*</lastmod>",
    re.IGNORECASE | re.DOTALL,
)
VACANCY_PATH = re.compile(r"^/vacancy/(\d+)$")

#: The other family in the same index, and the way in by profession. Measured
#: 2026-09-08: 15 files for Almaty holding 10 435 slugs, no ``lastmod`` on any
#: entry, and ``/vacancies/programmist`` carrying 50 vacancy ids. Matched on the
#: whole file name for the reason ``VACANCY_SITEMAP`` is: four letters separate
#: it from the vacancy files and three from nothing at all.
CATALOG_SITEMAP = re.compile(r"/sitemap/(vacancies\d+)\.xml$")

#: A catalogue URL as the sitemap writes it: ``/vacancies/{slug}``, no query
#: string, no date. Anything deeper is a page of one and is not fetched — see
#: ``_catalog_ids`` for why paging is closed to us.
CATALOG_PATH = re.compile(r"^/vacancies/([^/]+)/?$")

#: A vacancy id anywhere in a catalogue page — in an ``href`` or inside the
#: escaped JSON of the boot state. hh's own URL shape rather than a key name of
#: theirs we would have to guess at and re-guess when they rename it.
VACANCY_ID_ON_PAGE = re.compile(r"/vacancy/(\d+)")

#: hh's public dictionary of professional roles: 194 of them on 2026-09-08, and
#: one of the endpoints that stayed open when the jobseeker half of that host
#: closed. What the profile is matched against; see ``hh_roles.py``.
ROLES_URL = "https://api.hh.ru/professional_roles"

#: Block-level tags become a newline when the description is flattened for the
#: embedding; everything else becomes a space. Without the distinction a list of
#: requirements arrives as one run-on line.
BLOCK_TAGS = re.compile(r"</(?:p|div|li|ul|ol|tr|h[1-6]|blockquote)>|<br\s*/?>", re.IGNORECASE)
ANY_TAG = re.compile(r"<[^>]+>")

#: Mirrors of the ``RawPosting`` limits, which mirror the database columns.
MAX_EXTERNAL_ID = 200
MAX_URL = 1000
MAX_TITLE = 300
MAX_COMPANY = 200

#: Statuses that mean the posting is gone rather than that we were refused.
#: The sitemap is a snapshot and the site is not, so a walk of it always
#: contains a few of these.
GONE_STATUSES: frozenset[int] = frozenset({404, 410})

#: Unreadable pages one run tolerates before it gives up and says so. One
#: page can be odd — a truncated response, a posting mid-edit — and wedging
#: the crawl on it forever would be worse than skipping it, because the walk
#: resumes at the same entry every run. Three in one run is not one odd page:
#: it is hh having moved the state we parse, which is the whole thing this
#: connector must announce rather than absorb.
MAX_MARKUP_FAILURES = 3

#: Vacancy pages one run may fetch, across every site. The binding cost of this
#: source is requests, not postings: the sitemap carries no title, so a page has
#: to be fetched before anyone can tell whether it is worth keeping. At the
#: declared rate this is about twenty minutes of polite crawling, which fits
#: inside the three-hour incremental cadence with room to spare, and a first
#: full pass over a city's fourteen thousand pages completes over about a dozen
#: runs. What a run could not reach is logged, never silently dropped.
MAX_PAGES_PER_RUN = 1200

#: Catalogue pages one run opens per site, before it starts on vacancies.
#:
#: Small, and the arithmetic is the argument. One page named 50 vacancy ids when
#: it was measured, so eight of them name about four hundred — and a run has
#: never fetched anywhere near that many postings: hh answered one live crawl
#: with a check for robots at the 172nd request and another at the 50th. The
#: binding constraint is the vacancy budget, never the supply of ids, so every
#: catalogue page beyond what fills that budget is a posting not fetched. At the
#: worst rate observed, eight leaves forty-two postings; twenty would leave
#: thirty for no gain at all.
#:
#: The list is walked in a rotation across runs (``CatalogPlan.offset``), so a
#: 630-slug set is swept over some eighty runs — about ten days at the pipeline's
#: three-hour cadence — and every run names postings the last one did not. A
#: catalogue page is heavy, 1.49 MB measured, and only the ids are read out of
#: it; that is bandwidth rather than requests, and requests are what hh counts.
CATALOG_PAGES_PER_RUN = 8

#: How many of a run's catalogue pages come from the top of the ranked list
#: rather than from the rotation.
#:
#: Half, and the split is the answer to a real failure rather than a taste. The
#: list is ranked by nearness to the profile (``hh_roles._rank``), and a pure
#: rotation over it spends run 10 on the tail — measured on the live plan, role
#: 96 alone matches over a hundred slugs and dozens of them are 1C, ABAP,
#: Navision and CNC pages that have nothing to do with this profile. Re-reading
#: the top pages every run is also how a NEW python posting is found within
#: hours instead of within the eighty runs a full sweep takes; the rotation is
#: what eventually covers the rest.
CATALOG_HEAD_PAGES = 4

#: Catalogue slugs one plan may hold. 630 of 10 435 slugs were development on
#: 2026-09-08, so this is headroom for a wider profile rather than a limit that
#: bites today; what it stops is a keyword like "sql" quietly selecting half
#: the site and pushing the roles hh itself named to the back of the rotation.
MAX_CATALOG_SLUGS = 1000

#: How long a resolved plan is reused before the slug list is read again. The
#: catalogue costs 15 requests to re-read and its slugs are professions, which
#: do not turn over weekly; the postings behind them are re-read every run.
#: A profile change re-resolves immediately whatever this says, because the
#: stored plan records which families asked for it.
CATALOG_TTL = timedelta(days=7)

#: How many walked entries may wait for the pipeline's confirmation before the
#: walk stops adding to the list. Not a correctness bound — the pipeline
#: confirms every ``UPSERT_BATCH`` postings and the list drains each time — but
#: a stretch of pages that store nothing would otherwise be held in memory
#: without limit.
#:
#: This replaced ``WATERMARK_LAG``, which held the recorded position a fixed
#: number of postings behind the walk on the reasoning that the pipeline's
#: unwritten batch is at most that big. The reasoning was right and the number
#: was unknowable from here: a run shorter than the lag recorded NOTHING, and
#: measured against two live crawls no run ever was longer. hh answered with a
#: check for robots at the 172nd posting and then, at a higher rate, at the
#: 50th. ``BaseSource.record_progress`` replaced the guess with the pipeline
#: saying what it has actually written.
MAX_HELD = 5_000

#: Prefix of the ``source_state`` key holding one sitemap file's position, and
#: of the one holding its measured size. Constants rather than inline f-strings
#: because :meth:`HHSource.describe_position` reads back what the crawl wrote,
#: and two spellings of the same key is a bug that looks like an empty screen.
POSITION_PREFIX = "sitemap:"
CENSUS_PREFIX = "sitemap-size:"

#: Finished stretches kept per sitemap file. On overflow the oldest is dropped,
#: which makes those entries due again; the alternative, merging two stretches
#: that are not neighbours, would claim entries nobody fetched. Repeating costs
#: requests, losing costs data.
#:
#: Raised from 64 on 2026-09-08, and the reason is the whole point of that day's
#: work. This used to say "one run adds at most one stretch — the walk is
#: contiguous within a run", which was true of a walk ordered only by date. The
#: walk now takes the postings of the profile's own professions first, and those
#: sit scattered through a file ordered by date, so one run leaves dozens of
#: stretches in a file rather than one. At 64 the cap was reached inside a single
#: run and the oldest coverage was dropped every time — the crawl would have paid
#: for the same pages again and again while never finishing a file.
#:
#: The fragmentation is temporary in the direction that matters: two stretches
#: merge as soon as nothing lies between them, so a file being worked over
#: collapses back towards one. 512 is room for several runs of it, about 75 kB of
#: JSON per file at the observed span size.
MAX_SPANS = 512

#: The sitemaps are never cached. ``cache_ttl`` is thirty days because a
#: vacancy page carries its own ``lastmod`` as a cache salt, so an edited
#: posting misses and an untouched one hits. The sitemaps have no such salt —
#: their whole job is to tell us what changed — so inheriting that TTL would
#: pin the index and every file on disk for a month, and the connector would
#: discover nothing hh published after its first run. Measured: with
#: ``HTTP_CACHE_DIR`` set, run two of a two-vacancy fixture yields nothing at
#: all when the sitemaps are cached.
SITEMAP_CACHE_TTL = timedelta(0)


#: hh's ``mode`` says what the money is *per*, and only two of its five values
#: have an honest equivalent in ``SalaryPeriod``. A shift is not a day and a
#: rotation is not a month; mapping them anyway would put a wrong number into
#: the normalised salary that the dashboard sorts on, so those postings keep
#: their amount, lose the period, and carry the original mode in ``raw`` for
#: whoever adds the rest of the vocabulary.
MODE_TO_PERIOD: dict[str, SalaryPeriod] = {
    "MONTH": SalaryPeriod.MONTH,
    "HOUR": SalaryPeriod.HOUR,
}

#: A language requirement, as hh renders it into ``keySkills``: a language
#: name, a non-breaking space, an em dash, a CEFR level and its Russian label —
#: ``"Русский" + U+00A0 + "— C1 — Продвинутый"``. Nine of the 121 skills seen across 22
#: pages were these. They are requirements, but they are not hard skills, and
#: docs/MATCHING.md computes hh coverage as a set intersection over the skill
#: list at full weight — so leaving them in would report every candidate as
#: missing a required "skill" whose name is a sentence about Kazakh.
LANGUAGE_SKILL = re.compile(r"^(?P<language>[^\s—]+)\s*—\s*(?P<level>[ABC][12])\s*—")

#: ``workFormats`` is the only honest witness of remoteness on the page.
FORMAT_TO_REMOTE: dict[str, RemoteType] = {
    "REMOTE": RemoteType.FULL,
    "HYBRID": RemoteType.HYBRID,
    "ON_SITE": RemoteType.NO,
    "FIELD_WORK": RemoteType.NO,
}


class HHMarkupError(SourceError):
    """The vacancy page no longer contains the state we parse.

    Raised instead of returning nothing, because returning nothing is what a
    market with no jobs in it also looks like. Carries the response status and
    the body size, which together separate the three ways this happens: hh
    renamed the marker (200, full body), hh served us an error or a challenge
    page (non-200), or the posting is gone (404, marker present, empty view).
    """

    def __init__(
        self, detail: str, *, url: str, status_code: int, body_bytes: int, source_slug: str
    ) -> None:
        super().__init__(
            detail,
            source_slug=source_slug,
            url=url,
            response_status=status_code,
            body_bytes=body_bytes,
        )


def unwrap(value: Any) -> list[Any]:
    """Flatten hh's three ways of spelling a list into one.

    ``None`` is an empty list, a single-key wrapper such as
    ``{"keySkill": [...]}`` is its contents, and a list is itself. Applied to
    every collection on the view rather than to the fields that happened to be
    wrapped in one sample: the same field arrives wrapped on one page and null
    on the next, and ``driverLicenseTypes`` proved it by doing exactly that
    twice in twenty-two pages.

    ``Any`` because the element type is hh's and differs per field — strings for
    skills, ints for professional roles.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        if len(value) != 1:
            return []
        inner = next(iter(value.values()))
        return list(inner) if isinstance(inner, list) else [inner]
    return [value]


def strip_html(markup: str | None) -> str | None:
    """Description HTML as plain text, keeping the line breaks that carry meaning.

    Descriptions are ``<p>``, ``<ul>``, ``<li>``, ``<strong>`` and ``<br />``.
    The blunt "replace every tag with a space" used by the feed connectors turns
    a requirements list into a single line, which reads badly in the dashboard
    and gives the embedding one undifferentiated paragraph, so block ends become
    newlines here and everything else becomes a space.

    Deliberately not a new dependency. The brief suggested selectolax; against
    bs4 it would be the right call, but this is the whole of the work it would
    do, CLAUDE.md asks for a check that what we have does not already suffice,
    and a C extension in the install for one substitution is not a trade worth
    making.
    """
    if not markup:
        return None
    text = BLOCK_TAGS.sub("\n", markup)
    text = ANY_TAG.sub(" ", text)
    text = html_lib.unescape(text).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    # Blank lines are dropped rather than collapsed: hh nests its block
    # elements, so ``</li></ul><p>`` produces three breaks in a row, and the
    # result reads as a gappy transcript while embedding no better for the gaps.
    return "\n".join(line for line in lines if line) or None


class HHSite(BaseModel):
    """One hh host this deployment reads, as ``hh_sites.yaml`` describes it."""

    model_config = ConfigDict(frozen=True)

    host: str = Field(min_length=1, max_length=100)
    city: str = Field(min_length=1, max_length=120)
    country: str = Field(min_length=2, max_length=2)
    #: Used when the plan names no place this file recognises, which is what a
    #: remote-only or relocation-only profile produces.
    default: bool = False
    aliases: tuple[str, ...] = ()

    def matches(self, area: str | None) -> bool:
        """Whether a planned area names this city, in any spelling listed."""
        if not area:
            return False
        wanted = " ".join(area.split()).casefold()
        return wanted == self.city.casefold() or wanted in {
            alias.casefold() for alias in self.aliases
        }


class HHCompensation(BaseModel):
    """What the posting pays, in whichever of hh's shapes it arrived.

    Optional fields rather than a union of the shapes seen: 22 pages produced
    six distinct key sets and there is no reason to believe that is all of them.
    ``noCompensation`` is not modelled here at all — it is a sibling key that
    means the object carries no salary, and it is checked before this model is
    built, because ``{"noCompensation": {}}`` is a perfectly truthy dict and
    every ``if compensation:`` written against it is a silent bug.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    amount_from: Decimal | None = Field(default=None, alias="from", ge=0)
    amount_to: Decimal | None = Field(default=None, alias="to", ge=0)
    currency_code: str | None = Field(default=None, alias="currencyCode", max_length=3)
    gross: bool | None = None
    #: What the amount is per: MONTH, SHIFT, HOUR, FLY_IN_FLY_OUT, SERVICE.
    mode: str | None = Field(default=None, max_length=40)
    #: How often it is paid: MONTHLY, TWICE_PER_MONTH, WEEKLY, DAILY,
    #: PER_PROJECT. A payment schedule, not a rate, and never a period.
    frequency: str | None = Field(default=None, max_length=40)
    per_mode_from: Decimal | None = Field(default=None, alias="perModeFrom", ge=0)
    per_mode_to: Decimal | None = Field(default=None, alias="perModeTo", ge=0)


class HHArea(BaseModel):
    """Where the job is, as hh's region tree names it."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    area_id: int | None = Field(default=None, alias="@id")
    #: ISO 3166-1 alpha-2, and the only trustworthy country on the page.
    country_iso: str | None = Field(default=None, alias="@countryIsoCode", max_length=2)
    name: str | None = Field(default=None, max_length=120)


class HHCompany(BaseModel):
    """The employer, minus the parts that are branding or contact details."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    name: str | None = Field(default=None, max_length=500)
    visible_name: str | None = Field(default=None, alias="visibleName", max_length=500)
    site_url: str | None = Field(default=None, alias="companySiteUrl", max_length=1000)
    #: Accredited IT employer: in Kazakhstan and Russia this is a concrete
    #: eligibility fact for the candidate, not a badge.
    accredited_it: bool = Field(default=False, alias="accreditedITEmployer")
    #: hh is itself checking this employer. A useful filter against the postings
    #: that turn out to be recruitment farms.
    on_additional_check: bool = Field(default=False, alias="employerOnAdditionalCheck")
    trusted: bool = Field(default=False, alias="@trusted")

    @property
    def display_name(self) -> str | None:
        """What to show, preferring the name the employer chose."""
        return self.visible_name or self.name


class HHStatus(BaseModel):
    """The five flags that say whether a posting is live."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    active: bool = False
    archived: bool = False
    disabled: bool = False
    need_fix: bool = Field(default=False, alias="needFix")
    waiting: bool = False

    @property
    def is_live(self) -> bool:
        """Whether this posting is one a candidate could still apply to."""
        return self.active and not (self.archived or self.disabled)


class HHVacancyView(BaseModel):
    """``state["vacancyView"]``: the posting itself.

    ``extra="ignore"`` is mandatory rather than convenient — hh adds keys
    without notice, and this model already ignores dozens of them. What is
    declared is what we read; everything dropped is dropped in
    :meth:`HHSource._posting`, where the reason can be written down.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    vacancy_id: int = Field(alias="vacancyId")
    name: str = Field(min_length=1, max_length=1000)
    description: str | None = None
    #: Raw, because whether it carries a salary is decided by a key's presence
    #: and a model cannot express that without losing the distinction.
    compensation: dict[str, Any] | None = None
    area: HHArea | None = None
    company: HHCompany | None = None
    status: HHStatus | None = None
    key_skills: Any = Field(default=None, alias="keySkills")
    work_formats: Any = Field(default=None, alias="workFormats")
    professional_role_ids: Any = Field(default=None, alias="professionalRoleIds")
    civil_law_contracts: Any = Field(default=None, alias="civilLawContracts")
    driver_licence_types: Any = Field(default=None, alias="driverLicenseTypes")
    languages: Any = None
    work_schedule_by_days: Any = Field(default=None, alias="workScheduleByDays")
    working_hours: Any = Field(default=None, alias="workingHours")
    #: Bumped by auto-renewal, so a publication date and not a creation one.
    published_at: AwareDatetime | None = Field(default=None, alias="publicationDate")
    expires_at: AwareDatetime | None = Field(default=None, alias="validThroughTime")
    employment_form: str | None = Field(default=None, alias="employmentForm", max_length=40)
    work_experience: str | None = Field(default=None, alias="workExperience", max_length=40)
    closed_for_applicants: bool = Field(default=False, alias="closedForApplicants")
    #: Only the city is read from it; the rest is a street address and a map.
    address: dict[str, Any] | None = None
    #: Human-readable renderings of the coded fields, shipped with the page.
    translations: dict[str, Any] | None = None
    #: Employer billing on one side, derived publication flags on the other.
    #: Only the second half survives into ``raw``.
    vacancy_properties: dict[str, Any] | None = Field(default=None, alias="vacancyProperties")
    #: The contact person's *activity*, and nothing else about them. hh puts a
    #: name and sometimes a photograph in the same block; only
    #: ``latestActivity`` is read, because "when did this employer last look at
    #: their inbox" is a fact about the posting, and the rest is a person's
    #: identity that this anonymous read-only crawler has no business keeping.
    employer_manager: dict[str, Any] | None = Field(default=None, alias="employerManager")
    #: How many people have already applied, where the page states it. Not on
    #: every posting, and absent is not zero.
    responses_count: int | None = Field(default=None, alias="responsesCount")

    @field_validator("published_at", "expires_at", mode="before")
    @classmethod
    def _blank_is_absent(cls, value: Any) -> Any:
        """hh writes an empty string where a date is unset on some pages."""
        return None if isinstance(value, str) and not value.strip() else value


class HHSalary(BaseModel):
    """The salary, once it has been read out of whichever shape carried it."""

    model_config = ConfigDict(frozen=True)

    min: Decimal | None = None
    max: Decimal | None = None
    currency: str | None = None
    is_gross: bool | None = None
    #: None when hh's ``mode`` has no honest equivalent; see MODE_TO_PERIOD.
    period: SalaryPeriod | None = None
    #: Kept verbatim so a later phase can widen the mapping without a re-crawl.
    mode: str | None = None
    frequency: str | None = None


class HHDerived(BaseModel):
    """Everything the connector worked out, in one validated block.

    It travels in ``RawPosting.raw["_derived"]`` — the seam JSearch already
    uses — because ``VacancyCreate`` today accepts a title, a company, a
    description and a fingerprint, and normalisation proper is phase 4's. Doing
    the reading here anyway is not premature: the shapes are hh's, they are
    ugly, they were measured once, and re-deriving them later from a stored
    payload would mean measuring them again.

    A model rather than a dict, per CLAUDE.md rule 3.
    """

    model_config = ConfigDict(frozen=True)

    external_id: str
    url: str
    city: str | None = None
    country: str | None = None
    remote: RemoteType = RemoteType.NO
    salary: HHSalary | None = None
    published_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    #: hh's own structured requirement list. For an hh posting this is an exact
    #: set to intersect with the profile's skills, which is strictly better than
    #: asking a model to guess the same list out of the prose. Matching should
    #: branch on its presence.
    key_skills: tuple[str, ...] = ()
    #: The language requirements hh renders into the same list, kept apart so
    #: the skill intersection stays a skill intersection. Verbatim, because the
    #: level and its label are both in the string and neither is ours to parse
    #: into a vocabulary this project does not yet have.
    language_requirements: tuple[str, ...] = ()
    professional_role_ids: tuple[int, ...] = ()
    work_formats: tuple[str, ...] = ()
    employment_form: str | None = None
    work_experience: str | None = None
    #: The rendered form of the coded fields above, from the page's own
    #: dictionary, so no vocabulary is hardcoded in this repository.
    labels: dict[str, str] = Field(default_factory=dict)
    closed_for_applicants: bool = False
    accredited_it_employer: bool = False
    employer_on_additional_check: bool = False
    #: When the employer was last active on hh, as the page states it. What the
    #: dashboard renders as «был онлайн»: a posting whose employer has not
    #: opened hh in three weeks is one an application disappears into, and that
    #: is worth knowing *before* writing a letter for it. Published by the
    #: employer themselves; nothing is looked up anywhere else.
    employer_last_activity: AwareDatetime | None = None
    #: Applications already sent, where hh publishes the figure. None means the
    #: page did not state it, which is not the same as nobody having applied.
    responses_count: int | None = None
    #: From ``calculatedStates``, never from the billing block beside it.
    anonymous: bool = False
    advertising: bool = False
    pay_for_performance: bool = False
    #: The description as hh sent it. The cleaned text goes to
    #: ``RawPosting.description`` and from there into the column the embedding
    #: reads; keeping the markup here means the dashboard can render it and a
    #: later HTML-to-markdown pass needs no re-fetch.
    description_html: str | None = None
    #: The sitemap timestamp this page was fetched for. Not a freshness signal —
    #: auto-renewal moves it — but it is what the cache and the watermark are
    #: keyed on, so it belongs with the payload.
    sitemap_lastmod: AwareDatetime | None = None


class SitemapEntry(BaseModel):
    """One line of a vacancy sitemap: a page and when hh last touched it."""

    model_config = ConfigDict(frozen=True)

    external_id: str
    url: str
    lastmod: AwareDatetime


#: A key in the order the walk uses. Newest first, ties broken by id so that a
#: boundary can fall between two entries sharing a second without either being
#: repeated or skipped — which is what the old ``ids_at_lastmod`` list was for.
type EntryKey = tuple[datetime, str]


def _key(entry: SitemapEntry) -> EntryKey:
    """One entry's place in the walk's total order."""
    return (entry.lastmod, entry.external_id)


class Span(BaseModel):
    """One stretch of a sitemap file this crawl has finished, closed at both ends."""

    model_config = ConfigDict(frozen=True)

    low_lastmod: AwareDatetime
    low_id: str
    high_lastmod: AwareDatetime
    high_id: str

    @property
    def low(self) -> EntryKey:
        """The oldest entry in the stretch."""
        return (self.low_lastmod, self.low_id)

    @property
    def high(self) -> EntryKey:
        """The newest entry in the stretch."""
        return (self.high_lastmod, self.high_id)

    def holds(self, key: EntryKey) -> bool:
        """Whether this stretch covers that entry."""
        return self.low <= key <= self.high

    @classmethod
    def between(cls, low: EntryKey, high: EntryKey) -> "Span":
        """A stretch from one key to another, inclusive."""
        return cls(low_lastmod=low[0], low_id=low[1], high_lastmod=high[0], high_id=high[1])


class FileWatermark(BaseModel):
    """Which parts of one sitemap file this crawl has finished.

    **Not "how far it got".** It was a single timestamp meaning "everything
    older than this is done", which is the right shape for a walk that goes
    oldest-first, and that is the walk this connector used to do.

    It now goes newest-first, because the freshest postings are the ones worth
    applying to and a run is short: at hh's tolerated rate a page takes four to
    five seconds, and both live runs were stopped by a check for robots well
    inside a hundred pages. A run that spends its budget on the oldest end of a
    corpus of some 13 557 postings has spent it on the postings least likely to
    still be open.

    A frontier cannot describe that. Walk newest-first and the covered set grows
    downward from the top, and next run hh has published more above it — so the
    covered set is an interval, not a prefix, and after an interrupted run it can
    be two. Any single number describing that either claims the gap is done,
    which loses those postings for good, or claims the covered part is not, which
    buys every page again every run. Both were tried; the second is what left
    ``source_state`` empty.

    So it is a list of stretches over the same total order the walk sorts by.
    Stretches are recomputed from the file's own entry list each time anything is
    recorded, which is what lets neighbours merge: two are neighbours exactly
    when no entry of that file falls between them, and only the list can say.
    """

    model_config = ConfigDict(frozen=True)

    covered: tuple[Span, ...] = ()

    def is_done(self, entry: SitemapEntry) -> bool:
        """Whether some previous run already covered this entry."""
        key = _key(entry)
        return any(span.holds(key) for span in self.covered)

    def outstanding(self, entries: Sequence[SitemapEntry]) -> list[SitemapEntry]:
        """Those entries no previous run covered, newest first.

        The same answer :meth:`is_done` gives one entry at a time, computed once
        for the file. Asking per entry is a stretch-count multiplied by an
        entry-count — 1387 entries against the stretches a role-first walk leaves
        behind — and it runs on every file at the start of every run. The
        stretches are disjoint (:func:`_collapse` guarantees it), so the one that
        could hold a key is the last one starting at or below it.
        """
        if not self.covered:
            return sorted(entries, key=_key, reverse=True)
        spans = sorted(self.covered, key=lambda span: span.low)
        lows = [span.low for span in spans]
        due: list[SitemapEntry] = []
        for entry in entries:
            key = _key(entry)
            index = bisect_right(lows, key) - 1
            if index >= 0 and spans[index].high >= key:
                continue
            due.append(entry)
        return sorted(due, key=_key, reverse=True)

    def covering(self, order: Sequence[EntryKey], done: Iterable[EntryKey]) -> "FileWatermark":
        """This mark, extended to cover ``done``.

        ``order`` is the file's whole entry list as this run read it, newest
        first. Recomputed rather than patched: the input is a few thousand keys,
        this runs once per confirmed batch, and a merge that is wrong in the
        patching direction is a silently lost posting.

        Stretches whose entries this run did not see at all are kept untouched —
        a file that shrank must not silently uncover what an earlier run paid
        for — and on overflow the OLDEST is dropped rather than merged into its
        neighbour. Dropping means those entries are due again; merging would mean
        claiming entries nobody fetched. Repeating costs requests, losing costs
        data.
        """
        rank = {key: index for index, key in enumerate(order)}
        ascending = sorted(rank)

        # Which entries of this file are already covered, found by bisecting the
        # file's own keys once per stretch rather than by asking every stretch
        # about every entry. That was quadratic and invisible while a file held
        # at most a handful of stretches; the walk now leaves dozens per run, and
        # this method runs after every confirmed batch.
        marked: set[int] = set()
        unseen: list[Span] = []
        for span in self.covered:
            low = bisect_left(ascending, span.low)
            high = bisect_right(ascending, span.high)
            if low >= high:
                # A stretch this run saw no entry of. Kept untouched: a file that
                # shrank must not silently uncover what an earlier run paid for.
                unseen.append(span)
                continue
            marked.update(rank[key] for key in ascending[low:high])
        marked.update(rank[key] for key in done if key in rank)

        spans: list[Span] = []
        previous: int | None = None
        for index in sorted(marked):
            if previous is not None and index == previous + 1:
                spans[-1] = Span.between(order[index], spans[-1].high)
            else:
                spans.append(Span.between(order[index], order[index]))
            previous = index

        return FileWatermark(covered=_collapse((*unseen, *spans)))

    def is_covered(self, key: EntryKey) -> bool:
        """Whether that key falls inside any finished stretch."""
        return any(span.holds(key) for span in self.covered)


class CatalogPlan(BaseModel):
    """Which catalogue pages this deployment opens on one host, and where it got to.

    Stored in ``source_state`` rather than recomputed each run, because
    recomputing it costs 16 requests — hh's role directory plus 15 catalogue
    sitemaps — to answer a question whose answer is a list of professions. The
    postings behind those professions are re-read every run; the professions
    themselves are not.

    ``families`` is what makes a stale plan visible. It records which families of
    ``hh_roles.yaml`` asked for this list, so a new resume with a different stack
    re-resolves on the next run instead of crawling the previous candidate's
    professions until :data:`CATALOG_TTL` runs out.

    ``offset`` is the rotation: a run reads :data:`CATALOG_PAGES_PER_RUN` slugs
    starting there and leaves it past them, so consecutive runs name different
    postings. It is saved once, after the pass — a run stopped by a check for
    robots re-reads the same slugs next time, which costs a few requests and
    keeps this out of the hot path.
    """

    model_config = ConfigDict(frozen=True)

    resolved_at: AwareDatetime
    families: tuple[str, ...] = ()
    #: The headline this plan was ranked under. Stored for the same reason the
    #: families are: it decides the order, so a change to it invalidates the plan
    #: even when the families it selected are identical.
    headline: str | None = None
    #: What the profile asked for, as hh's directory numbers it. Not used to
    #: choose pages — the slugs are — but to count how many of the postings a run
    #: bought were actually in those roles, which is the number that says whether
    #: any of this worked.
    role_ids: tuple[int, ...] = ()
    slugs: tuple[str, ...] = ()
    offset: int = Field(default=0, ge=0)


def catalog_sitemaps(body: str, host: str) -> list[tuple[str, str]]:
    """The ``vacancies{N}.xml`` files an index lists, on this host only.

    Host-checked for the reason ``_entry`` checks it: a sitemap is somebody
    else's document and every URL in it is input.
    """
    found = {
        (match.group(1), url)
        for url in SITEMAP_LOC.findall(body)
        if (match := CATALOG_SITEMAP.search(url)) and urlsplit(url).hostname == host
    }
    return sorted(found)


def catalog_entries(body: str, host: str) -> tuple[tuple[str, ...], int, int]:
    """Slugs, the total ``<loc>`` count, and how many entries carry a ``lastmod``.

    Three numbers rather than one because the second and third are what make the
    first trustworthy: recognising 3 of 5 URLs is a different measurement from
    recognising 5 of 5, and the crawl needs to know that the catalogue carries no
    dates at all — that is why it cannot be walked by freshness the way the
    vacancy sitemaps are.
    """
    locs = SITEMAP_LOC.findall(body)
    slugs: list[str] = []
    for url in locs:
        parts = urlsplit(url)
        if parts.hostname != host or parts.query:
            continue
        match = CATALOG_PATH.match(parts.path)
        if match is not None:
            slugs.append(match.group(1))
    return tuple(dict.fromkeys(slugs)), len(locs), body.count("<lastmod>")


def pages_this_run(plan: "CatalogPlan") -> tuple[tuple[str, ...], int]:
    """Which catalogue pages to open now, and where the rotation continues.

    Half from the head of the ranked list and half from a window that moves
    across the tail; see :data:`CATALOG_HEAD_PAGES` for why it is not simply a
    rotation over the whole list. A plan short enough to read in one run is read
    in one run, and the cursor stays where it is.

    Pure and separate from the fetching because it is the decision worth
    testing: everything else in the pass is a request and a regex.
    """
    slugs = plan.slugs
    if not slugs:
        return (), 0
    if len(slugs) <= CATALOG_PAGES_PER_RUN:
        return slugs, plan.offset
    head = slugs[:CATALOG_HEAD_PAGES]
    tail = slugs[len(head) :]
    take = CATALOG_PAGES_PER_RUN - len(head)
    start = plan.offset % len(tail)
    rotating = [tail[(start + step) % len(tail)] for step in range(min(take, len(tail)))]
    return (*head, *rotating), (start + len(rotating)) % len(tail)


def _collapse(spans: Iterable[Span]) -> tuple[Span, ...]:
    """Overlapping stretches merged into one, newest first, capped.

    Two stretches can overlap without being neighbours in the file's order: a
    stretch kept from an earlier run may sit entirely inside a range this run
    walked in one go, which is what happens the first time the walk crosses a gap
    it left behind. Their union is exactly as true as either of them, and leaving
    them overlapping would break the one assumption
    :meth:`FileWatermark.outstanding` makes to stay linear.

    Truncation drops the OLDEST, and says so out loud: those entries are due
    again, which costs requests and never data, but a crawl that silently
    re-bought the same pages every run is what the old cap did.
    """
    collapsed: list[Span] = []
    for span in sorted(spans, key=lambda item: item.low):
        if collapsed and span.low <= collapsed[-1].high:
            if span.high > collapsed[-1].high:
                collapsed[-1] = Span.between(collapsed[-1].low, span.high)
            continue
        collapsed.append(span)
    collapsed.sort(key=lambda item: item.high, reverse=True)
    if len(collapsed) > MAX_SPANS:
        logger.warning(
            "sources.hh.spans_dropped",
            dropped=len(collapsed) - MAX_SPANS,
            kept=MAX_SPANS,
            detail="oldest covered stretches dropped; those entries are due again",
        )
    return tuple(collapsed[:MAX_SPANS])


class FileCensus(BaseModel):
    """How big one sitemap file was, and how much of it was still due.

    Written beside the position rather than inside it, and that separation is
    the point. :class:`FileWatermark` is a claim — "these stretches are
    covered" — and every run that finishes work extends it. This is an
    observation of somebody else's file at one moment, and a run that is
    stopped by a check for robots leaves it exactly as true as it was.
    Merging the two would mean a rescue write during an unwind either
    republishing a stale count as fresh, or dropping the position to avoid it.

    It exists because the position alone cannot answer "how much is left".
    Stretches are intervals over ``(lastmod, id)``; nothing in them counts
    entries, and the file's own size is hh's fact, known only while a run is
    holding the file. So the walk records it there, once per file per run, and
    the overview reads it back. An absent row means no run has counted this
    file since the counting was added — which is reported as *unknown*, never
    as zero, because zero would read as a finished backfill.
    """

    model_config = ConfigDict(frozen=True)

    #: Entries the file held when this run read it.
    total: int = Field(ge=0)
    #: How many of those the position did not yet cover, at that moment.
    outstanding: int = Field(ge=0)


def load_sites(path: Path | None = None) -> tuple[HHSite, ...]:
    """The configured hh sites. Read on demand, not at import.

    The default is resolved here rather than in the signature: a default
    argument is evaluated once, when the function is defined, so
    ``path: Path = SITES_FILE`` would bind that one object forever and the
    module constant would stop being the single place the location is stated.
    """
    path = path or SITES_FILE
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SourceError(
            f"hh: не читается {path.name} со списком городов: {exc}", source_slug="hh"
        ) from exc
    try:
        return tuple(HHSite.model_validate(entry) for entry in raw.get("sites", []))
    except ValidationError as exc:
        raise SourceError(
            f"hh: {path.name} не описывает список городов: {exc.errors()}", source_slug="hh"
        ) from exc


@dataclass(slots=True)
class CrawlBudget:
    """Requests one run may still spend, shared across every site it walks.

    A plain counter rather than a per-site allowance, because the interesting
    question is what the whole run cost hh and not how it was divided. Passed
    down and mutated: an async generator cannot hand a number back to its
    caller, and threading the count through the yields — which the first draft
    of this file did — produces a budget that silently stops applying.
    """

    remaining: int

    def spend(self, requests: int = 1) -> None:
        """Record requests that have been sent."""
        self.remaining -= requests

    def exhausted(self) -> bool:
        """Whether this run has spent what it was allowed.

        A method rather than a property, and not for taste: mypy narrows an
        attribute expression and keeps the narrowing across method calls, so
        with ``budget.exhausted`` as a property the second check in the loop
        below was typed as always-False and ``warn_unreachable`` failed the
        build on a branch that runs on every truncated crawl.
        """
        return self.remaining <= 0


@dataclass(slots=True)
class _SiteRun:
    """What one site's walk has done so far, shared by its two passes.

    A small object rather than a handful of locals because the head pass and the
    ascending pass both add to it and both are inside an async generator, where
    a returned tally has nowhere to go.
    """

    site: HHSite
    fetched: int = 0
    stored: int = 0
    #: Pages that were bought and produced nothing storable: taken down since
    #: the sitemap was written, archived, or answering for a different vacancy.
    #: One counter rather than three, because the log is read to answer "how
    #: much of what we paid for was worth keeping" and the reasons are already
    #: in the debug lines beside it.
    not_stored: int = 0
    unreadable: int = 0
    #: Pages fetched because the catalogue named them, i.e. in the role pass
    #: rather than in the general walk behind it.
    role_pass: int = 0
    #: Postings that turned out to carry one of the professional roles the
    #: profile asked for. Together with ``fetched`` this is the number the whole
    #: catalogue path exists to move: relevant postings per request spent.
    role_hits: int = 0


@dataclass(slots=True)
class _CatalogPass:
    """What one run's reading of the catalogue produced.

    ``ids`` is the whole of the effect on the walk: an entry whose id is in it
    is fetched before the entries that are not, and nothing else changes. The
    counters beside it are for the log line, which is where a person finds out
    whether the catalogue is earning its twenty requests.
    """

    ids: frozenset[str] = frozenset()
    role_ids: frozenset[int] = frozenset()
    #: The catalogue pages this run actually opened, in the order it opened
    #: them. Carried rather than counted because it is what the run report has
    #: to show: which eight of a hundred pages were bought.
    opened: tuple[str, ...] = ()
    slugs: int = 0


@dataclass(slots=True)
class _Held:
    """One walked entry, waiting for the pipeline to confirm its posting is written.

    ``after`` is how many postings the walk had yielded once this entry was
    finished with. It is safe to record when the pipeline says it has written
    that many: for an entry that stored nothing, as soon as everything ahead of
    it is written; for one that stored a posting, when that posting is in the
    database.

    This replaced a fixed lag, which was the same number guessed by the party
    that cannot know it. See ``MAX_HELD`` and ``BaseSource.record_progress``.
    """

    site: HHSite
    name: str
    entry: SitemapEntry
    after: int


@register_source
class HHSource(BaseSource):
    """HeadHunter, read through its own sitemap."""

    slug: ClassVar[str] = "hh"
    name: ClassVar[str] = "HeadHunter"
    regions: ClassVar[tuple[str, ...]] = ("KZ",)
    #: Pages written for people, so robots.txt governs and the shared client
    #: fetches it. Stated rather than inherited because it is the whole basis on
    #: which this connector is allowed to exist.
    access_mode: ClassVar[AccessMode] = AccessMode.CRAWL
    requires_auth: ClassVar[bool] = False
    required_credentials: ClassVar[tuple[str, ...]] = ()
    #: One request a second, two in hand. hh publishes no Crawl-delay for the
    #: wildcard group — the only one in the file belongs to bingbot — so this is
    #: our own restraint rather than their instruction, and we are a guest here.
    #: One request every four to five seconds, and the numbers are measured
    #: rather than chosen for comfort. Two live runs against almaty.hh.kz:
    #:
    #:     0.73 rps -> hh answered with a captcha at the 172nd posting
    #:     1.02 rps -> hh answered with a captcha at the 50th posting
    #:
    #: Both runs were inside robots.txt and neither used an account, so the
    #: limit hh is enforcing here is a rate, not a permission. A crawl that is
    #: stopped after fifty pages collects nothing and costs the account its
    #: standing, so the slower rate is not a concession — it is the only rate at
    #: which the corpus grows at all.
    #:
    #: ``burst=1`` because a burst of two is two requests in the same instant,
    #: which is the shape being avoided. The jitter puts the real interval
    #: between four and five seconds, i.e. 0.20-0.25 requests a second.
    #:
    #: The cost is stated plainly: at this rate ``MAX_PAGES_PER_RUN`` pages take
    #: about ninety minutes. That is why the walk goes newest-first — see
    #: ``search_batch`` — because a run that is interrupted should have spent
    #: its time on the postings worth applying to.
    rate_limit: ClassVar[RateLimit] = RateLimit(
        requests_per_second=0.25, burst=1, jitter_seconds=1.0
    )
    #: The walk fetches the page itself; there is nothing left to fill in.
    needs_detail_fetch: ClassVar[bool] = False
    #: Not metered by hh. Counting our own requests in a second place would only
    #: create two numbers that can disagree; the page budget is the bound.
    daily_quota: ClassVar[int | None] = None
    #: Long, because freshness here is not a function of time. Every page is
    #: requested with the sitemap's ``lastmod`` as a cache salt, so an edited
    #: posting misses the cache and an untouched one hits it — the brief's
    #: "invalidate on the vacancy changing, not on a TTL", expressed as a key.
    #: The TTL is only a floor on how long a developer's disk keeps the bytes.
    cache_ttl: ClassVar[timedelta] = timedelta(days=30)
    #: A crawl this size should not restart minutes after finishing. The
    #: pipeline's own incremental cadence is three hours, which at this budget
    #: covers the measured churn several times over, so this is a floor rather
    #: than the real schedule.
    min_interval: ClassVar[timedelta] = timedelta(hours=1)

    def __init__(self, *, http: "SourceHTTP | None" = None) -> None:
        super().__init__(http=http)
        self._sites: tuple[HHSite, ...] | None = None
        #: Entries the walk has finished with, waiting for the pipeline to say
        #: their postings are written. Held on the instance rather than in the
        #: walk because the confirmation arrives from outside the generator —
        #: after the pipeline's write — and a local of an async generator has
        #: nowhere to be reached from.
        self._held: list[_Held] = []
        #: Each walked file's entry list, newest first, kept for the run. It is
        #: what tells two finished stretches they are neighbours; see
        #: ``FileWatermark.covering``.
        self._order: dict[tuple[str, str], list[EntryKey]] = {}
        #: ``hh_roles.yaml``, read once per instance like the site list.
        self._families: tuple[RoleFamily, ...] | None = None
        #: hh's role directory, fetched at most once per run: it is one document
        #: for every host, and a second city must not pay for it again.
        self._directory: tuple[DirectoryRole, ...] | None = None
        #: Each host's sitemap index, kept for the run so that the vacancy files
        #: and the catalogue files are read out of one request rather than two.
        self._index: dict[str, str] = {}

    @property
    def sites(self) -> tuple[HHSite, ...]:
        """The configured sites, read from the file once per instance."""
        if self._sites is None:
            self._sites = load_sites()
        return self._sites

    @property
    def families(self) -> tuple[RoleFamily, ...]:
        """The configured profile-to-roles families, read once per instance."""
        if self._families is None:
            self._families = load_families()
        return self._families

    def sites_for(self, queries: Sequence[SearchQuery]) -> tuple[HHSite, ...]:
        """Which hosts this plan asks for.

        A planned area is free text out of the candidate's resume, so it is
        matched against the aliases in the file rather than parsed. A plan that
        names no city we serve — which is what a remote-only or relocation-only
        profile produces — gets the sites marked ``default``: hh is a regional
        site, and reading the city this deployment exists for is a better answer
        than reading nothing at all.
        """
        wanted = [site for site in self.sites if any(site.matches(q.area) for q in queries)]
        return tuple(wanted) if wanted else tuple(site for site in self.sites if site.default)

    # ── fetching ──────────────────────────────────────────────────────

    async def search_batch(self, queries: Sequence[SearchQuery]) -> AsyncIterator[RawPosting]:
        """Walk the corpus once for the whole plan.

        The base implementation runs one ``search`` per query, which is right
        for a source you can ask a question. hh cannot be asked one: its sitemap
        holds a URL and a date, so every query would re-walk the same pages.

        **The plan's keywords never filter a posting, and they now choose which
        pages to open.** Those are opposite things and the difference is the
        whole of the change of 2026-09-08. Filtering after a fetch saves no
        request — the sitemap carries no title, so the page is already paid for
        — and it is worse than useless besides: the walk records how far it got,
        so a posting dropped for today's keywords is marked as dealt with and no
        future run fetches it. Upload a new CV and everything the old keyword set
        rejected stays invisible forever. That reasoning stands, and nothing here
        filters.

        Choosing a catalogue page happens BEFORE the request, which is why it is
        allowed to use the same words. hh publishes a page per profession —
        ``/vacancies/programmist``, measured with 50 vacancy ids on it — and the
        ids on the pages this profile's professions point at are fetched first.
        The rest of the corpus follows in the same walk, newest first, on
        whatever budget is left. Relevance still belongs to ``matching/``; what
        this decides is only what a short run spends its requests on.

        Why that matters, measured on 2026-09-08: of 294 hh postings collected by
        a walk ordered purely by date, none were in the programmer, developer or
        devops roles, the commonest role was sales, and scoring the result put
        zero postings in "apply" and zero in "strong match". The connector was
        working exactly as written and collecting the wrong corpus.
        """
        keywords = tuple(sorted({word for query in queries for word in query.keywords}))
        # One headline per plan — it is the profile's, not the query's — so the
        # first one any query carries is the one. Queries built by hand carry
        # none, and the ranking then falls back to the keywords alone.
        headline = next((query.headline for query in queries if query.headline), None)
        sites = self.sites_for(queries)
        if not sites:
            logger.warning("sources.hh.no_sites_configured", queries=len(queries))
            return
        families = families_for(keywords, self.families)
        if keywords and not families:
            # Not an error and not silent. A profile whose stack this file has
            # never seen still gets a crawl, and its own words still pick
            # catalogue pages — see ``hh_roles.slugs_for``. What it does not get
            # is somebody else's professions as a default.
            logger.warning(
                "sources.hh.no_role_families",
                keywords=list(keywords),
                detail="profile matches no family in hh_roles.yaml; slugs come from keywords only",
            )
        else:
            logger.info(
                "sources.hh.role_families",
                families=[family.key for family in families],
                # Which of them the headline named, as opposed to which got in
                # on a skill the candidate happens to list. Both are crawled —
                # the brief asks for breadth — but only the first kind outranks
                # the other in ``hh_roles._rank``, and a run that opened nothing
                # but Go and C# pages is what happens when nobody can see the
                # difference from the outside.
                focus=[
                    family.key for family in families if headline and carries(headline, family.when)
                ],
                headline=headline,
                keywords=len(keywords),
            )

        budget = CrawlBudget(remaining=MAX_PAGES_PER_RUN)
        # Every city gets an equal share. One shared counter walked in order
        # would mean the first city in the file takes the whole budget for as
        # long as its backfill lasts — about a dozen runs — while the others
        # report a clean, successful, empty crawl.
        #
        # What a city does not spend is NOT handed to another one inside the
        # same run. Doing that means walking a site twice, and the second walk
        # re-reads its index and every sitemap and re-buys its head slice, which
        # records no position by design. The unspent budget is not lost; the
        # next run spends it, starting where this one stopped.
        share = max(1, MAX_PAGES_PER_RUN // len(sites))
        for site in sites:
            async for posting in self._crawl_site(
                site,
                budget,
                allowance=share,
                keywords=keywords,
                families=families,
                headline=headline,
            ):
                yield posting
        # No tail flush any more. The walk records nothing on its own: every
        # entry it finishes waits on ``self._held`` until the pipeline confirms
        # the posting is written, and the pipeline confirms after its last write
        # too — so a drained file's remainder is recorded by that confirmation
        # rather than by a hand-off window this generator had to open for it.

    async def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]:
        """One query's worth of the same walk.

        Present because ``BaseSource`` requires it and a caller may hold this
        connector directly. It delegates, so there is one crawl loop to reason
        about rather than two.
        """
        async for posting in self.search_batch([query]):
            yield posting

    async def _crawl_site(
        self,
        site: HHSite,
        budget: CrawlBudget,
        *,
        allowance: int,
        keywords: Sequence[str] = (),
        families: Sequence[RoleFamily] = (),
        headline: str | None = None,
    ) -> AsyncIterator[RawPosting]:
        """Walk one host: the index, the catalogue, then the outstanding entries.

        **The professions first, then everything else, in one walk.** The
        catalogue names which postings belong to the professions the profile
        asked for; those are fetched before the rest, and the rest follows in the
        same order it always had — newest first, on whatever budget is left. The
        second half is not a fallback, it is the half that keeps the corpus
        varied and covers the professions ``hh_roles.yaml`` failed to name.

        **Why the ids are intersected with the sitemap rather than fetched
        directly.** A catalogue page gives an id and nothing else. The sitemap
        gives the same id with the date hh last touched it and the file it lives
        in, which is what the recorded position is made of. Intersecting means
        the role pass is a REORDERING of the ordinary walk: every entry it
        fetches is recorded, resumed and deduplicated by machinery that already
        works. The cost is stated rather than hidden — an id on a catalogue page
        that is in no sitemap file is not fetched, and is counted in the log —
        because a page with no place to record it would be re-bought every run
        forever, and a crawl that cannot finish is worse than one that is late.

        **What paging would have bought, and why there is none.** Measured
        2026-09-08: the catalogue's own next-page links exist only as
        ``?page=0..3``, which hh's ``Disallow: *?*`` closes to us, and there is
        no query-less form of them. So one page per slug, 50 ids, and depth comes
        from the breadth of the slug set instead — 630 of Almaty's 10 435 slugs
        were development. The overlap that breadth produces costs nothing: the
        same posting reached through two professions is one row, collapsed by the
        fingerprint the pipeline already computes.
        """
        site_budget = CrawlBudget(remaining=min(allowance, budget.remaining))

        def spend() -> None:
            budget.spend()
            site_budget.spend()

        def stop() -> bool:
            return budget.exhausted() or site_budget.exhausted()

        if stop():
            return
        files = await self._vacancy_sitemaps(site)
        spend()
        catalog = await self._catalog_ids(
            site,
            keywords=keywords,
            families=families,
            headline=headline,
            spend=spend,
            stop=stop,
        )

        due: dict[str, list[SitemapEntry]] = {}
        marks: dict[str, FileWatermark] = {}
        for name, url in files:
            if stop():
                logger.info("sources.hh.file_not_read", host=site.host, file=name)
                continue
            entries = await self._sitemap_entries(site, url)
            spend()
            # Read once and kept: the same mark decides what is due and then
            # advances over it, and nothing writes in between. Reading it twice
            # would be two sources of truth for one number.
            marks[name] = await self._watermark(site, name)
            # The file's whole entry list, newest first, kept for the run. It is
            # what lets two finished stretches be recognised as neighbours when
            # the position is recorded — only this list can say whether anything
            # falls between them — and it is the order the walk itself uses.
            order = sorted((_key(entry) for entry in entries), reverse=True)
            self._order[(site.host, name)] = order
            due[name] = marks[name].outstanding(entries)
            # The one moment anybody knows how big this file is. Recorded here,
            # before a single page is fetched, so a run that hh stops at its
            # first request still leaves the overview able to say how much of
            # the corpus is outstanding.
            await self._save_census(
                site, name, FileCensus(total=len(entries), outstanding=len(due[name]))
            )
        outstanding = sum(len(entries) for entries in due.values())

        state = _SiteRun(site=site)
        # ONE pass, strictly newest first, across every file of the site.
        #
        # It used to be two: a fifty-page slice of the newest entries, then the
        # rest oldest-first. That shape came from a position that was a frontier
        # through time, which could only move in one direction, so freshness had
        # to be bolted on in front of it. The position is a set of stretches now
        # and the walk can simply take the newest thing outstanding.
        #
        # Which matters because a run is short. At the rate hh tolerates a page
        # costs four to five seconds, and both live runs were stopped by a check
        # for robots inside a hundred pages. The whole corpus is not the goal —
        # some 13 557 postings for one city, most of them long filled — so what a
        # run must not do is spend its budget at the old end. Older entries are
        # reached with what is left after the fresh ones, which is exactly what a
        # descending walk over "everything not yet covered" does without needing
        # a second phase to say so.
        walk = sorted(
            ((name, entry) for name, entries in due.items() for entry in entries),
            key=lambda pair: _key(pair[1]),
            reverse=True,
        )
        # The professions first, and only that. A stable sort on one boolean
        # keeps the newest-first order inside both halves, so this adds an
        # ordering and takes nothing away: every entry that was due before is
        # still due, in the same relative place among its own kind.
        walk.sort(key=lambda pair: pair[1].external_id not in catalog.ids)
        reachable = sum(1 for _, entry in walk if entry.external_id in catalog.ids)
        if catalog.ids:
            logger.info(
                "sources.hh.role_pass_planned",
                host=site.host,
                opened=list(catalog.opened),
                catalog_ids=len(catalog.ids),
                # The gap between these two is the cost named in the docstring:
                # ids the catalogue offered that no sitemap file of this host
                # accounts for, plus ids earlier runs already covered.
                due_now=reachable,
                outstanding=outstanding,
            )
        try:
            for index, (name, entry) in enumerate(walk):
                if stop():
                    logger.info(
                        "sources.hh.budget_reached",
                        host=site.host,
                        # What a run could not reach has to be visible, or a
                        # truncated crawl reads as a completed one.
                        remaining=len(walk) - index,
                        outstanding_on_site=outstanding,
                        held=len(self._held),
                    )
                    break
                if entry.external_id in catalog.ids:
                    state.role_pass += 1
                posting = await self._fetch_counted(entry, state)
                spend()
                if posting is not None:
                    if self._in_wanted_roles(posting, catalog.role_ids):
                        state.role_hits += 1
                    yield posting
                # ``after``, not ``before``: an entry that stored nothing is
                # accounted for as soon as everything ahead of it is written, and
                # one that stored a posting only when that posting is. A stretch
                # of pages that yield nothing — taken down, archived, answering
                # for another vacancy — is common in a corpus this size, and
                # counting them by entry rather than by posting is what would
                # push the position past unwritten work.
                self._hold(site, name, entry, state.stored)
        except Exception:
            # Re-raised untouched; nothing here classifies it. The held entries
            # are not dropped: the pipeline rescues the batch it was holding and
            # confirms it while this unwinds, and that confirmation is what
            # records them.
            raise

        self._log_site(site, state, budget, outstanding, catalog)

    def _log_site(
        self,
        site: HHSite,
        state: "_SiteRun",
        budget: CrawlBudget,
        outstanding: int,
        catalog: "_CatalogPass",
    ) -> None:
        """One line per site, carrying what was covered and what was not.

        ``role_hits`` against ``fetched`` is the measurement the catalogue path
        was built to move, and it is reported per run rather than left to be
        reconstructed from the database: before this change a walk ordered by
        date returned 294 hh postings for Almaty with none of them in a
        development role.
        """
        logger.info(
            "sources.hh.site_finished",
            host=site.host,
            outstanding=outstanding,
            fetched=state.fetched,
            stored=state.stored,
            not_stored=state.not_stored,
            unreadable=state.unreadable,
            catalog_pages=len(catalog.opened),
            catalog_slugs=catalog.slugs,
            role_pass=state.role_pass,
            role_hits=state.role_hits,
            budget_left=max(0, budget.remaining),
        )

    def _in_wanted_roles(self, posting: RawPosting, roles: frozenset[int]) -> bool:
        """Whether this posting carries one of the roles the profile asked for.

        Reads ``_derived``, which is this connector's own block and not a foreign
        payload — the rule against raw dicts between layers is about contracts
        crossing a boundary, and this one has not left the module that wrote it.
        Counting rather than filtering: a posting outside the wanted roles is
        stored exactly as before, because the scorer is what narrows and it has
        been honest all along.
        """
        if not roles:
            return False
        derived = posting.raw.get("_derived")
        if not isinstance(derived, dict):
            return False
        found = derived.get("professional_role_ids")
        if not isinstance(found, list):
            return False
        return any(isinstance(role, int) and role in roles for role in found)

    async def _fetch_counted(self, entry: SitemapEntry, state: "_SiteRun") -> RawPosting | None:
        """One page, counted, with a run's tolerance for unreadable markup.

        Loud, but only once it is a pattern. A single odd page — a truncated
        response, a posting caught mid-edit — must not wedge the crawl, because
        the walk resumes at the same entry every run and would never get past
        it. Three in one run is not an odd page: it is hh having moved the state
        we parse, which is the failure this connector exists to announce rather
        than absorb.

        Corrected 2026-09-07. This used to say that the raise leaves the
        position unsaved past its last periodic write, costing at most
        ``WATERMARK_SAVE_EVERY`` re-bought pages, and that this was the cheaper
        half of the trade, because catching it to save the mark would mean
        recording progress through a file we have just decided we can no longer
        read. The second half of that is wrong, and the wrongness is worth
        keeping visible: the mark does not record progress through the file, it
        records postings already committed to the database. Whether the NEXT
        page parses has no bearing on whether the last hundred did. So the walk
        in ``_crawl_site`` now saves the mark as this exception unwinds, and the
        entry that failed is never in it — the raise happens before that entry
        is appended to ``pending``, which is the mechanism, not a coincidence.
        """
        state.fetched += 1
        try:
            posting = await self._fetch(state.site, entry)
        except HHChallengedError:
            # What this clause does and does not do, because the difference was
            # worth an argument. It does NOT keep the challenge out of the
            # markup tolerance below: that is a property of the type — a
            # challenge is not an HHMarkupError, so the counter never sees one —
            # and it would hold with these lines deleted. Deleting them changes
            # exactly one thing, and this is it: the log line naming the host
            # and the page hh stopped us on, which is the only record of where
            # a crawl was cut and is what somebody deciding whether to walk that
            # host more slowly reads. The pipeline sees the exception but not
            # which page it happened on.
            #
            # The comment is also the place to say that the accident is the
            # wanted behaviour rather than a lucky one: hh has decided something
            # about this crawler, and the answer is to stop walking every host
            # of theirs — not to try two more pages first and then report a
            # markup change that did not happen.
            #
            # ``test_a_challenge_names_the_host_and_the_page_in_the_log``
            # asserts the event, so removing this clause fails a test instead of
            # quietly losing the line.
            logger.warning(
                "sources.hh.challenged",
                host=state.site.host,
                url=entry.url,
                fetched=state.fetched,
                stored=state.stored,
            )
            raise
        except HHMarkupError:
            state.unreadable += 1
            if state.unreadable >= MAX_MARKUP_FAILURES:
                raise
            logger.warning(
                "sources.hh.markup_unreadable",
                url=entry.url,
                failures=state.unreadable,
                of=MAX_MARKUP_FAILURES,
            )
            return None
        if posting is None:
            state.not_stored += 1
            return None
        state.stored += 1
        return posting

    async def _vacancy_sitemaps(self, site: HHSite) -> list[tuple[str, str]]:
        """The ``vacancy{N}.xml`` files this host's index lists, in order.

        An allow-list anchored on the whole file name, never a substring test.
        ``resumes{N}.xml`` is the one that matters — those are living people's
        CVs — and ``vacancies{N}.xml`` differs from ``vacancy{N}.xml`` by three
        letters while holding some eighty thousand SEO landing pages. What the
        pattern rejects is counted and logged, so a new family appearing in the
        index shows up in a run log instead of nowhere.
        """
        index_url = f"https://{site.host}{SITEMAP_INDEX_PATH}"
        body = await self.http.get_text(index_url, cache_ttl=SITEMAP_CACHE_TTL)
        # Kept for the run so that the catalogue files, which live in the same
        # document, cost no second request. Not a cache with a lifetime: it dies
        # with the connector instance, which the registry builds once per run.
        self._index[site.host] = body
        listed = SITEMAP_LOC.findall(body)
        # The host is checked here for the reason ``_entry`` checks it on the
        # vacancy URLs: a sitemap is a document somebody else writes, and every
        # URL in it is input. The pattern is anchored on the file name and would
        # happily match one on another host.
        found = {
            (match.group(1), url)
            for url in listed
            if (match := VACANCY_SITEMAP.search(url)) and urlsplit(url).hostname == site.host
        }
        if not found:
            raise HHMarkupError(
                f"hh: в {index_url} нет ни одного файла vacancy*.xml — формат карты "
                "сайта изменился",
                url=index_url,
                status_code=200,
                body_bytes=len(body),
                source_slug=self.slug,
            )
        logger.debug(
            "sources.hh.sitemap_index",
            host=site.host,
            listed=len(listed),
            vacancy_files=len(found),
        )
        return sorted(found)

    async def _sitemap_entries(self, site: HHSite, url: str) -> list[SitemapEntry]:
        """Every dated vacancy URL in one sitemap file.

        A line that is not a vacancy page on this host, or whose date will not
        parse, is dropped with a count rather than failing the file: one
        malformed entry must not cost the other 1386. A file listing URLs of
        which none can be read is a different thing and says so; a file listing
        nothing at all is simply empty, which a small city legitimately is.
        """
        body = await self.http.get_text(url, cache_ttl=SITEMAP_CACHE_TTL)
        entries: list[SitemapEntry] = []
        dropped = 0
        for loc, lastmod in SITEMAP_ENTRY.findall(body):
            entry = _entry(site, loc, lastmod)
            if entry is None:
                dropped += 1
                continue
            entries.append(entry)
        if not entries:
            if SITEMAP_LOC.search(body):
                raise HHMarkupError(
                    f"hh: в {url} есть <loc>, но ни одной пары <loc>+<lastmod> — формат "
                    "карты сайта изменился",
                    url=url,
                    status_code=200,
                    body_bytes=len(body),
                    source_slug=self.slug,
                )
            logger.warning("sources.hh.sitemap_empty", file=url, body_bytes=len(body))
            return []
        if dropped:
            logger.warning("sources.hh.sitemap_lines_dropped", file=url, dropped=dropped)
        return entries

    async def _fetch(self, site: HHSite, entry: SitemapEntry) -> RawPosting | None:
        """One vacancy page, or None when there is honestly nothing to store.

        A posting taken down between the sitemap being written and us reading it
        answers 404, and the walk has to survive that: the sitemap is a snapshot
        and the site is not. An archived or disabled posting is skipped for a
        related reason — storing it would put a job nobody can apply to into the
        dashboard beside the ones they can.
        """
        try:
            body = await self.http.get_text(
                entry.url,
                # The sitemap's own timestamp: an edited posting is a cache miss
                # and an untouched one a hit. See ``ResponseCache.key``.
                cache_salt=entry.lastmod.isoformat(),
            )
        except HHChallengedError:
            # Re-raised before the clause below can look at it. There is no
            # status code to read — the transport refused the redirect into
            # hh's captcha before that hop was sent — so the ``response_status``
            # test would find nothing, conclude the posting is not gone, and
            # re-raise it anyway. Stated rather than left to that accident,
            # because adding a status to the challenge later would silently turn
            # a stopped crawl into a page counted as missing.
            raise
        except SourceError as exc:
            status_code = exc.extra.get("response_status")
            if status_code in GONE_STATUSES:
                logger.debug("sources.hh.vacancy_gone", url=entry.url, status=status_code)
                return None
            raise

        parsed = self._state(entry, body)
        if parsed is None:
            return None
        view, dictionary = parsed
        return self._posting(site, entry, view, dictionary)

    def _state(self, entry: SitemapEntry, body: str) -> tuple[HHVacancyView, dict[str, Any]] | None:
        """The posting and its field dictionary, out of the page's boot state.

        Every failure here is loud, because we are parsing a third party's
        internal state: when they move it this connector stops finding anything,
        and a source that quietly returns nothing looks exactly like a market
        with no jobs in it. The single quiet case is a posting that has genuinely
        gone — hh answers 404 with the marker still in place and an empty view,
        so an empty view on its own is not evidence of a rename.
        """
        match = STATE_MARKER.search(body)
        if match is None:
            raise HHMarkupError(
                f"hh: на {entry.url} нет разметки HH-Lux-InitialState — страница вакансии "
                "изменилась",
                url=entry.url,
                status_code=200,
                body_bytes=len(body),
                source_slug=self.slug,
            )
        try:
            state = _json_state(match.group(1))
        except ValueError as exc:
            raise HHMarkupError(
                f"hh: содержимое HH-Lux-InitialState на {entry.url} не разбирается как JSON",
                url=entry.url,
                status_code=200,
                body_bytes=len(body),
                source_slug=self.slug,
            ) from exc

        raw_view = state.get("vacancyView")
        if not raw_view:
            logger.info("sources.hh.empty_view", url=entry.url, error_code=state.get("errorCode"))
            return None
        try:
            view = HHVacancyView.model_validate(raw_view)
        except ValidationError as exc:
            # include_input=False is load-bearing, not tidiness: Pydantic puts the
            # whole validated object into every error entry, and this string is
            # committed to pipeline_run.errors and served by the API. With the
            # input left in, one renamed key ships the recruiter's contacts, the
            # employer's billing block and the manager's id — measured at 6.8 kB
            # per error on a real page — into a JSONB column and an HTTP
            # response. The location and the message are what a person needs.
            raise HHMarkupError(
                f"hh: vacancyView на {entry.url} не соответствует ожидаемой форме: "
                f"{exc.errors(include_input=False, include_url=False)[:3]}",
                url=entry.url,
                status_code=200,
                body_bytes=len(body),
                source_slug=self.slug,
            ) from exc
        if str(view.vacancy_id) != entry.external_id:
            # The shared client follows redirects, so the page that answered is
            # not necessarily the page that was asked for — hh moves a posting
            # to its successor often enough. Storing it under the id we asked
            # for would put one vacancy's text under another's key, and the
            # upsert would then keep overwriting it.
            logger.warning(
                "sources.hh.identity_mismatch",
                url=entry.url,
                asked=entry.external_id,
                answered=view.vacancy_id,
            )
            return None
        dictionary = state.get("vacancyFieldsDictionary")
        return view, dictionary if isinstance(dictionary, dict) else {}

    def _posting(
        self,
        site: HHSite,
        entry: SitemapEntry,
        view: HHVacancyView,
        dictionary: dict[str, Any],
    ) -> RawPosting | None:
        """A ``RawPosting``, or None for a posting that should not be stored."""
        status = view.status or HHStatus(active=True)
        if not status.is_live:
            logger.debug("sources.hh.not_live", url=entry.url, external_id=entry.external_id)
            return None
        full_title = " ".join(view.name.split())
        title = full_title[:MAX_TITLE]
        if not title:
            logger.warning("sources.hh.skipped", reason="title", external_id=entry.external_id)
            return None
        if len(full_title) > MAX_TITLE:
            # Every other thing this connector drops is counted. A cut title is
            # the one loss that reaches the dashboard, the fingerprint and the
            # embedding without appearing anywhere, and two long titles sharing
            # a prefix then hash to one vacancy.
            logger.warning(
                "sources.hh.truncated",
                field="title",
                external_id=entry.external_id,
                length=len(full_title),
                limit=MAX_TITLE,
            )
        display = view.company.display_name if view.company else None
        company = " ".join(display.split())[:MAX_COMPANY] if display else None
        if display and len(" ".join(display.split())) > MAX_COMPANY:
            logger.warning(
                "sources.hh.truncated",
                field="company",
                external_id=entry.external_id,
                limit=MAX_COMPANY,
            )

        return RawPosting(
            source_slug=self.slug,
            external_id=entry.external_id,
            url=entry.url[:MAX_URL],
            title=title,
            company=company,
            # The flattened text, not the markup. This reaches
            # ``vacancy.description_raw``, which is the column the embedding is
            # computed from, so storing HTML here would put tag names into every
            # vector. The markup survives in ``_derived.description_html``, so
            # the dashboard and a later HTML-to-markdown pass need no re-fetch.
            description=strip_html(view.description),
            raw={"_derived": self._derive(site, entry, view, dictionary).model_dump(mode="json")},
        )

    def _derive(
        self,
        site: HHSite,
        entry: SitemapEntry,
        view: HHVacancyView,
        dictionary: dict[str, Any],
    ) -> HHDerived:
        """Read the page into the block phase 4 normalises from."""
        formats = tuple(str(value) for value in unwrap(view.work_formats))
        city: str | None = None
        if isinstance(view.address, dict):
            raw_city = view.address.get("city")
            city = raw_city if isinstance(raw_city, str) and raw_city.strip() else None
        if not city and view.area is not None:
            city = view.area.name
        states = _calculated_states(view.vacancy_properties)
        company = view.company
        skills, languages = _split_skills(unwrap(view.key_skills))
        return HHDerived(
            external_id=entry.external_id,
            url=entry.url,
            city=(city or site.city).strip()[:120],
            country=(view.area.country_iso if view.area else None) or site.country,
            remote=_remote_from(formats),
            salary=_salary(view.compensation),
            published_at=view.published_at,
            expires_at=view.expires_at,
            key_skills=skills,
            language_requirements=languages,
            professional_role_ids=tuple(
                int(role) for role in unwrap(view.professional_role_ids) if _is_int(role)
            ),
            work_formats=formats,
            employment_form=view.employment_form,
            work_experience=view.work_experience,
            labels=_labels(view, formats, dictionary),
            closed_for_applicants=view.closed_for_applicants,
            accredited_it_employer=company.accredited_it if company else False,
            employer_on_additional_check=company.on_additional_check if company else False,
            employer_last_activity=_latest_activity(view.employer_manager),
            responses_count=view.responses_count,
            anonymous=bool(states.get("anonymous")),
            advertising=bool(states.get("advertising")),
            pay_for_performance=bool(states.get("payForPerformance")),
            description_html=view.description,
            sitemap_lastmod=entry.lastmod,
        )

    # ── the catalogue ─────────────────────────────────────────────────

    async def _catalog_ids(
        self,
        site: HHSite,
        *,
        keywords: Sequence[str],
        families: Sequence[RoleFamily],
        headline: str | None,
        spend: "Callable[[], None]",
        stop: "Callable[[], bool]",
    ) -> "_CatalogPass":
        """The vacancy ids this run's share of the catalogue names.

        A bounded number of pages, taken from where the last run left off and
        leaving the cursor past them, so the slug list is swept across runs
        instead of the same head of it being re-read every time.
        """
        if not keywords:
            # Nothing to select by, so nothing is bought to find that out. A plan
            # costs sixteen requests to resolve — hh's role directory and fifteen
            # catalogue sitemaps — and a run with no profile behind it is the
            # ordinary date-ordered walk, which is what it was before all this.
            logger.debug("sources.hh.catalog_skipped", host=site.host, reason="no keywords")
            return _CatalogPass()
        plan = await self._catalog_plan(
            site,
            keywords=keywords,
            families=families,
            headline=headline,
            spend=spend,
            stop=stop,
        )
        if plan is None or not plan.slugs:
            return _CatalogPass()

        wanted, offset = pages_this_run(plan)
        ids: set[str] = set()
        opened: list[str] = []
        for slug in wanted:
            if stop():
                break
            opened.append(slug)
            body = await self._catalog_page(site, slug)
            spend()
            if body is not None:
                ids.update(VACANCY_ID_ON_PAGE.findall(body))
        await self._save_plan(site, plan.model_copy(update={"offset": offset}))
        logger.info(
            "sources.hh.catalog_read",
            host=site.host,
            # The slugs themselves, not a count. Which eight of a hundred pages
            # a run opens decides what the whole run collects, and a number does
            # not say whether they were python pages or 1C ones.
            opened=opened,
            ids=len(ids),
            slugs=len(plan.slugs),
            next_offset=offset,
        )
        return _CatalogPass(
            ids=frozenset(ids),
            role_ids=frozenset(plan.role_ids),
            opened=tuple(opened),
            slugs=len(plan.slugs),
        )

    async def _catalog_page(self, site: HHSite, slug: str) -> str | None:
        """One catalogue page, or None when there is nothing behind that slug.

        A slug in the sitemap that answers 404 is ordinary — the sitemap is a
        snapshot, professions are retired — and must not stop a run. A check for
        robots must, and is logged with the page it arrived on for the same
        reason ``_fetch_counted`` logs it: that line is the only record of where
        a crawl was cut.
        """
        url = f"https://{site.host}/vacancies/{slug}"
        try:
            # No cache lifetime, like the sitemaps and for the same reason: this
            # document's whole job is to say what is on hh now, so a developer
            # with a warm disk cache would be handed a frozen id set and a run
            # that discovers nothing.
            return await self.http.get_text(url, cache_ttl=SITEMAP_CACHE_TTL)
        except HHChallengedError:
            logger.warning("sources.hh.challenged", host=site.host, url=url, stage="catalog")
            raise
        except SourceError as exc:
            if exc.extra.get("response_status") in GONE_STATUSES:
                logger.debug("sources.hh.catalog_gone", url=url)
                return None
            raise

    async def _catalog_plan(
        self,
        site: HHSite,
        *,
        keywords: Sequence[str],
        families: Sequence[RoleFamily],
        headline: str | None,
        spend: "Callable[[], None]",
        stop: "Callable[[], bool]",
    ) -> CatalogPlan | None:
        """The stored plan, or a fresh one when it is stale or asks for the wrong work.

        A plan that cannot be re-resolved is kept rather than dropped: crawling
        last week's professions is better than crawling none, and the reason it
        could not be refreshed is in the log beside it.
        """
        stored = await self.state_get(self._catalog_key(site))
        plan: CatalogPlan | None = None
        if stored:
            try:
                plan = CatalogPlan.model_validate(stored)
            except ValidationError as exc:
                logger.warning(
                    "sources.hh.catalog_plan_unreadable",
                    host=site.host,
                    errors=exc.errors(include_input=False, include_url=False)[:2],
                )
        wanted = tuple(family.key for family in families)
        fresh = plan is not None and datetime.now(UTC) - plan.resolved_at < CATALOG_TTL
        # The headline is compared as well as the families, and it has to be: it
        # is the strongest weight in the ranking, so a resume retitled from
        # "Python Developer" to "Data Engineer" reorders the whole plan while
        # leaving the set of families it matched untouched.
        if fresh and plan is not None and plan.families == wanted and plan.headline == headline:
            return plan
        if stop():
            return plan

        directory = await self._role_directory(spend)
        roles = roles_for(families, directory)
        slugs = await self._catalog_slugs(site, spend=spend, stop=stop)
        if not slugs:
            logger.warning(
                "sources.hh.catalog_not_resolved",
                host=site.host,
                detail="no catalogue slugs read; keeping whatever plan was stored",
            )
            return plan
        chosen = slugs_for(roles, keywords, slugs, families=families, headline=headline)
        if len(chosen) > MAX_CATALOG_SLUGS:
            logger.warning(
                "sources.hh.catalog_slugs_capped",
                host=site.host,
                matched=len(chosen),
                kept=MAX_CATALOG_SLUGS,
            )
            chosen = chosen[:MAX_CATALOG_SLUGS]
        refreshed = CatalogPlan(
            resolved_at=datetime.now(UTC),
            families=wanted,
            headline=headline,
            role_ids=tuple(role.id for role in roles),
            slugs=chosen,
            offset=0,
        )
        await self._save_plan(site, refreshed)
        logger.info(
            "sources.hh.catalog_resolved",
            host=site.host,
            families=list(wanted),
            # The head of the ranked list, which is what the next run opens.
            head=list(chosen[:CATALOG_HEAD_PAGES]),
            roles=len(roles),
            catalog_slugs=len(slugs),
            chosen=len(chosen),
        )
        return refreshed

    async def _role_directory(self, spend: "Callable[[], None]") -> tuple[DirectoryRole, ...]:
        """hh's professional roles, fetched at most once per run.

        A failure here is not fatal and not silent: without the directory the
        families name nothing, the plan falls back to the profile's own keywords
        against the slug list, and the crawl still runs. Cached even when empty,
        so a second city does not ask a host that has just refused.
        """
        if self._directory is not None:
            return self._directory
        # Spent before the request rather than after it, so that a refusal costs
        # the budget the same as an answer: it cost hh the same.
        spend()
        try:
            payload = await self.http.get_json(ROLES_URL, cache_ttl=CATALOG_TTL)
        except HHChallengedError:
            raise
        except (SourceError, OSError) as exc:
            logger.warning("sources.hh.roles_unavailable", url=ROLES_URL, error=str(exc))
            self._directory = ()
            return self._directory
        self._directory = read_directory(payload)
        logger.info("sources.hh.roles_read", roles=len(self._directory))
        return self._directory

    async def _catalog_slugs(
        self, site: HHSite, *, spend: "Callable[[], None]", stop: "Callable[[], bool]"
    ) -> tuple[str, ...]:
        """Every profession this host publishes a catalogue page for."""
        files = await self._catalog_sitemaps(site, spend)
        slugs: list[str] = []
        dated = 0
        for name, url in files:
            if stop():
                logger.info("sources.hh.catalog_file_not_read", host=site.host, file=name)
                continue
            body = await self.http.get_text(url, cache_ttl=SITEMAP_CACHE_TTL)
            spend()
            found, locs, lastmods = catalog_entries(body, site.host)
            dated += lastmods
            slugs.extend(found)
            logger.debug(
                "sources.hh.catalog_file", host=site.host, file=name, locs=locs, slugs=len(found)
            )
        if dated:
            # Measured at zero on 2026-09-08, and the walk is built on that: the
            # catalogue cannot be read by freshness, so its ids are ordered by
            # the vacancy sitemap's dates instead. hh adding dates here would be
            # a better way to do this, and somebody should be told.
            logger.info("sources.hh.catalog_has_lastmod", host=site.host, entries=dated)
        return tuple(dict.fromkeys(slugs))

    async def _catalog_sitemaps(
        self, site: HHSite, spend: "Callable[[], None]"
    ) -> list[tuple[str, str]]:
        """The ``vacancies{N}.xml`` files this host's index lists.

        Reads the index out of the run's memo when the vacancy files have
        already been taken from it, which is every path that reaches here today;
        the fetch is kept for the one that would not, and pays for itself.
        """
        body = self._index.get(site.host)
        if body is None:  # pragma: no cover - the walk always reads the index first
            body = await self.http.get_text(
                f"https://{site.host}{SITEMAP_INDEX_PATH}", cache_ttl=SITEMAP_CACHE_TTL
            )
            self._index[site.host] = body
            spend()
        found = catalog_sitemaps(body, site.host)
        if not found:
            logger.warning("sources.hh.no_catalog_sitemaps", host=site.host)
        return found

    def _catalog_key(self, site: HHSite) -> str:
        """Where one host's catalogue plan is stored."""
        return f"catalog:{site.host}"

    async def _save_plan(self, site: HHSite, plan: CatalogPlan) -> None:
        """Record the plan and its rotation cursor."""
        await self.state_set(self._catalog_key(site), plan.model_dump(mode="json"))

    def preview_search(
        self, queries: Sequence[SearchQuery], stored: Sequence[SavedState]
    ) -> SearchPreview:
        """Which catalogue pages would be opened first under this plan.

        The slug list is the one a previous run resolved and stored; it is
        re-ordered here with the ranking the crawl uses, under the plan's
        keywords and intent line, so the effect of new job titles shows before
        a request is made. When the stored list was resolved under a different
        line, the next run resolves it again and the set itself may change —
        the note says so rather than presenting the old set as final.
        """
        keywords = tuple(sorted({word for query in queries for word in query.keywords}))
        headline = next((query.headline for query in queries if query.headline), None)
        families = families_for(keywords, self.families)
        wanted = {self._catalog_key(site) for site in self.sites_for(queries)}
        slugs: dict[str, None] = {}
        stale = False
        resolved = False
        for row in stored:
            if row.key not in wanted:
                continue
            try:
                plan = CatalogPlan.model_validate(row.value)
            except ValidationError:
                logger.warning("sources.hh.state_unreadable", key=row.key)
                continue
            resolved = True
            stale = (
                stale
                or plan.headline != headline
                or set(plan.families) != {family.key for family in families}
            )
            slugs.update(dict.fromkeys(plan.slugs))
        ranked = rank_slugs(slugs, keywords, families=families, headline=headline)
        if not resolved:
            note = "Список профессий hh ещё не собирался: он появится после первого прогона."
        elif stale:
            note = (
                "Список собран под прежний поиск, порядок пересчитан под текущий. "
                "На следующем прогоне hh заново соберёт и сам список профессий (семейства: "
                f"{', '.join(family.key for family in families) or 'нет'})."
            )
        else:
            note = f"За прогон открывается {CATALOG_PAGES_PER_RUN} страниц каталога."
        return SearchPreview(
            use=SearchUse.CATALOG,
            terms=list(ranked[:PREVIEW_TERMS]),
            more=max(0, len(ranked) - PREVIEW_TERMS),
            note=note,
        )

    # ── position ──────────────────────────────────────────────────────

    def _hold(self, site: HHSite, name: str, entry: SitemapEntry, after: int) -> None:
        """Remember one finished entry until the pipeline confirms its posting.

        Bounded by :data:`MAX_HELD`. Dropping the oldest held entry costs a
        re-fetch on some later run and never a posting: an entry that is not
        recorded is simply still due. The list normally drains long before this,
        because the pipeline confirms every ``UPSERT_BATCH`` postings.
        """
        self._held.append(_Held(site=site, name=name, entry=entry, after=after))
        if len(self._held) > MAX_HELD:
            dropped = len(self._held) - MAX_HELD
            del self._held[:dropped]
            logger.warning(
                "sources.hh.held_overflow",
                dropped=dropped,
                detail="entries waiting on confirmation exceeded MAX_HELD; they stay due",
            )

    async def record_progress(self, durable: int) -> None:
        """Record every held entry whose posting the pipeline has now written.

        The one place this connector writes a position. Called by the pipeline
        after each batch — including the batch it rescues while a crawl stopped
        by a check for robots unwinds, which is the case the whole mechanism
        exists for. See ``BaseSource.record_progress`` for why the caller has to
        be the one to say.
        """
        confirmed = [held for held in self._held if held.after <= durable]
        if not confirmed:
            return
        self._held = [held for held in self._held if held.after > durable]

        by_file: dict[tuple[str, str], tuple[HHSite, str, list[EntryKey]]] = {}
        for held in confirmed:
            key = (held.site.host, held.name)
            site, name, keys = by_file.setdefault(key, (held.site, held.name, []))
            keys.append(_key(held.entry))
        for key, (site, name, keys) in by_file.items():
            order = self._order.get(key, [])
            mark = await self._watermark(site, name)
            await self._save_watermark(site, name, mark.covering(order, keys))

    def _state_key(self, site: HHSite, name: str) -> str:
        """Where one sitemap file's position is stored. Per file, never global."""
        return f"{POSITION_PREFIX}{site.host}:{name}"

    def _census_key(self, site: HHSite, name: str) -> str:
        """Where one sitemap file's measured size is stored. Per file as well."""
        return f"{CENSUS_PREFIX}{site.host}:{name}"

    async def _save_census(self, site: HHSite, name: str, census: FileCensus) -> None:
        """Record how big the file was and how much of it was still due.

        The one write in this connector that is allowed to fail quietly, and the
        asymmetry with :meth:`_save_watermark` is deliberate. A position is
        correctness: losing one means a future run re-buys pages or, worse,
        skips them, so a store that refuses it must stop the run and say so. A
        census is a number on a screen. Stopping a crawl that can reach hh
        because a counter could not be saved would trade the thing the run is
        for against the thing that describes it.

        Not silent — it is logged with the key and the reason, which is the
        distinction CLAUDE.md draws against ``except Exception: pass``.
        """
        try:
            await self.state_set(self._census_key(site, name), census.model_dump(mode="json"))
        except Exception as exc:
            logger.warning(
                "sources.hh.census_not_recorded",
                host=site.host,
                file=name,
                error=type(exc).__name__,
                detail=str(exc),
            )

    def describe_position(self, stored: Sequence[SavedState]) -> list[CrawlPosition]:
        """Where this crawl has got to, per host and per sitemap file.

        Reads back exactly what :meth:`_save_watermark` and
        :meth:`_save_census` wrote, and nothing else in the project knows how
        to. Rows whose key belongs to neither scheme are ignored rather than
        guessed at: a key written by an older version of this connector is not
        a position, and inventing a reading for it would put a number on the
        screen that nothing produced.

        A stored value that no longer parses is skipped with a warning, the
        same way the crawl treats one — the row is somebody's old shape, and a
        dashboard is the wrong place to fail over it.
        """
        cities = self._city_names()
        positions: dict[tuple[str, str], FileWatermark] = {}
        censuses: dict[tuple[str, str], FileCensus] = {}
        dates: dict[tuple[str, str], datetime] = {}

        for row in stored:
            parsed = _split_file_key(row.key)
            if parsed is None:
                continue
            prefix, place = parsed
            try:
                if prefix == POSITION_PREFIX:
                    positions[place] = FileWatermark.model_validate(row.value)
                else:
                    censuses[place] = FileCensus.model_validate(row.value)
            except ValidationError as exc:
                logger.warning(
                    "sources.hh.state_unreadable",
                    key=row.key,
                    errors=exc.errors(include_input=False, include_url=False)[:2],
                )
                continue
            # The freshest of the two rows: the census is written when a file is
            # read, the position when work on it is confirmed, and a person
            # asking "when did this last move" means either.
            seen = dates.get(place)
            dates[place] = row.updated_at if seen is None else max(seen, row.updated_at)

        described = [
            _describe_file(
                place,
                city=cities.get(place[0]),
                mark=positions.get(place),
                census=censuses.get(place),
                updated_at=dates.get(place),
            )
            for place in sorted(positions.keys() | censuses.keys())
        ]
        return described

    def _city_names(self) -> dict[str, str]:
        """Host to city, for the screen. Empty when the site list will not load.

        Swallowed deliberately and narrowly: this is a label. ``GET /sources``
        already reports a broken ``hh_sites.yaml`` as the source being
        unavailable, with the parse error in it, so failing here as well would
        replace a whole overview screen with the same message it is already
        showing one panel down.
        """
        try:
            return {site.host: site.city for site in self.sites}
        except SourceError as exc:
            logger.warning("sources.hh.sites_unreadable", detail=exc.detail)
            return {}

    async def _watermark(self, site: HHSite, name: str) -> FileWatermark:
        """How far the last run got through this file.

        A stored value that no longer parses is treated as no value at all. The
        cost is one re-crawl of that file, which is idempotent; the alternative
        is a source that cannot start until somebody deletes a row by hand.
        """
        stored = await self.state_get(self._state_key(site, name))
        if not stored:
            return FileWatermark()
        try:
            return FileWatermark.model_validate(stored)
        except ValidationError as exc:
            logger.warning(
                "sources.hh.position_unreadable",
                host=site.host,
                file=name,
                errors=exc.errors(include_input=False, include_url=False)[:2],
            )
            return FileWatermark()

    async def _save_watermark(self, site: HHSite, name: str, mark: FileWatermark) -> None:
        """Record the position, if there is one to record.

        A mark with no ``lastmod`` names nothing, and writing it would store a
        row that says "we have covered up to nowhere" — which reads, on the next
        run, exactly like the absent row it replaced. So it is not written; but
        it IS logged, because the state it describes is a real one that hid for
        a month. A walk can read a thousand entries and still hold an empty mark
        when the lag has not been cleared, and the only way to see that from
        outside was an empty ``source_state`` table nobody was looking at.
        """
        if not mark.covered:
            logger.debug("sources.hh.position_not_advanced", host=site.host, file=name)
            return
        await self.state_set(self._state_key(site, name), mark.model_dump(mode="json"))
        logger.debug(
            "sources.hh.position_saved",
            host=site.host,
            file=name,
            stretches=len(mark.covered),
            newest=mark.covered[0].high_lastmod.isoformat(),
            oldest=mark.covered[-1].low_lastmod.isoformat(),
        )

    async def _record_while_unwinding(self, site: HHSite, name: str, mark: FileWatermark) -> None:
        """Save the position as an exception passes through, or say it could not.

        The only failure swallowed here is the position write itself, and it is
        swallowed because letting it out would replace the exception that
        actually stopped the run. Those two read completely differently to
        whoever gets the report: a run stopped by a check for robots is
        rescheduled, a run stopped by a broken connector sends somebody to read
        this file. ``pipeline/runner.py`` makes the same trade one layer up when
        its rescue write fails, and for the same reason. Losing the position
        costs one file's unsaved stretch, bought again next run; losing the
        reason costs a person an afternoon.
        """
        try:
            await self._save_watermark(site, name, mark)
        except Exception:
            logger.exception("sources.hh.position_not_recorded", host=site.host, file=name)


def _split_file_key(key: str) -> tuple[str, tuple[str, str]] | None:
    """``sitemap:almaty.hh.kz:vacancy0`` -> the prefix and (host, file).

    Neither a host nor a sitemap file name contains a colon, so the three parts
    separate cleanly. Anything else — a key from an older scheme, a key from
    another connector that shares this row's slug — returns None and is left
    alone.
    """
    for prefix in (POSITION_PREFIX, CENSUS_PREFIX):
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix) :]
        host, separator, name = rest.partition(":")
        if separator and host and name:
            return prefix, (host, name)
    return None


def _describe_file(
    place: tuple[str, str],
    *,
    city: str | None,
    mark: FileWatermark | None,
    census: FileCensus | None,
    updated_at: datetime | None,
) -> CrawlPosition:
    """One sitemap file's line on the overview screen.

    The counts come from the census and only from it. Deriving "covered" as
    ``total - outstanding`` would be the same number said twice; deriving it
    from the stretches is not possible at all, because a stretch is an interval
    over ``(lastmod, id)`` and counts nothing. So what is not measured stays
    ``None`` and the screen says so.
    """
    host, name = place
    covered = mark.covered if mark is not None else ()
    return CrawlPosition(
        scope=host,
        label=name,
        title=city,
        total=census.total if census is not None else None,
        outstanding=census.outstanding if census is not None else None,
        stretches=len(covered),
        newest=covered[0].high_lastmod if covered else None,
        oldest=covered[-1].low_lastmod if covered else None,
        updated_at=updated_at,
    )


def _is_int(value: Any) -> bool:
    """Whether a professional-role id is one. hh has sent these as strings."""
    return isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit())


def _json_state(escaped: str) -> dict[str, Any]:
    """The template's contents as JSON. Raises ``ValueError`` when it is not.

    ``Any`` in the value type for the reason CLAUDE.md asks for: this is hh's
    whole boot state, dozens of unrelated keys, and the two we read are
    validated by their own models one call later.
    """
    decoded = json.loads(html_lib.unescape(escaped))
    if not isinstance(decoded, dict):
        raise ValueError("HH-Lux-InitialState is not a JSON object")
    return decoded


def _entry(site: HHSite, loc: str, lastmod: str) -> SitemapEntry | None:
    """One sitemap line as a typed entry, or None when it is not one of ours.

    The host is checked rather than assumed, and a URL carrying a query string
    is refused here as well as in the transport: a sitemap is a document written
    by somebody else, and the URLs in it are input.
    """
    parts = urlsplit(loc)
    if parts.hostname != site.host or parts.query:
        return None
    match = VACANCY_PATH.match(parts.path)
    if match is None:
        return None
    external_id = match.group(1)
    if len(external_id) > MAX_EXTERNAL_ID:
        return None
    try:
        when = datetime.fromisoformat(lastmod)
    except ValueError:
        return None
    return SitemapEntry(
        external_id=external_id,
        # Rebuilt rather than taken verbatim, so nothing a sitemap says can put
        # a query string or a different host into a URL we then fetch.
        url=f"https://{site.host}/vacancy/{external_id}",
        lastmod=when if when.tzinfo else when.replace(tzinfo=UTC),
    )


def _salary(compensation: dict[str, Any] | None) -> HHSalary | None:
    """The salary, or None when the posting states it has none.

    ``{"noCompensation": {}}`` is the shape that catches people: a non-empty
    dict, so it passes every truthiness test written against it. This is not an
    edge case — five of six postings in the brief's sample carried no salary —
    and a hard salary filter over this corpus would remove most of it, which is
    a matching decision rather than a parsing one.
    """
    if not compensation or "noCompensation" in compensation:
        return None
    try:
        parsed = HHCompensation.model_validate(compensation)
    except ValidationError as exc:
        logger.warning(
            "sources.hh.compensation_unparsed",
            errors=exc.errors(include_input=False, include_url=False)[:2],
        )
        return None
    if parsed.amount_from is None and parsed.amount_to is None:
        return None
    return HHSalary(
        min=parsed.amount_from,
        max=parsed.amount_to,
        currency=parsed.currency_code,
        is_gross=parsed.gross,
        period=MODE_TO_PERIOD.get(parsed.mode or ""),
        mode=parsed.mode,
        frequency=parsed.frequency,
    )


def _split_skills(entries: Sequence[Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """hh's requirement list, split into skills and the languages mixed into it.

    ``Any`` on the way in because ``unwrap`` returns whatever the field held;
    everything is stringified here, which is what the payload has always
    contained.
    """
    skills: list[str] = []
    languages: list[str] = []
    for entry in entries:
        value = str(entry)
        (languages if LANGUAGE_SKILL.match(value) else skills).append(value)
    return tuple(skills), tuple(languages)


def _remote_from(formats: Sequence[str]) -> RemoteType:
    """Remoteness from ``workFormats``, taking the most remote form offered."""
    ranked = {FORMAT_TO_REMOTE.get(value, RemoteType.NO) for value in formats}
    if RemoteType.FULL in ranked:
        return RemoteType.FULL
    if RemoteType.HYBRID in ranked:
        return RemoteType.HYBRID
    return RemoteType.NO


def _labels(
    view: HHVacancyView, formats: Sequence[str], dictionary: dict[str, Any]
) -> dict[str, str]:
    """Human-readable renderings of the coded fields, from the page's own tables.

    Nothing here is hardcoded, deliberately: hh ships the vocabulary with every
    page — ``{"id": "SELF_EMPLOYED", "text": "с самозанятым"}`` — so a value they
    add tomorrow renders tomorrow, instead of after somebody notices a blank in
    the dashboard. ``workExperience`` is the exception hh makes itself: it has no
    entry in the dictionary and its rendering arrives in ``translations``.
    """
    labels: dict[str, str] = {}
    if view.employment_form:
        text = _dictionary_text(dictionary, "employmentForm", view.employment_form)
        if text:
            labels["employmentForm"] = text
    rendered = [
        text
        for value in formats
        if (text := _dictionary_text(dictionary, "workFormats", value)) is not None
    ]
    if rendered:
        labels["workFormats"] = ", ".join(rendered)
    experience = (view.translations or {}).get("workExperience")
    if isinstance(experience, str) and experience.strip():
        labels["workExperience"] = experience.strip()
    return labels


def _dictionary_text(dictionary: dict[str, Any], field: str, value: str) -> str | None:
    """The page's own rendering of one coded value, when it carries one."""
    entries = dictionary.get(field)
    for item in entries if isinstance(entries, list) else []:
        if isinstance(item, dict) and item.get("id") == value:
            text = item.get("text")
            return str(text) if text else None
    return None


def _latest_activity(manager: dict[str, Any] | None) -> datetime | None:
    """When the employer was last active, out of the block that also names them.

    One key is read and the rest of ``employerManager`` is dropped where it
    stands. The block carries the recruiter's name and sometimes their
    photograph, and this crawler is anonymous, read-only and has no business
    keeping either — the brief for the feature that uses this says in as many
    words that no employee of a company is to be looked up anywhere, and the
    smallest form of that rule is not storing the ones hh hands over unasked.

    What is kept is a timestamp the employer published about themselves, which
    answers a question worth asking before writing a letter: is anyone reading
    this inbox. Parsed defensively — the value is whatever hh's page had in it
    on the day it was crawled, and a string that is not a date is no date.
    """
    if not isinstance(manager, dict):
        return None
    raw = manager.get("latestActivity")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _calculated_states(properties: dict[str, Any] | None) -> dict[str, Any]:
    """The derived publication flags, without the billing they sit beside.

    ``vacancyProperties.properties`` is the employer's invoice — package names,
    service ids, paid-placement windows — and it is on the page whether anyone
    wants it there or not. None of it describes the job, so none of it is
    stored. A live sample carried ``HH_AUTO_RENEWAL`` with
    ``intervalMinutes = 4320``: a fact about hh's billing, and the reason nothing
    downstream may read this source's timestamps as freshness.
    """
    states = (properties or {}).get("calculatedStates")
    if not isinstance(states, dict):
        return {}
    hh_states = states.get("HH")
    return hh_states if isinstance(hh_states, dict) else {}
