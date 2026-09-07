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

Corrected 2026-09-07: this paragraph used to say that a challenged run leaves
the position where it was, which was true and useless, because where it was
was nowhere. See ``WATERMARK_LAG`` for what the run recorded instead, which
was nothing at all, and for how many rows that cost.

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
a city. There is no title in the sitemap, so keywords cannot be pushed upstream
and cannot be judged before the page is fetched: this source walks a corpus
instead of running a search, which is why it overrides ``search_batch``, keeps
its own page budget, and remembers per sitemap file how far it got. A first
crawl takes several runs. Each of them logs what it did not reach.

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
from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
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
from app.sources.base import AccessMode, BaseSource, RateLimit, RawPosting, SearchQuery
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
#: (SEO landing pages by profession, 5716 entries with no lastmod) is a
#: different file and deliberately not used yet; ``employers`` is companies; and
#: ``resumes{N}`` is living people's resumes, which is why the selection here is
#: an allow-list matched on the whole name rather than a substring test.
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

#: How far the recorded position lags behind what has been yielded, in postings.
#:
#: A posting is handed to the pipeline long before the pipeline writes it: the
#: runner accumulates a batch and commits it in one go. A mark that named the
#: posting just yielded would therefore, after a crash, declare written what was
#: only ever in memory — and those postings are then never fetched again,
#: because the mark says they are done.
#:
#: The safe value is derivable, so here is the derivation rather than a number
#: to take on trust. Write ``B`` for the runner's batch size (``UPSERT_BATCH``,
#: 100). The runner appends every posting it is handed and commits the moment
#: the batch reaches ``B``, so once ``S`` postings have been handed over it has
#: committed the first ``floor(S / B) * B`` of them and holds at most ``B - 1``.
#: The walk advances the mark over an entry when ``stored - before > LAG``,
#: which is to say when at least ``LAG`` further postings have been handed over
#: after that entry's own. Call that posting's position in the run ``g``, so
#: ``S - g >= LAG``. It is committed when ``floor(S / B) * B >= g``, and since
#: ``floor(S / B) * B > S - B`` it is enough that ``S - B >= g - 1``, which
#: follows from ``S - g >= LAG`` whenever ``LAG >= B - 1``. So ``B - 1`` is the
#: smallest lag that cannot name an unwritten posting, and anything above it is
#: margin. ``stored`` counts one site while the runner's batch counts the whole
#: run, which only ever helps: sites are walked one after another, so postings
#: handed over after an entry within its site are a subset of those handed over
#: after it in the run, and the test the walk applies is therefore the stricter
#: of the two.
#:
#: Corrected 2026-09-07, and the correction is the point. The value was 200 —
#: two batches — on the reasoning that a connector must not know how the
#: pipeline batches, only that this exceeds it. What that margin cost was then
#: measured. A run advances the mark ``max(0, stored - LAG)`` times, so at 200 a
#: run storing 10, 100, 172 or exactly 200 postings advanced it zero times, left
#: ``lastmod`` unset, and recorded nothing at any exit. The live run of
#: 2026-09-06 stored 172 before hh's captcha stopped it. That is why
#: ``source_state`` was empty rather than stale, why every run re-read the file
#: from the top, and why the corpus stood at 466 rows against some 13 557 for
#: the city. The margin was not free; it was the whole cost.
#:
#: What guards this against ``UPSERT_BATCH`` growing is not slack — slack fails
#: by silently recording nothing, which is exactly what happened —
#: but ``test_the_lag_is_derived_from_the_pipelines_unwritten_window``, which reads
#: both numbers and fails the build when they cross. The number stays stated
#: here rather than imported from ``pipeline/``, so that a connector still does
#: not depend on how the pipeline batches; only its test does.
WATERMARK_LAG = 100

#: Entries the position may advance over between two writes of it. Every entry
#: would be a database round trip per page; never would mean a run killed near
#: its budget re-walked everything next time.
WATERMARK_SAVE_EVERY = 50

#: The sitemaps are never cached. ``cache_ttl`` is thirty days because a
#: vacancy page carries its own ``lastmod`` as a cache salt, so an edited
#: posting misses and an untouched one hits. The sitemaps have no such salt —
#: their whole job is to tell us what changed — so inheriting that TTL would
#: pin the index and every file on disk for a month, and the connector would
#: discover nothing hh published after its first run. Measured: with
#: ``HTTP_CACHE_DIR`` set, run two of a two-vacancy fixture yields nothing at
#: all when the sitemaps are cached.
SITEMAP_CACHE_TTL = timedelta(0)

#: Freshest entries fetched before the resumable ascending walk begins.
#:
#: A city's sitemap spans about a month. A purely ascending crawl therefore
#: spends its first several runs on postings three weeks old — a good share of
#: them already past ``validThroughTime`` — while the vacancy published this
#: morning waits a fortnight. For a job search that is exactly the wrong end of
#: the file, so a bounded slice of the newest entries is bought first. It cannot
#: be more than a small part of the budget, because it advances no position and
#: is therefore re-bought on the next run.
HEAD_SLICE = 50

#: Entries sharing one ``lastmod`` second are remembered by id so that resuming
#: neither repeats them nor skips them. Bounded so the stored row cannot grow
#: without limit; past it the tie is resolved by repeating, which costs requests
#: and never costs a posting.
MAX_TIED_IDS = 1000

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


class FileWatermark(BaseModel):
    """How far a previous run got through one sitemap file.

    The ids are the entries sharing the exact second of :attr:`lastmod`. Without
    them resuming has to choose between repeating that second's work every run
    or skipping whatever tied with it, and skipping loses postings — the same
    asymmetry ``app/normalize/fingerprint.py`` argues for elsewhere: repeating
    costs requests, losing costs data.
    """

    model_config = ConfigDict(frozen=True)

    lastmod: AwareDatetime | None = None
    ids_at_lastmod: tuple[str, ...] = ()

    def is_done(self, entry: SitemapEntry) -> bool:
        """Whether a previous run already covered this entry."""
        if self.lastmod is None:
            return False
        if entry.lastmod < self.lastmod:
            return True
        return entry.lastmod == self.lastmod and entry.external_id in self.ids_at_lastmod

    def advanced(self, entry: SitemapEntry) -> "FileWatermark":
        """This mark, moved to include ``entry``. Never moves backwards."""
        if self.lastmod is not None and entry.lastmod < self.lastmod:
            return self
        if self.lastmod == entry.lastmod:
            tied = (*self.ids_at_lastmod, entry.external_id)[-MAX_TIED_IDS:]
            return FileWatermark(lastmod=self.lastmod, ids_at_lastmod=tied)
        return FileWatermark(lastmod=entry.lastmod, ids_at_lastmod=(entry.external_id,))


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
    #: Ids bought by the head pass, so the ascending pass walks past them
    #: without paying again.
    fetched_head: set[str] = field(default_factory=set)
    fetched: int = 0
    stored: int = 0
    #: Pages that were bought and produced nothing storable: taken down since
    #: the sitemap was written, archived, or answering for a different vacancy.
    #: One counter rather than three, because the log is read to answer "how
    #: much of what we paid for was worth keeping" and the reasons are already
    #: in the debug lines beside it.
    not_stored: int = 0
    unreadable: int = 0


@dataclass(slots=True)
class _Tail:
    """The end of a drained sitemap file, held back until the walk is over.

    The lag keeps the recorded position behind what has been handed to the
    pipeline. When a file runs out there is nothing left to keep the lag
    honest, so its remainder waits here until every posting of the run has been
    yielded — at which point the runner writes its last batch immediately.
    """

    site: HHSite
    name: str
    mark: FileWatermark
    entries: list[SitemapEntry]


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
    rate_limit: ClassVar[RateLimit] = RateLimit(requests_per_second=1.0, burst=2)
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

    @property
    def sites(self) -> tuple[HHSite, ...]:
        """The configured sites, read from the file once per instance."""
        if self._sites is None:
            self._sites = load_sites()
        return self._sites

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

        **The plan's keywords are not applied, and that is deliberate.** They
        cannot be applied before a page is fetched, because the sitemap carries
        no title — so a filter here saves no request at all, it only decides
        what to throw away after paying for it. And throwing it away is not
        free: the walk records how far it got, so a posting dropped for today's
        keywords is marked as dealt with and is never fetched again by any
        future run. Upload a new CV with new skills and every posting the old
        keyword set rejected stays invisible forever. Relevance belongs to
        ``matching/``, which scores what is stored; this connector's job is to
        store what hh published. The union is logged so that nobody reading a
        run report believes hh honoured it.
        """
        keywords = sorted({word for query in queries for word in query.keywords})
        sites = self.sites_for(queries)
        if not sites:
            logger.warning("sources.hh.no_sites_configured", queries=len(queries))
            return
        if keywords:
            logger.info("sources.hh.keywords_ignored", keywords=keywords, sites=len(sites))

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
        tails: list[_Tail] = []
        for site in sites:
            async for posting in self._crawl_site(site, budget, allowance=share, tails=tails):
                yield posting
        # Everything has been handed over, so the end of each drained file — the
        # part the lag was holding back — can be recorded. This is the last
        # thing the walk does, because the runner writes its final batch as soon
        # as this generator finishes: the window in which a recorded entry is
        # still unwritten is that hand-off and nothing more. A run that dies
        # earlier records none of these and re-fetches them next time.
        #
        # This is the one place the walk records an entry whose posting the
        # pipeline may still be holding, and it is worth being explicit that the
        # exception is bought rather than free. A drained file's remainder has
        # nowhere else to go: hold it back and every future run buys those pages
        # again, forever. A file cut short by the budget or by a challenge has
        # somewhere else to go — the next run, which starts there — so those two
        # exits record the lagged mark and nothing more.
        for tail in tails:
            mark = tail.mark
            for entry in tail.entries:
                mark = mark.advanced(entry)
            await self._save_watermark(tail.site, tail.name, mark)

    async def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]:
        """One query's worth of the same walk.

        Present because ``BaseSource`` requires it and a caller may hold this
        connector directly. It delegates, so there is one crawl loop to reason
        about rather than two.
        """
        async for posting in self.search_batch([query]):
            yield posting

    async def _crawl_site(
        self, site: HHSite, budget: CrawlBudget, *, allowance: int, tails: list["_Tail"]
    ) -> AsyncIterator[RawPosting]:
        """Walk one host: the index, then every sitemap's outstanding entries.

        Two passes over the same due set, for two incompatible requirements.

        *Newest first, briefly.* A city's sitemap spans about a month, so a
        purely ascending walk spends the first several runs on postings three
        weeks old — many already past ``validThroughTime`` — while today's
        vacancies wait a fortnight. For a job search that is the wrong end.
        So a bounded head slice of the freshest entries is fetched first.

        *Then oldest first, for the rest.* Only an ascending walk is resumable:
        the recorded position can then say "everything up to here is done", and
        a run that stops early leaves an unbroken remainder. The head slice
        deliberately does **not** advance that position — advancing it to the
        newest entry would declare everything below it done — so the ascending
        pass walks past those ids without buying them twice.
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
            due[name] = sorted(
                (entry for entry in entries if not marks[name].is_done(entry)),
                key=lambda entry: (entry.lastmod, entry.external_id),
            )
        outstanding = sum(len(entries) for entries in due.values())

        state = _SiteRun(site=site)
        head = sorted(
            (entry for entries in due.values() for entry in entries),
            key=lambda entry: entry.lastmod,
            reverse=True,
        )[:HEAD_SLICE]
        for entry in head:
            if stop():
                break
            posting = await self._fetch_counted(entry, state)
            spend()
            state.fetched_head.add(entry.external_id)
            if posting is not None:
                yield posting

        for name, entries in due.items():
            if stop():
                logger.info(
                    "sources.hh.file_not_reached",
                    host=site.host,
                    file=name,
                    outstanding=len(entries),
                )
                continue
            mark = marks[name]
            # The position lags the yields, by ``WATERMARK_LAG`` postings; the
            # derivation of that number, and what happened when it was twice as
            # large as it needed to be, are written out where it is declared.
            #
            # Each entry is remembered with the number of postings yielded
            # before it. The lag has to be measured in POSTINGS, not in entries:
            # a stretch of entries that yield nothing — taken down, archived,
            # answering for another vacancy — would otherwise push the mark
            # forward while the postings before them were still unwritten, and a
            # corpus this size has such stretches.
            pending: deque[tuple[SitemapEntry, int]] = deque()
            unsaved = 0
            try:
                for index, entry in enumerate(entries):
                    if stop():
                        logger.info(
                            "sources.hh.budget_reached",
                            host=site.host,
                            file=name,
                            # What a run could not reach has to be visible, or a
                            # truncated crawl reads as a completed one.
                            remaining_in_file=len(entries) - index,
                            outstanding_on_site=outstanding,
                        )
                        # Everything the lag still holds back is left for the
                        # next run rather than recorded here, and that is the
                        # difference between this exit and a drained file. A
                        # drained file's remainder is held back forever if it is
                        # not recorded, so ``search_batch`` accepts a hand-off
                        # window to record it; a truncated file's remainder is
                        # simply the next run's first entries, bought once more
                        # and then walked past. A bounded re-buy is not worth a
                        # window in which the mark names a posting the pipeline
                        # has not written.
                        await self._save_watermark(site, name, mark)
                        self._log_site(site, state, budget, outstanding)
                        return
                    before = state.stored
                    if entry.external_id in state.fetched_head:
                        # Already bought in the head pass. Walk past it so the
                        # mark can advance; do not pay for it twice.
                        pending.append((entry, before))
                    else:
                        posting = await self._fetch_counted(entry, state)
                        spend()
                        pending.append((entry, before))
                        if posting is not None:
                            yield posting
                    while pending and state.stored - pending[0][1] > WATERMARK_LAG:
                        mark = mark.advanced(pending.popleft()[0])
                        unsaved += 1
                    if unsaved >= WATERMARK_SAVE_EVERY:
                        await self._save_watermark(site, name, mark)
                        unsaved = 0
            except Exception:
                # The run ends here: hh answered with a check for robots, or it
                # moved the markup, or the transport gave up on a page. The mark
                # is safe to write at this instant for exactly the reason it is
                # safe at any other — it names only entries whose postings are a
                # full batch behind what has been handed over, and how the run
                # ends changes nothing about that. Recording it costs one row
                # and saves the up-to-``WATERMARK_SAVE_EVERY``-minus-one
                # advances made since the last periodic write, on a source whose
                # every run so far has ended in precisely this clause. The
                # exception is re-raised untouched; nothing here classifies it.
                await self._record_while_unwinding(site, name, mark)
                raise
            # The file is drained. Its remainder is not recorded here: the
            # postings from it are still in the pipeline's unwritten batch, and
            # a mark naming them would declare written what is only in memory.
            # It waits until the whole walk is done — see ``search_batch``.
            await self._save_watermark(site, name, mark)
            if pending:
                tails.append(_Tail(site, name, mark, [entry for entry, _ in pending]))

        self._log_site(site, state, budget, outstanding)

    def _log_site(
        self, site: HHSite, state: "_SiteRun", budget: CrawlBudget, outstanding: int
    ) -> None:
        """One line per site, carrying what was covered and what was not."""
        logger.info(
            "sources.hh.site_finished",
            host=site.host,
            outstanding=outstanding,
            fetched=state.fetched,
            head=len(state.fetched_head),
            stored=state.stored,
            not_stored=state.not_stored,
            unreadable=state.unreadable,
            budget_left=max(0, budget.remaining),
        )

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
            anonymous=bool(states.get("anonymous")),
            advertising=bool(states.get("advertising")),
            pay_for_performance=bool(states.get("payForPerformance")),
            description_html=view.description,
            sitemap_lastmod=entry.lastmod,
        )

    # ── position ──────────────────────────────────────────────────────

    def _state_key(self, site: HHSite, name: str) -> str:
        """Where one sitemap file's position is stored. Per file, never global."""
        return f"sitemap:{site.host}:{name}"

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
        if mark.lastmod is None:
            logger.debug("sources.hh.position_not_advanced", host=site.host, file=name)
            return
        await self.state_set(self._state_key(site, name), mark.model_dump(mode="json"))
        logger.debug(
            "sources.hh.position_saved",
            host=site.host,
            file=name,
            through=mark.lastmod.isoformat(),
            tied_ids=len(mark.ids_at_lastmod),
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
