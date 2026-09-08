"""Stage 0 of reading hh by profession: measure the catalogue, never guess it.

**Why this file exists.** The crawl walks ``vacancy{N}.xml`` newest-first and is
blind to the profession, because a sitemap entry carries a ``<loc>`` and a
``<lastmod>`` and nothing else. Measured on 643 scored vacancies on 2026-09-08:
of 294 hh rows collected, none belonged to the programmer/developer/devops
family, the top role was sales at 34, and the whole corpus produced one match
above "miss". Scoring is honest and there is nothing in the corpus to score.

Filtering by role at walk time is impossible in the obvious way:
``professionalRoleIds`` lives on the vacancy page, i.e. after the request has
already been spent, so discarding a posting afterwards saves database rows and
not one single request. The share of relevant postings per request — the number
that actually decides whether this source is worth its rate limit — is unchanged
by any amount of filtering downstream.

There is a candidate for a real answer, and it is a *candidate*, not a plan.
Beside ``vacancy{N}.xml`` the sitemap index lists ``vacancies{N}.xml``, which is
a different family: measured on 2026-09-06, ``vacancies0.xml`` for Almaty was
858 KB and held 5716 URLs shaped like ``https://almaty.hh.kz/vacancies/
crm-marketolog`` — no query string, no ``lastmod``. They look like catalogue
pages per profession. If such a page lists vacancy ids, then the walk can start
from a profession instead of from a date, with no query string anywhere and
every URL inside what robots.txt allows. If it does not, the honest fallback is
the walk as it is plus an early discard, which must be described as what it is.

**Which of those is true is a measurement nobody has taken**, and this module is
the instrument for taking it. It answers three questions and reports what it
saw, not what it expected:

1. Which ``vacancies{N}.xml`` files the index lists, how many slugs they hold,
   whether any entry carries a ``lastmod``, and which slugs name something in
   the development family.
2. What one catalogue page actually contains: whether ``HH-Lux-InitialState`` is
   on it, what the top level of that state holds, how many distinct
   ``/vacancy/{id}`` ids appear in the document, and which links to further
   catalogue pages exist — split by whether they carry a query string, because
   the ones that do are closed to us and the ones that do not are the pagination
   this whole idea depends on.
3. What ``api.hh.ru/professional_roles`` — open, unlike the jobseeker half of
   that host — calls the roles in the same family, with their ids, so that the
   ``roles=96`` seen in hh's own ad telemetry can be checked against the
   directory rather than believed.

**What this module is not.** It is not a connector: it registers nothing and the
pipeline never holds it. It does not decide anything — the terms in
:data:`DEV_TERMS` are the search terms of one measurement, not this project's
answer to "which roles does this profile want", which is a config file that
belongs next to ``hh_sites.yaml`` and can only be written once the vocabulary it
must hold is known. And it changes no rate: every request goes through the hh
connector's own :class:`~app.sources.http.SourceHTTP`, so robots.txt, the ban on
query strings, the challenge detection and the measured 0.25 requests a second
apply here exactly as they apply to a crawl. A probe that went faster to answer
sooner would answer with a captcha.

One question it deliberately cannot answer in one pass: whether a catalogue
page's composition changes over time. That needs two runs and a diff, which is
what ``--json`` is for — dump it, run it again tomorrow, compare.
"""

import html as html_lib
import json
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.core.exceptions import SourceError
from app.core.logging import get_logger
from app.sources.hh import (
    SITEMAP_CACHE_TTL,
    SITEMAP_INDEX_PATH,
    SITEMAP_LOC,
    STATE_MARKER,
    HHSite,
)
from app.sources.http import HHChallengedError, SourceHTTP

logger = get_logger(__name__)

#: The catalogue family. Three letters away from ``vacancy{N}.xml``, which is
#: why the connector matches its own files on the whole name — and why this one
#: does too, rather than on a substring that would happily take both.
CATALOG_SITEMAP = re.compile(r"/sitemap/(vacancies\d+)\.xml$")

#: A catalogue URL as the sitemap writes it: ``/vacancies/{slug}`` and nothing
#: else. Anything deeper is a page of one, and is measured from the page itself.
CATALOG_PATH = re.compile(r"^/vacancies/([^/]+)/?$")

#: Any link into the catalogue found in a page's markup, pagination included.
#: Deliberately loose: what shapes exist is the thing being measured.
CATALOG_LINK = re.compile(r"/vacancies/[^\s\"'<>\\)]*")

#: A vacancy id anywhere in the document — in an ``href`` or inside the escaped
#: JSON of the boot state. The single least assuming way to ask "does this page
#: list vacancies", because it reads hh's own URL shape rather than a key name
#: this repository would otherwise have to invent.
VACANCY_ID = re.compile(r"/vacancy/(\d+)")

#: The same question asked of the parsed state, by key rather than by URL. Both
#: numbers are reported: agreeing they are evidence, disagreeing they are the
#: interesting part.
STATE_ID_KEY = re.compile(r"vacancyid$", re.IGNORECASE)

#: What "related to development" means for this measurement, and nothing more.
#: Wide on purpose — the brief asks for the whole neighbouring circle, because
#: too narrow loses postings for good while too wide costs a scoring pass that
#: is already written — and reported as matched slugs so that a person, not this
#: tuple, makes the call. Latin terms are how a catalogue slug transliterates
#: them; Cyrillic ones are how ``professional_roles`` names them.
DEV_TERMS: tuple[str, ...] = (
    "python",
    "backend",
    "back-end",
    "razrabotchik",
    "programmist",
    "developer",
    "devops",
    "sre",
    "data",
    "analitik",
    "analyst",
    "machine",
    "qa",
    "test",
    "avtomatiz",
    "integrac",
    "integrat",
    "frontend",
    "fullstack",
    "sistemnyy",
    "dba",
    "ml",
    "программист",
    "разработчик",
    "данных",
    "аналитик",
    "тестировщик",
    "машинного",
    "интеграц",
    "автоматизац",
    "администратор баз",
    "системный",
)

#: Below this a term is matched as a whole token rather than as a substring.
#: ``ml`` inside ``kremlin`` and ``qa`` inside anything are noise, and a probe
#: whose output has to be filtered by eye is a probe nobody runs twice.
MIN_SUBSTRING_TERM = 4

#: The id seen as ``roles=96`` in hh's own advertising telemetry, which the
#: directory is asked about by name so that the guess is either confirmed or
#: replaced by the real one.
ADVERTISED_ROLE_ID = "96"

#: Slugs and ids listed in full in the console report. The JSON dump carries
#: everything; a terminal that scrolls for five thousand lines carries nothing.
MAX_SAMPLE = 40


#: Everything that is not a letter or a digit separates one word from the next.
#: The Cyrillic halves are written as escapes rather than as themselves: a range
#: of Cyrillic letters sitting next to ``a-zA-Z`` in one class is exactly the
#: confusable-character mistake ruff's RUF001 exists to catch, and a class that
#: silently contains a Cyrillic ``a`` instead of a Latin one is unreadable and
#: wrong in a way no test would show.
WORD_BREAK = re.compile(r"[^0-9a-zA-Z\u0430-\u044f\u0451\u0410-\u042f\u0401]+")


def _tokens(text: str) -> tuple[str, ...]:
    """A name split into the words a term may be matched against."""
    return tuple(part for part in WORD_BREAK.split(text.casefold()) if part)


def matched_terms(text: str, terms: Sequence[str] = DEV_TERMS) -> tuple[str, ...]:
    """Which of these terms this name carries, and by which rule.

    Long terms match as substrings, so ``razrabotchik`` finds
    ``razrabotchik-python`` and ``администратор баз`` finds the role whose name
    continues ``данных``. Short ones match a whole token only; see
    :data:`MIN_SUBSTRING_TERM` for what that is worth.
    """
    lowered = text.casefold()
    tokens = set(_tokens(text))

    def carried(term: str) -> bool:
        needle = term.casefold()
        if len(needle) < MIN_SUBSTRING_TERM:
            return needle in tokens
        return needle in lowered

    return tuple(term for term in terms if carried(term))


class CatalogFile(BaseModel):
    """One ``vacancies{N}.xml``, as this run read it."""

    model_config = ConfigDict(frozen=True)

    name: str
    url: str
    body_bytes: int
    #: Every ``<loc>`` in the file, whether or not it is a catalogue URL.
    locs: int
    #: Distinct ``/vacancies/{slug}`` values found among them.
    slugs: int
    #: Entries carrying a ``<lastmod>``. The 2026-09-06 note says none do; this
    #: is the number that either confirms it or retires the claim.
    with_lastmod: int


class CatalogIndex(BaseModel):
    """Every catalogue slug one host publishes, and which of them look relevant."""

    model_config = ConfigDict(frozen=True)

    host: str
    files: tuple[CatalogFile, ...] = ()
    slugs: tuple[str, ...] = ()
    matched: tuple[str, ...] = ()


class StateKey(BaseModel):
    """One top-level key of the boot state: what it is called and how big it is.

    A census rather than a lookup. Naming the key we hope holds a vacancy list
    would be a guess, and a guess that finds nothing reads exactly like a page
    that holds nothing.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    kind: str
    size: int | None = None


class CatalogPage(BaseModel):
    """One catalogue page, measured. Every field here is an observation."""

    model_config = ConfigDict(frozen=True)

    url: str
    body_bytes: int
    #: Whether the frontend's boot state is on this page at all. If it is not,
    #: the catalogue route needs a different reader than the vacancy route, and
    #: that is worth knowing before anybody designs one.
    has_state: bool = False
    state_parsed: bool = False
    state_keys: tuple[StateKey, ...] = ()
    #: Distinct ids in the document, and distinct ids under a ``vacancyId`` key
    #: in the parsed state. Two independent counts of the same thing.
    ids_in_document: tuple[str, ...] = ()
    ids_in_state: tuple[str, ...] = ()
    #: Distinct catalogue links found in the markup, split by the one property
    #: that decides whether we may follow them.
    links_without_query: tuple[str, ...] = ()
    links_with_query: tuple[str, ...] = ()


class Role(BaseModel):
    """One entry of ``api.hh.ru/professional_roles``."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    category: str | None = None


class RoleDirectory(BaseModel):
    """The published directory, and what it calls the development family."""

    model_config = ConfigDict(frozen=True)

    total: int = 0
    matched: tuple[Role, ...] = ()
    #: What id 96 actually is, or None when the directory has no such entry.
    advertised: Role | None = None


class ProbeReport(BaseModel):
    """One run of the probe. This is the artefact docs/SOURCES.md quotes."""

    model_config = ConfigDict(frozen=True)

    measured_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    host: str
    terms: tuple[str, ...] = DEV_TERMS
    index: CatalogIndex | None = None
    page: CatalogPage | None = None
    roles: RoleDirectory | None = None
    #: What could not be measured and why. A probe that half worked has to say
    #: which half, or its silence is read as an answer.
    notes: tuple[str, ...] = ()


def catalog_sitemaps(body: str, host: str) -> list[tuple[str, str]]:
    """The ``vacancies{N}.xml`` files this index lists, on this host only.

    Host-checked for the reason the connector checks it: a sitemap is somebody
    else's document and every URL in it is input.
    """
    found = {
        (match.group(1), url)
        for url in SITEMAP_LOC.findall(body)
        if (match := CATALOG_SITEMAP.search(url)) and urlsplit(url).hostname == host
    }
    return sorted(found)


def catalog_entries(body: str, host: str) -> tuple[tuple[str, ...], int, int]:
    """Slugs, total ``<loc>`` count and how many entries carry a ``lastmod``.

    Returned together because the second and third numbers are what say whether
    the first is trustworthy: a file whose every line is a catalogue URL is a
    different thing from one where we recognised a tenth of them.
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


def _walk_state(value: Any) -> Iterator[tuple[str, Any]]:
    """Every ``(key, value)`` pair anywhere in the boot state.

    ``Any`` because this is a third party's whole boot state, dozens of
    unrelated keys deep, and the point of the walk is that nothing about its
    shape is assumed.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key), item
            yield from _walk_state(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_state(item)


def state_ids(state: Any) -> tuple[str, ...]:
    """Distinct vacancy ids found under a ``vacancyId`` key, at any depth."""
    found: list[str] = []
    for key, value in _walk_state(state):
        if not STATE_ID_KEY.search(key):
            continue
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
            found.append(str(value))
    return tuple(dict.fromkeys(found))


def state_census(state: Any) -> tuple[StateKey, ...]:
    """The top level of the boot state: key, type and size.

    Containers first and biggest first, then everything else alphabetically. The
    ordering matters because this list is read by a person looking for the place
    a vacancy list would sit, and sorting a dict of one key below a string of
    nine characters — which one ``len`` for everything does — puts the candidates
    underneath the page's locale setting.
    """
    if not isinstance(state, dict):
        return ()
    ranked = [
        (
            0 if isinstance(value, dict | list) else 1,
            -len(value) if isinstance(value, dict | list | str) else 0,
            str(name),
            StateKey(
                name=str(name),
                kind=type(value).__name__,
                size=len(value) if isinstance(value, dict | list | str) else None,
            ),
        )
        for name, value in state.items()
    ]
    return tuple(entry[-1] for entry in sorted(ranked, key=lambda entry: entry[:-1]))


def read_page(url: str, body: str) -> CatalogPage:
    """One catalogue page as a set of observations.

    Nothing here fails: a page without the marker, without ids or without links
    is a real answer to the question being asked, and the fork in the brief
    turns on exactly that answer.
    """
    ids = tuple(dict.fromkeys(VACANCY_ID.findall(body)))
    links = tuple(dict.fromkeys(CATALOG_LINK.findall(body)))
    match = STATE_MARKER.search(body)
    state: Any = None
    parsed = False
    if match is not None:
        try:
            state = json.loads(html_lib.unescape(match.group(1)))
            parsed = True
        except ValueError:
            logger.warning("sources.hh_probe.state_unparsed", url=url)
    return CatalogPage(
        url=url,
        body_bytes=len(body),
        has_state=match is not None,
        state_parsed=parsed,
        state_keys=state_census(state),
        ids_in_document=ids,
        ids_in_state=state_ids(state),
        links_without_query=tuple(link for link in links if "?" not in link),
        links_with_query=tuple(link for link in links if "?" in link),
    )


def _is_category(value: dict[str, Any]) -> bool:
    """Whether this object holds other named objects rather than being one.

    The shape test, rather than the key name ``roles``, for the reason the whole
    module is written this way: what the endpoint nests under what is the thing
    being measured. It matters because a category id and a role id are separate
    numbering spaces — hh has both a category 11 and a role 11 — so reading them
    into one table by id loses whichever came second.
    """
    return any(
        isinstance(item, list) and any(isinstance(element, dict) for element in item)
        for item in value.values()
    )


def read_roles(payload: Any, terms: Sequence[str] = DEV_TERMS) -> RoleDirectory:
    """The professional-role directory, read without assuming its nesting.

    ``professional_roles`` is documented as categories holding roles, and this
    walks for any object carrying both an ``id`` and a textual ``name`` instead
    of relying on that: the measurement is what the endpoint returns today, and
    a shape change should show up as a different count rather than as an empty
    result that reads like "hh has no developer roles".
    """
    roles: dict[str, Role] = {}

    def visit(value: Any, category: str | None) -> None:
        if isinstance(value, dict):
            name = value.get("name")
            identifier = value.get("id")
            named = (
                isinstance(name, str) and bool(name.strip()) and isinstance(identifier, int | str)
            )
            if named and not _is_category(value):
                roles.setdefault(
                    str(identifier), Role(id=str(identifier), name=str(name), category=category)
                )
            for item in value.values():
                visit(item, str(name) if named and _is_category(value) else category)
        elif isinstance(value, list):
            for item in value:
                visit(item, category)

    visit(payload, None)
    matched = tuple(role for role in roles.values() if matched_terms(role.name, terms))
    return RoleDirectory(
        total=len(roles),
        matched=tuple(sorted(matched, key=lambda role: role.name)),
        advertised=roles.get(ADVERTISED_ROLE_ID),
    )


async def probe(
    http: SourceHTTP,
    site: HHSite,
    *,
    terms: Sequence[str] = DEV_TERMS,
    slug: str | None = None,
    max_files: int | None = None,
    read_roles_directory: bool = True,
) -> ProbeReport:
    """Take the three measurements, and record what could not be taken.

    Every failure is caught and named rather than raised. A probe exists to come
    back with an answer about the part of hh it could reach, and a traceback
    after eleven of fifteen files is a run that measured nothing.
    """
    notes: list[str] = []
    index = await _measure_index(http, site, terms=terms, max_files=max_files, notes=notes)
    page = await _measure_page(http, site, index=index, slug=slug, notes=notes)
    roles = (
        await _measure_roles(terms=terms, http=http, notes=notes) if read_roles_directory else None
    )
    return ProbeReport(
        host=site.host,
        terms=tuple(terms),
        index=index,
        page=page,
        roles=roles,
        notes=tuple(notes),
    )


async def _measure_index(
    http: SourceHTTP,
    site: HHSite,
    *,
    terms: Sequence[str],
    max_files: int | None,
    notes: list[str],
) -> CatalogIndex | None:
    """Question one: which catalogue files exist and what slugs do they hold."""
    index_url = f"https://{site.host}{SITEMAP_INDEX_PATH}"
    try:
        body = await http.get_text(index_url, cache_ttl=SITEMAP_CACHE_TTL)
    except (SourceError, OSError) as exc:
        notes.append(f"карта сайта {index_url} не прочитана: {exc}")
        return None

    listed = catalog_sitemaps(body, site.host)
    if not listed:
        notes.append(f"в {index_url} нет ни одного файла vacancies*.xml")
        return CatalogIndex(host=site.host)
    if max_files is not None:
        skipped = len(listed) - max_files
        listed = listed[:max_files]
        if skipped > 0:
            notes.append(f"прочитано {len(listed)} файлов каталога из {len(listed) + skipped}")

    files: list[CatalogFile] = []
    slugs: list[str] = []
    for name, url in listed:
        try:
            file_body = await http.get_text(url, cache_ttl=SITEMAP_CACHE_TTL)
        except (SourceError, OSError) as exc:
            notes.append(f"{name}: не прочитан ({exc})")
            continue
        found, locs, with_lastmod = catalog_entries(file_body, site.host)
        files.append(
            CatalogFile(
                name=name,
                url=url,
                body_bytes=len(file_body),
                locs=locs,
                slugs=len(found),
                with_lastmod=with_lastmod,
            )
        )
        slugs.extend(found)
        logger.info(
            "sources.hh_probe.catalog_file",
            file=name,
            body_bytes=len(file_body),
            locs=locs,
            slugs=len(found),
        )

    unique = tuple(dict.fromkeys(slugs))
    return CatalogIndex(
        host=site.host,
        files=tuple(files),
        slugs=unique,
        matched=tuple(slug for slug in unique if matched_terms(slug, terms)),
    )


async def _measure_page(
    http: SourceHTTP,
    site: HHSite,
    *,
    index: CatalogIndex | None,
    slug: str | None,
    notes: list[str],
) -> CatalogPage | None:
    """Question two: what is actually on one catalogue page.

    One page, not a sample of them. The question is whether the shape exists at
    all, and a second page costs another four seconds to answer it again.
    """
    chosen = slug or _first(index)
    if chosen is None:
        # Two different silences, and telling them apart is the whole reason
        # this branch is written out: "the sitemap holds no such slug" is a
        # measurement, and "we never read the sitemap" is not one.
        notes.append(
            "страница каталога не выбрана: карта сайта не прочитана"
            if index is None
            else "страница каталога не выбрана: в карте сайта нет ни одного слага"
        )
        return None
    url = f"https://{site.host}/vacancies/{chosen}"
    try:
        body = await http.get_text(url, cache_ttl=SITEMAP_CACHE_TTL)
    except HHChallengedError as exc:
        notes.append(f"страница каталога {url}: источник ответил проверкой на робота ({exc})")
        return None
    except (SourceError, OSError) as exc:
        notes.append(f"страница каталога {url} не прочитана: {exc}")
        return None
    return read_page(url, body)


def _first(index: CatalogIndex | None) -> str | None:
    """The slug to look at: a matched one if there is one, else any."""
    if index is None:
        return None
    return next(iter(index.matched), None) or next(iter(index.slugs), None)


async def _measure_roles(
    *, terms: Sequence[str], http: SourceHTTP, notes: list[str]
) -> RoleDirectory | None:
    """Question three: what hh's own directory calls these roles.

    ``api.hh.ru/professional_roles`` is one of the dictionaries that stayed open
    when the jobseeker half of that host closed; the transport blocks
    ``/vacancies`` there and nothing else.
    """
    url = "https://api.hh.ru/professional_roles"
    try:
        payload = await http.get_json(url, cache_ttl=SITEMAP_CACHE_TTL)
    except (SourceError, OSError) as exc:
        notes.append(f"справочник {url} не прочитан: {exc}")
        return None
    return read_roles(payload, terms)
