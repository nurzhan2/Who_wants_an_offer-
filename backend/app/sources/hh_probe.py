"""The measurement behind the crawl by profession, and the way to take it again.

**What it found.** Run against ``almaty.hh.kz`` on 2026-09-08 and recorded in
docs/SOURCES.md § «Обход по профессиям»: 15 ``vacancies{N}.xml`` files holding
10 435 catalogue slugs, 630 of them development; ``/vacancies/programmist`` is
1.49 MB carrying the frontend's boot state with 50 vacancy ids on it, in the
markup and in the state alike; the only paging is ``?page=0..3``, which hh's
``Disallow: *?*`` closes to us, with no query-less form of it anywhere on the
page; and ``api.hh.ru/professional_roles`` answers with 194 roles, of which id
96 is «Программист, разработчик» — confirming the ``roles=96`` that had only
been seen in hh's advertising telemetry.

That answered the fork this module was written for, and ``hh.py`` now walks the
catalogue. What this file is for from here is the two jobs a measurement has
after it has been believed.

**Watching it stay true.** The crawl rests on facts about somebody else's site:
that a catalogue page lists ids, that the slug family exists, that paging is
closed. Each of them can change without warning, and each would fail quietly —
a catalogue that stops listing ids does not raise, it returns an empty set and
the walk falls back to the date order it had before, silently collecting sales
managers again. Re-running this says so in one screen.

**Checking the one step nobody can verify by reading.** A catalogue slug is a
transliteration of a Russian role name, and matching «Программист, разработчик»
to ``programmist`` is done by a table in ``hh_roles.py`` that no amount of care
makes self-evidently right. Given the profile's keywords, ``--keyword``, this
prints the plan the production code would build — every role the profile asked
for, and the slugs each one found on the live site. A role with an empty list
beside it is the finding: hh names that work in a way the table did not
recognise, and the fix is a term in ``hh_roles.yaml``.

**The negative result was a sampling error, and that is worth keeping.** The
first run of this probe opened ``digital-analitik``, found almost nothing, and
reported that catalogue pages do not list vacancies. One page of one rare
profession is not a measurement of the catalogue; ``programmist`` was, and it
answered the other way. The probe now takes ``--slug`` for exactly this reason,
and anybody re-running it should open a profession the city actually hires for
before concluding anything.

**What it does not do.** It decides nothing and stores nothing. The terms in
:data:`DEV_TERMS` are one measurement's search terms — "how much of this
catalogue is development at all" — and not the project's answer to which roles a
profile wants, which is ``hh_roles.yaml`` and is applied by the crawl. And it
changes no rate: every request goes through the hh connector's own
:class:`~app.sources.http.SourceHTTP`, so robots.txt, the ban on query strings,
the challenge detection and the measured 0.25 requests a second apply here
exactly as they apply to a crawl. A probe that went faster to answer sooner
would answer with a captcha.

One question a single run still cannot answer: whether a catalogue page's
composition changes over time. That needs two runs and a diff, which is what
``--json`` is for — dump it, run it again tomorrow, compare.
"""

import html as html_lib
import json
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.core.exceptions import SourceError
from app.core.logging import get_logger
from app.sources.hh import (
    ROLES_URL,
    SITEMAP_CACHE_TTL,
    SITEMAP_INDEX_PATH,
    STATE_MARKER,
    VACANCY_ID_ON_PAGE,
    HHSite,
    catalog_entries,
    catalog_sitemaps,
)
from app.sources.hh_roles import (
    DirectoryRole,
    carries,
    families_for,
    load_families,
    read_directory,
    roles_for,
    slugs_for,
)
from app.sources.http import HHChallengedError, SourceHTTP

logger = get_logger(__name__)

#: The same question asked of the parsed state as ``VACANCY_ID_ON_PAGE`` asks
#: of the document, by key rather than by URL. Both numbers are reported:
#: agreeing they are evidence, disagreeing they are the interesting part.
STATE_ID_KEY = re.compile(r"vacancyid$", re.IGNORECASE)

#: Any link into the catalogue found in a page's markup, pagination included.
#: Deliberately loose: measured 2026-09-08, the only next-page links are
#: ``?page=0..3``, and it is this pattern plus the split on ``?`` that said so.
CATALOG_LINK = re.compile(r"/vacancies/[^\s\"'<>\\)]*")

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


#: The id seen as ``roles=96`` in hh's own advertising telemetry. Confirmed
#: 2026-09-08 against the directory: «Программист, разработчик».
ADVERTISED_ROLE_ID = "96"

#: Slugs and ids listed in full in the console report. The JSON dump carries
#: everything; a terminal that scrolls for ten thousand lines carries nothing.
MAX_SAMPLE = 40


def matched_terms(text: str, terms: Sequence[str] = DEV_TERMS) -> tuple[str, ...]:
    """Which of these terms this name carries.

    The production matcher, not a second one: the crawl decides which catalogue
    pages to open with :func:`app.sources.hh_roles.carries`, so a measurement
    made with a different rule would be a measurement of something else.
    """
    return carries(text, terms)


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


class PlannedRole(BaseModel):
    """One role the profile asked for, and the catalogue pages it names."""

    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    category: str | None = None
    slugs: tuple[str, ...] = ()


class CrawlPlan(BaseModel):
    """What the crawl would actually open for a given profile.

    The one part of this report that is not a measurement of hh but a
    measurement of us: the same functions the connector calls, run against the
    same live slug list, so that the step nobody can verify by reading — a
    Cyrillic role name matched against a Latin slug — can be checked by eye
    against the real site instead of trusted.

    A role with no slugs beside it is the finding to look for. It means hh names
    that work in a way this repository's transliteration did not recognise, and
    the fix is a term in ``hh_roles.yaml`` rather than an argument about it.
    """

    model_config = ConfigDict(frozen=True)

    keywords: tuple[str, ...] = ()
    families: tuple[str, ...] = ()
    roles: tuple[PlannedRole, ...] = ()
    #: Slugs no selected role named, picked by the profile's own words. The
    #: fallback that keeps this working for a profile no family describes.
    by_keyword: tuple[str, ...] = ()
    total: int = 0


def plan_for(
    keywords: Sequence[str], directory: Sequence[DirectoryRole], slugs: Sequence[str]
) -> CrawlPlan:
    """Run the production profile-to-slugs chain and report every step of it."""
    families = families_for(keywords, load_families())
    roles = roles_for(families, directory)
    planned = tuple(
        PlannedRole(
            id=role.id,
            name=role.name,
            category=role.category,
            # One role at a time, and with no keywords, so that each line of the
            # report says what THAT role found rather than what the union did.
            slugs=slugs_for([role], (), slugs),
        )
        for role in roles
    )
    chosen = slugs_for(roles, keywords, slugs)
    named = {slug for role in planned for slug in role.slugs}
    return CrawlPlan(
        keywords=tuple(keywords),
        families=tuple(family.key for family in families),
        roles=planned,
        by_keyword=tuple(slug for slug in chosen if slug not in named),
        total=len(chosen),
    )


class ProbeReport(BaseModel):
    """One run of the probe. This is the artefact docs/SOURCES.md quotes."""

    model_config = ConfigDict(frozen=True)

    measured_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    host: str
    terms: tuple[str, ...] = DEV_TERMS
    index: CatalogIndex | None = None
    page: CatalogPage | None = None
    roles: RoleDirectory | None = None
    #: Only filled when the probe was given a profile's keywords to plan with.
    plan: CrawlPlan | None = None
    #: What could not be measured and why. A probe that half worked has to say
    #: which half, or its silence is read as an answer.
    notes: tuple[str, ...] = ()


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
    ids = tuple(dict.fromkeys(VACANCY_ID_ON_PAGE.findall(body)))
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


def read_roles(payload: Any, terms: Sequence[str] = DEV_TERMS) -> RoleDirectory:
    """The professional-role directory, and what it calls the wanted family.

    The reading is :func:`app.sources.hh_roles.read_directory`, which is what the
    crawl uses; this adds only the two things a measurement wants on top —
    which entries the terms matched, and what the id seen in hh's advertising
    telemetry turns out to be.
    """
    directory = read_directory(payload)
    matched = tuple(role for role in directory if matched_terms(role.name, terms))
    return RoleDirectory(
        total=len(directory),
        matched=tuple(
            Role(id=str(role.id), name=role.name, category=role.category)
            for role in sorted(matched, key=lambda role: role.name)
        ),
        advertised=next(
            (
                Role(id=str(role.id), name=role.name, category=role.category)
                for role in directory
                if str(role.id) == ADVERTISED_ROLE_ID
            ),
            None,
        ),
    )


async def probe(
    http: SourceHTTP,
    site: HHSite,
    *,
    terms: Sequence[str] = DEV_TERMS,
    slug: str | None = None,
    max_files: int | None = None,
    read_roles_directory: bool = True,
    keywords: Sequence[str] = (),
) -> ProbeReport:
    """Take the three measurements, and record what could not be taken.

    Every failure is caught and named rather than raised. A probe exists to come
    back with an answer about the part of hh it could reach, and a traceback
    after eleven of fifteen files is a run that measured nothing.
    """
    notes: list[str] = []
    index = await _measure_index(http, site, terms=terms, max_files=max_files, notes=notes)
    page = await _measure_page(http, site, index=index, slug=slug, notes=notes)
    directory: tuple[DirectoryRole, ...] = ()
    roles: RoleDirectory | None = None
    if read_roles_directory:
        payload = await _read_roles_payload(http, notes)
        if payload is not None:
            directory = read_directory(payload)
            roles = read_roles(payload, terms)
    plan: CrawlPlan | None = None
    if keywords:
        # Two different silences again, and the report has to tell them apart:
        # "you gave me no profile" and "I could not read the slug list" produce
        # the same empty section and mean opposite things.
        if index is None:
            notes.append("план обхода не построен: список слагов не прочитан")
        else:
            if not directory:
                notes.append("план построен только по ключевым словам: справочник не прочитан")
            plan = plan_for(keywords, directory, index.slugs)
    return ProbeReport(
        host=site.host,
        terms=tuple(terms),
        index=index,
        page=page,
        roles=roles,
        plan=plan,
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


async def _read_roles_payload(http: SourceHTTP, notes: list[str]) -> Any:
    """Question three: hh's own directory, fetched once and read twice.

    ``api.hh.ru/professional_roles`` is one of the dictionaries that stayed open
    when the jobseeker half of that host closed; the transport blocks
    ``/vacancies`` there and nothing else. ``Any`` because the payload is hh's
    and is validated by :func:`app.sources.hh_roles.read_directory` one line
    later, in the module the crawl shares with this one.
    """
    try:
        return await http.get_json(ROLES_URL, cache_ttl=SITEMAP_CACHE_TTL)
    except (SourceError, OSError) as exc:
        notes.append(f"справочник {ROLES_URL} не прочитан: {exc}")
        return None
