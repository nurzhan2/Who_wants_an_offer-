"""Where a vacancy came from, where to go, and which rows are not real.

The list and the card show every source a vacancy was collected from and link
to the original — to ``vacancy_source.url`` as stored, never an address rebuilt
from an id, because the sources differ in domain and hh in regional subdomain.
When several sources carry one vacancy, the link goes to the one holding the
most data.

Seed rows are the other half. On 2026-09-16 every telegram and jsearch row in
the database came from ``scripts/seed.py``. They are shown as such and kept out
of the queues that spend a model call.
"""

from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import MatchRepository, ProfileRepository, VacancyRepository
from app.db.seed_rows import SEED_URL_PREFIX, is_seed_url, payload_size
from app.documents import store as documents_store
from app.letters import store as letters_store
from app.schemas.crawl import SavedState, SearchUse
from app.schemas.vacancy import VacancyFilter
from app.services import vacancies as vacancies_service
from app.sources.arbeitnow import ArbeitnowSource
from app.sources.base import SearchQuery, mentions
from app.sources.hh import HHSource
from factories import make_match, make_profile, make_vacancy

SEED_URL = f"{SEED_URL_PREFIX}telegram/dev-001"


# ── matching a phrase ─────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("needle", "found"),
    [
        ("python", True),
        ("Python Developer", True),
        ("developer python", True),
        ("Java Developer", False),
        ("pyth", True),
    ],
)
def test_a_title_matches_by_all_of_its_words(needle: str, found: bool) -> None:
    """«Backend Developer (Python)» is the job a person typing «Python Developer» means."""
    haystack = "senior backend developer (python) at acme"

    assert mentions(haystack, needle) is found


@pytest.mark.unit
def test_a_feed_source_previews_the_words_it_filters_by() -> None:
    """The default preview: the plan's keywords, each once, in plan order."""
    queries = [
        SearchQuery(keywords=("Python Developer", "Backend")),
        SearchQuery(keywords=("Python Developer",), area="Berlin"),
    ]

    preview = ArbeitnowSource().preview_search(queries, [])

    assert preview.use is SearchUse.FILTER
    assert preview.terms == ["Python Developer", "Backend"]
    assert preview.more == 0


# ── the seed marker ───────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url", "seed"),
    [
        (SEED_URL, True),
        ("https://example.test/jsearch/dev-058", True),
        # The test factories' own postings stand for real ones.
        ("https://example.test/hh/vacancy-1", False),
        # A real arbeitnow slug with the seed's id shape in it.
        ("https://www.arbeitnow.com/jobs/companies/acme/python-dev-123", False),
        ("https://almaty.hh.kz/vacancy/136364510", False),
    ],
)
def test_only_the_seeds_exact_address_is_a_seed(url: str, seed: bool) -> None:
    """The host alone is too broad and the id alone is too weak."""
    assert is_seed_url(url) is seed


@pytest.mark.unit
def test_a_larger_payload_is_a_fuller_one() -> None:
    """The size the card sorts by grows with what the row holds."""
    assert payload_size({"a": "текст"}) > payload_size({"a": ""})


# ── the list and the card ─────────────────────────────────────────────


async def vacancy_with_sources(
    vacancies: VacancyRepository, seed: str, rows: list[tuple[str, str, dict[str, object]]]
) -> UUID:
    """One vacancy carried by several source rows, in the given order."""
    vacancy = make_vacancy(seed)
    vacancy_id: UUID | None = None
    for slug, url, raw in rows:
        result = await vacancies.upsert_by_external_id(
            vacancy, source_slug=slug, external_id=f"{slug}-{seed}", url=url, raw=raw
        )
        vacancy_id = result.vacancy_id
    assert vacancy_id is not None
    return vacancy_id


@pytest.mark.db
async def test_the_list_links_to_the_fullest_source_first(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """A thin aggregator row stored first must not win over the full hh page."""
    vacancy_id = await vacancy_with_sources(
        vacancies,
        "links-fullest",
        [
            ("jsearch", "https://www.linkedin.com/jobs/view/1", {"job_publisher": "LinkedIn"}),
            ("hh", "https://almaty.hh.kz/vacancy/1", {"description": "полное описание " * 50}),
        ],
    )

    page = await vacancies_service.list_page(db_session, VacancyFilter(), limit=50)
    item = next(row for row in page.items if row.id == vacancy_id)

    assert item.source_slugs == ["hh", "jsearch"]
    assert item.source_url == "https://almaty.hh.kz/vacancy/1"
    assert item.is_seed is False


@pytest.mark.db
async def test_a_seed_only_vacancy_is_marked_and_a_real_row_wins(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """A seed row never becomes the link while a real one exists, however large."""
    seeded = await vacancy_with_sources(
        vacancies, "links-seed", [("telegram", SEED_URL, {"seed": "dev-001"})]
    )
    mixed = await vacancy_with_sources(
        vacancies,
        "links-mixed",
        [
            ("hh", "https://example.test/hh/dev-002", {"seed": "x" * 500}),
            ("remotive", "https://remotive.com/remote-jobs/1", {}),
        ],
    )

    page = await vacancies_service.list_page(db_session, VacancyFilter(), limit=50)
    by_id = {item.id: item for item in page.items}

    assert by_id[seeded].is_seed is True
    assert by_id[mixed].is_seed is False
    assert by_id[mixed].source_url == "https://remotive.com/remote-jobs/1"


@pytest.mark.db
async def test_the_source_filter_keeps_only_that_sources_vacancies(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """«Only hh» and «only arbeitnow» are the questions the filter answers."""
    hh = await vacancy_with_sources(
        vacancies, "links-hh", [("hh", "https://almaty.hh.kz/vacancy/7", {})]
    )
    other = await vacancy_with_sources(
        vacancies, "links-arbeitnow", [("arbeitnow", "https://www.arbeitnow.com/j/7", {})]
    )

    page = await vacancies_service.list_page(
        db_session, VacancyFilter(source=["arbeitnow"]), limit=50
    )
    ids = {item.id for item in page.items}

    assert other in ids
    assert hh not in ids


@pytest.mark.db
async def test_the_card_names_the_publisher_and_the_primary_source(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """«jsearch → LinkedIn» is read off the row's own connector."""
    vacancy_id = await vacancy_with_sources(
        vacancies,
        "links-card",
        [
            ("telegram", SEED_URL.replace("001", "003"), {}),
            (
                "jsearch",
                "https://www.linkedin.com/jobs/view/2",
                {"job_publisher": "LinkedIn", "job_description": "текст " * 40},
            ),
            ("remotive", "https://remotive.com/remote-jobs/2", {}),
        ],
    )

    card = await vacancies_service.card(db_session, vacancy_id)

    assert card is not None
    sources = card.vacancy.sources
    assert [source.source_slug for source in sources] == ["jsearch", "remotive", "telegram"]
    assert [source.is_primary for source in sources] == [True, False, False]
    assert sources[0].publisher == "LinkedIn"
    assert sources[1].publisher is None
    # No connector registers telegram; the row is still shown, as a seed.
    assert sources[2].publisher is None
    assert sources[2].is_seed is True


# ── the queues ────────────────────────────────────────────────────────


@pytest.mark.db
async def test_no_letter_or_cv_is_queued_for_a_seeded_vacancy(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """Its address is example.test: a document for it is a model call for nothing."""
    profile = await profiles.create(make_profile())
    seeded = await vacancy_with_sources(
        vacancies, "queue-seed", [("jsearch", SEED_URL.replace("telegram", "jsearch"), {})]
    )
    real = await vacancy_with_sources(
        vacancies, "queue-real", [("remotive", "https://remotive.com/remote-jobs/3", {})]
    )
    await matches.bulk_upsert(
        [
            make_match(profile.id, seeded, Decimal("95")),
            make_match(profile.id, real, Decimal("90")),
        ]
    )

    letters = await letters_store.queue(db_session, profile_id=profile.id, limit=10)
    documents = await documents_store.candidates(db_session, profile_id=profile.id)

    assert [row.vacancy_id for row in letters] == [real]
    assert seeded not in {row.vacancy_id for row in documents}
    assert real in {row.vacancy_id for row in documents}


# ── hh: which catalogue pages first ───────────────────────────────────


def catalog_row(**value: object) -> SavedState:
    """A stored catalogue plan for the default host."""
    return SavedState(
        key="catalog:almaty.hh.kz",
        value={"resolved_at": "2026-09-08T19:20:10+00:00", **value},
        updated_at="2026-09-08T19:20:10Z",
    )


@pytest.mark.unit
def test_hh_before_its_first_run_says_so() -> None:
    """No stored list is not an empty list."""
    preview = HHSource().preview_search([SearchQuery(keywords=("Python Developer",))], [])

    assert preview.use is SearchUse.CATALOG
    assert preview.terms == []
    assert "первого прогона" in (preview.note or "")


@pytest.mark.unit
def test_hh_reorders_its_stored_pages_under_new_titles() -> None:
    """The stored set, re-ranked offline; the note warns the set itself may change."""
    queries = [SearchQuery(keywords=("Python Developer",), headline="Python Developer")]
    stored = [
        catalog_row(
            families=["backend"],
            headline="Backend Engineer",
            slugs=["go-razrabotchik", "java-developer", "python-developer"],
        )
    ]

    preview = HHSource().preview_search(queries, stored)

    assert preview.terms[0] == "python-developer"
    assert "заново" in (preview.note or "")


@pytest.mark.unit
def test_hh_with_an_unchanged_plan_quotes_its_budget() -> None:
    """Same line, same families: nothing will be re-resolved."""
    queries = [SearchQuery(keywords=("python",), headline="Python Developer")]
    stored = [
        catalog_row(families=["backend"], headline="Python Developer", slugs=["python-developer"]),
        SavedState(key="catalog:almaty.hh.kz:broken", value={}, updated_at="2026-09-08T00:00:00Z"),
    ]

    preview = HHSource().preview_search(queries, stored)

    assert preview.terms == ["python-developer"]
    assert "страниц каталога" in (preview.note or "")


@pytest.mark.unit
def test_hh_skips_a_plan_it_can_no_longer_read() -> None:
    """An old shape in the state table is not a reason to fail the screen."""
    queries = [SearchQuery(keywords=("python",))]
    stored = [
        SavedState(key="catalog:almaty.hh.kz", value={"slugs": 5}, updated_at="2026-09-08T00:00Z")
    ]

    preview = HHSource().preview_search(queries, stored)

    assert preview.terms == []
