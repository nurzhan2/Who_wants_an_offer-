"""The Stage 0 catalogue probe: it reports what a document contains.

**Every document in this file is invented, and that is the whole caveat.** The
hh connector's own tests run against pages hh really served, because they assert
what hh sends. These cannot: what a catalogue page holds is precisely the thing
nobody has measured, and a fixture written here would pin what this repository
imagined. So nothing below claims anything about hh. What it holds is the other
half — that the instrument reports the document it was given, including when the
document is empty, and that the URLs it builds are ones the transport lets
through. An instrument that miscounts is worse than no instrument, because its
output looks like a measurement.

The one assertion here that IS about hh is negative and belongs in code: the
probe reads ``vacancies{N}.xml`` and must never touch ``resumes{N}.xml``. Those
are living people's CVs, the two file names differ by four letters, and the
connector already learned to match on the whole name rather than a substring.

Nothing here reaches the network; respx answers every request.
"""

import html
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx

from app.core.config import settings
from app.sources.hh import HHSite, HHSource
from app.sources.hh_probe import (
    ADVERTISED_ROLE_ID,
    catalog_entries,
    catalog_sitemaps,
    matched_terms,
    probe,
    read_page,
    read_roles,
)
from app.sources.http import SourceClient

pytestmark = pytest.mark.unit

HOST = "almaty.hh.kz"
SITE = HHSite(host=HOST, city="Алматы", country="KZ", default=True)
INDEX_URL = f"https://{HOST}/sitemap/main.xml"
ROBOTS_URL = f"https://{HOST}/robots.txt"
API_ROBOTS_URL = "https://api.hh.ru/robots.txt"
ROLES_URL = "https://api.hh.ru/professional_roles"

#: The wildcard group of the live file. Copied from ``test_sources_hh.py`` so
#: that the probe is proved against the same rules the crawl runs under.
HH_ROBOTS = (
    "User-agent: *\n"
    "Allow: *?u*\n"
    "Allow: *?currencyCode*\n"
    "Allow: *?vacancyId*\n"
    "Disallow: *?*\n"
    "Disallow: /resume$\n"
)


# -- helpers -----------------------------------------------------------


async def _instant(seconds: float) -> None:
    """The rate limiter's sleep, removed. A test must not spend four seconds."""
    return None


def index(paths: list[str]) -> str:
    """A sitemap index listing those files."""
    body = "".join(f"<sitemap><loc>{path}</loc></sitemap>" for path in paths)
    return (
        "<?xml version='1.0' encoding='utf-8'?>"
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + body
        + "</sitemapindex>"
    )


def catalog(slugs: list[str], *, host: str = HOST) -> str:
    """A catalogue sitemap listing those slugs, with no ``lastmod``.

    No ``lastmod`` because that is what the 2026-09-06 note reports; the probe
    counts them either way, and ``test_catalogue_entries_count_what_they_see``
    is what holds it to counting rather than to expecting.
    """
    body = "".join(f"<url><loc>https://{host}/vacancies/{slug}</loc></url>" for slug in slugs)
    return (
        "<?xml version='1.0' encoding='utf-8'?>"
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + body + "</urlset>"
    )


def page(*, ids: list[int], links: list[str], state: dict[str, Any] | None) -> str:
    """An INVENTED catalogue page. See the module docstring before believing it."""
    markup = "".join(f'<a href="/vacancy/{vacancy_id}">x</a>' for vacancy_id in ids)
    markup += "".join(f'<a href="{link}">page</a>' for link in links)
    template = (
        '<template style="display:none" id="HH-Lux-InitialState">'
        + html.escape(json.dumps(state, ensure_ascii=False))
        + "</template>"
        if state is not None
        else ""
    )
    return f"<!doctype html><html><body>{markup}{template}</body></html>"


@pytest.fixture
def http() -> Iterator[respx.MockRouter]:
    """Every outbound request, intercepted before it leaves the process."""
    with respx.mock(assert_all_called=False) as router:
        router.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=HH_ROBOTS))
        # 404 is what api.hh.ru really answers for robots.txt, and RFC 9309
        # reads that as no restrictions.
        router.get(API_ROBOTS_URL).mock(return_value=httpx.Response(404))
        yield router


@pytest.fixture
async def bound(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[HHSource]:
    """The hh connector on a client whose waiting has been removed.

    The real connector rather than a stub, so that everything the transport
    refuses — a query string, ``/search``, the resume files — refuses the probe
    too, and the test proves it instead of the docstring claiming it.
    """
    monkeypatch.setattr(settings, "http_cache_dir", None)
    client = SourceClient(sleep=_instant)
    source = HHSource()
    source.bind(client.bind(source))
    try:
        yield source
    finally:
        await client.aclose()


# -- the sitemap index -------------------------------------------------


def test_the_index_reader_takes_the_catalogue_family_and_nothing_else() -> None:
    """Four letters separate the catalogue from the crawl's own files."""
    body = index(
        [
            f"https://{HOST}/sitemap/vacancy0.xml",
            f"https://{HOST}/sitemap/vacancies0.xml",
            f"https://{HOST}/sitemap/vacancies14.xml",
            f"https://{HOST}/sitemap/resumes0.xml",
            f"https://{HOST}/sitemap/employers.xml",
            "https://astana.hh.kz/sitemap/vacancies0.xml",
        ]
    )

    assert catalog_sitemaps(body, HOST) == [
        ("vacancies0", f"https://{HOST}/sitemap/vacancies0.xml"),
        ("vacancies14", f"https://{HOST}/sitemap/vacancies14.xml"),
    ]


def test_catalogue_entries_count_what_they_see() -> None:
    """Slugs, the total ``<loc>`` count, and how many entries carry a date.

    The second number is what makes the first trustworthy: recognising 3 of 5
    URLs is a different measurement from recognising 5 of 5, and a probe that
    reported only the slugs would hide the difference.
    """
    body = index([f"https://{HOST}/sitemap/vacancies0.xml"])  # not a catalogue file
    slugs, locs, lastmods = catalog_entries(
        catalog(["python-razrabotchik", "buhgalter"]) + body, HOST
    )

    assert slugs == ("python-razrabotchik", "buhgalter")
    assert locs == 3
    assert lastmods == 0


def test_a_catalogue_url_on_another_host_or_with_a_query_is_not_ours() -> None:
    """A sitemap is somebody else's document and every URL in it is input."""
    body = (
        catalog(["razrabotchik"], host="astana.hh.kz")
        + f"<url><loc>https://{HOST}/vacancies/qa?page=2</loc></url>"
        + catalog(["devops"])
    )

    assert catalog_entries(body, HOST)[0] == ("devops",)


# -- the search terms --------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("web-razrabotchik", True),
        ("python-developer", True),
        ("html-verstalshchik", False),
        ("kredit-menedzher", False),
        ("qa-avtomatizator", True),
    ],
)
def test_a_short_term_matches_a_word_and_a_long_one_matches_inside(
    name: str, expected: bool
) -> None:
    """``ml`` inside ``html`` is noise; ``razrabotchik`` inside a slug is not.

    The whole value of a wide term list is that its output can be read, and a
    list that matches ``html-verstalshchik`` because it contains the letters of
    ``ml`` produces output nobody reads.
    """
    assert bool(matched_terms(name)) is expected


# -- one catalogue page ------------------------------------------------


def test_the_page_reader_counts_ids_and_splits_links_by_query() -> None:
    """Both counts, and the split that decides whether a link may be followed."""
    state = {
        "vacancySearchResult": {"vacancies": [{"vacancyId": 11}, {"vacancyId": 12}]},
        "userType": "anonymous",
    }
    body = page(
        ids=[11, 12],
        links=["/vacancies/python/2", "/vacancies/python?page=3"],
        state=state,
    )

    measured = read_page("https://x/vacancies/python", body)

    assert measured.has_state and measured.state_parsed
    assert measured.ids_in_document == ("11", "12")
    assert measured.ids_in_state == ("11", "12")
    assert measured.links_without_query == ("/vacancies/python/2",)
    assert measured.links_with_query == ("/vacancies/python?page=3",)
    assert [key.name for key in measured.state_keys] == ["vacancySearchResult", "userType"]


def test_a_page_with_nothing_on_it_is_an_answer_rather_than_a_failure() -> None:
    """The negative result is half the fork, so it must not raise."""
    measured = read_page("https://x/vacancies/python", "<html><body>nothing</body></html>")

    assert measured.has_state is False
    assert measured.ids_in_document == ()
    assert measured.ids_in_state == ()


def test_an_unparsable_state_is_recorded_as_present_and_unread() -> None:
    """ "There is a marker" and "we read it" are two different facts."""
    body = '<template style="display:none" id="HH-Lux-InitialState">not json</template>'

    measured = read_page("https://x/vacancies/python", body)

    assert measured.has_state is True
    assert measured.state_parsed is False


# -- the role directory ------------------------------------------------


ROLES: dict[str, Any] = {
    "categories": [
        {
            "id": "11",
            "name": "Информационные технологии",
            "roles": [
                {"id": "96", "name": "Программист, разработчик"},
                {"id": "160", "name": "DevOps-инженер"},
                {"id": "11", "name": "Аналитик данных"},
            ],
        },
        {
            "id": "17",
            "name": "Продажи",
            "roles": [{"id": "70", "name": "Менеджер по продажам"}],
        },
    ]
}


def test_a_category_id_and_a_role_id_do_not_collide() -> None:
    """They are separate numbering spaces, and this payload proves it matters.

    Category 11 is IT and role 11 is a data analyst. Reading both into one table
    by id loses whichever arrives second — and the one lost would have been a
    development role, which is the only kind this exercise is about.
    """
    directory = read_roles(ROLES)

    assert directory.total == 4
    assert {role.name for role in directory.matched} == {
        "Программист, разработчик",
        "DevOps-инженер",
        "Аналитик данных",
    }


def test_the_advertised_role_id_is_looked_up_rather_than_believed() -> None:
    """``roles=96`` came out of ad telemetry; the directory is what settles it."""
    directory = read_roles(ROLES)

    assert directory.advertised is not None
    assert directory.advertised.id == ADVERTISED_ROLE_ID
    assert directory.advertised.name == "Программист, разработчик"
    assert directory.advertised.category == "Информационные технологии"


def test_a_directory_without_that_id_says_so_instead_of_inventing_one() -> None:
    """The guess being wrong is a result, and has to survive to the report."""
    directory = read_roles({"categories": [{"id": "1", "name": "X", "roles": []}]})

    assert directory.advertised is None


# -- the whole probe ---------------------------------------------------


async def test_the_probe_reads_the_catalogue_and_never_the_resume_files(
    bound: HHSource, http: respx.MockRouter
) -> None:
    """The one assertion here that is about hh rather than about the instrument."""
    http.get(INDEX_URL).mock(
        return_value=httpx.Response(
            200,
            text=index(
                [
                    f"https://{HOST}/sitemap/vacancy0.xml",
                    f"https://{HOST}/sitemap/vacancies0.xml",
                    f"https://{HOST}/sitemap/resumes0.xml",
                ]
            ),
        )
    )
    http.get(f"https://{HOST}/sitemap/vacancies0.xml").mock(
        return_value=httpx.Response(200, text=catalog(["python-razrabotchik", "buhgalter"]))
    )
    http.get(f"https://{HOST}/vacancies/python-razrabotchik").mock(
        return_value=httpx.Response(200, text=page(ids=[11], links=[], state=None))
    )
    http.get(ROLES_URL).mock(return_value=httpx.Response(200, json=ROLES))

    report = await probe(bound.http, SITE)

    asked = [str(call.request.url) for call in http.calls]
    assert not any("resumes" in url for url in asked)
    assert not any("?" in url for url in asked)
    assert report.index is not None
    assert report.index.matched == ("python-razrabotchik",)
    assert report.page is not None and report.page.ids_in_document == ("11",)
    assert report.roles is not None and report.roles.total == 4
    assert report.notes == ()


async def test_a_challenge_on_a_catalogue_page_is_a_note_and_not_a_traceback(
    bound: HHSource, http: respx.MockRouter
) -> None:
    """A probe that dies on question two has still answered question one.

    The redirect into ``/account`` is what hh answered a live crawl with on
    2026-09-06, and the transport turns it into ``HHChallengedError``. A crawl
    must stop on that. A measurement must record it and finish the sentence.
    """
    http.get(INDEX_URL).mock(
        return_value=httpx.Response(200, text=index([f"https://{HOST}/sitemap/vacancies0.xml"]))
    )
    http.get(f"https://{HOST}/sitemap/vacancies0.xml").mock(
        return_value=httpx.Response(200, text=catalog(["python-razrabotchik"]))
    )
    http.get(f"https://{HOST}/vacancies/python-razrabotchik").mock(
        return_value=httpx.Response(
            302, headers={"location": f"https://{HOST}/account/captcha?backurl=%2F"}
        )
    )
    http.get(ROLES_URL).mock(return_value=httpx.Response(200, json=ROLES))

    report = await probe(bound.http, SITE)

    assert report.index is not None and report.index.slugs == ("python-razrabotchik",)
    assert report.page is None
    assert report.roles is not None
    assert any("проверкой на робота" in note for note in report.notes)


async def test_an_index_that_lists_no_catalogue_files_says_so(
    bound: HHSource, http: respx.MockRouter
) -> None:
    """The whole idea resting on a file family that is gone has to be loud."""
    http.get(INDEX_URL).mock(
        return_value=httpx.Response(200, text=index([f"https://{HOST}/sitemap/vacancy0.xml"]))
    )
    http.get(ROLES_URL).mock(return_value=httpx.Response(200, json=ROLES))

    report = await probe(bound.http, SITE, read_roles_directory=False)

    assert report.index is not None and report.index.slugs == ()
    assert report.page is None
    assert any("vacancies*.xml" in note for note in report.notes)


async def test_the_dictionary_can_be_skipped_and_the_rest_still_runs(
    bound: HHSource, http: respx.MockRouter
) -> None:
    """``--no-roles`` exists so a re-run costs one request instead of two."""
    http.get(INDEX_URL).mock(
        return_value=httpx.Response(200, text=index([f"https://{HOST}/sitemap/vacancies0.xml"]))
    )
    http.get(f"https://{HOST}/sitemap/vacancies0.xml").mock(
        return_value=httpx.Response(200, text=catalog(["devops"]))
    )
    http.get(f"https://{HOST}/vacancies/devops").mock(
        return_value=httpx.Response(200, text=page(ids=[1], links=[], state=None))
    )

    report = await probe(bound.http, SITE, read_roles_directory=False)

    assert report.roles is None
    assert not any(ROLES_URL in str(call.request.url) for call in http.calls)


async def test_the_named_slug_wins_over_the_first_match(
    bound: HHSource, http: respx.MockRouter
) -> None:
    """``--slug`` is how the same page is measured twice on different days."""
    http.get(INDEX_URL).mock(
        return_value=httpx.Response(200, text=index([f"https://{HOST}/sitemap/vacancies0.xml"]))
    )
    http.get(f"https://{HOST}/sitemap/vacancies0.xml").mock(
        return_value=httpx.Response(200, text=catalog(["python-razrabotchik", "devops"]))
    )
    http.get(f"https://{HOST}/vacancies/devops").mock(
        return_value=httpx.Response(200, text=page(ids=[7], links=[], state=None))
    )

    report = await probe(bound.http, SITE, slug="devops", read_roles_directory=False)

    assert report.page is not None
    assert report.page.url.endswith("/vacancies/devops")


async def test_a_probe_that_half_worked_says_which_half(
    bound: HHSource, http: respx.MockRouter
) -> None:
    """Every step that failed is named, and the steps that did not still report.

    The failure mode this guards against is a probe that comes back with three
    empty sections and no reason: that reads as "hh has no catalogue, no pages
    and no roles", which is a measurement, and it would be a false one.
    """
    http.get(INDEX_URL).mock(
        return_value=httpx.Response(
            200,
            text=index(
                [
                    f"https://{HOST}/sitemap/vacancies0.xml",
                    f"https://{HOST}/sitemap/vacancies1.xml",
                ]
            ),
        )
    )
    http.get(f"https://{HOST}/sitemap/vacancies0.xml").mock(
        return_value=httpx.Response(200, text=catalog(["devops"]))
    )
    http.get(f"https://{HOST}/sitemap/vacancies1.xml").mock(return_value=httpx.Response(404))
    http.get(f"https://{HOST}/vacancies/devops").mock(return_value=httpx.Response(404))
    http.get(ROLES_URL).mock(return_value=httpx.Response(403))

    report = await probe(bound.http, SITE)

    assert report.index is not None and report.index.slugs == ("devops",)
    assert [entry.name for entry in report.index.files] == ["vacancies0"]
    assert report.page is None
    assert report.roles is None
    assert any("vacancies1" in note for note in report.notes)
    assert any("страница каталога" in note for note in report.notes)
    assert any("справочник" in note for note in report.notes)


async def test_reading_fewer_files_is_recorded_as_a_partial_measurement(
    bound: HHSource, http: respx.MockRouter
) -> None:
    """``--files 1`` makes the slug list incomplete, and the report has to say so."""
    http.get(INDEX_URL).mock(
        return_value=httpx.Response(
            200,
            text=index(
                [
                    f"https://{HOST}/sitemap/vacancies0.xml",
                    f"https://{HOST}/sitemap/vacancies1.xml",
                ]
            ),
        )
    )
    http.get(f"https://{HOST}/sitemap/vacancies0.xml").mock(
        return_value=httpx.Response(200, text=catalog(["devops"]))
    )
    http.get(f"https://{HOST}/vacancies/devops").mock(
        return_value=httpx.Response(200, text=page(ids=[1], links=[], state=None))
    )

    report = await probe(bound.http, SITE, max_files=1, read_roles_directory=False)

    assert any("из 2" in note for note in report.notes)
    assert not any("vacancies1" in str(call.request.url) for call in http.calls)


async def test_an_unreadable_index_stops_the_measurement_and_names_the_file(
    bound: HHSource, http: respx.MockRouter
) -> None:
    """Without the index there are no catalogue files to ask about."""
    http.get(INDEX_URL).mock(return_value=httpx.Response(404))

    report = await probe(bound.http, SITE, read_roles_directory=False)

    assert report.index is None
    assert report.page is None
    assert any("карта сайта" in note for note in report.notes)
    # "we never read the sitemap" must not be reported as "the sitemap holds
    # no development slugs": the second is a measurement and the first is not.
    assert not any("нет ни одного слага" in note for note in report.notes)
