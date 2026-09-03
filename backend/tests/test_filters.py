"""Every field of VacancyFilter, from its validators down to the SQL it produces.

The filter object is the whole contract of the dashboard list: the API layer
does nothing but hand it to the repository. Two classes of bug live here and
nowhere else. A validator that lets an impossible request through turns into an
empty page the user cannot explain, and a WHERE clause that reads the
advertised salary instead of the normalised one ranks 500000 KZT above
4000 USD -- which is the entire reason the normalised columns exist.
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ApplicationStatus, MatchBucket, RemoteType, Seniority
from app.db.models import Application, Vacancy
from app.db.repositories import MatchRepository, ProfileRepository, VacancyRepository
from app.schemas.common import SortDirection, SortField
from app.schemas.vacancy import VacancyFilter
from factories import (
    fingerprint_for,
    make_match,
    make_profile,
    make_upsert_item,
    make_vacancy,
)

#: Source slug used whenever a test does not care which source a posting came from.
DEFAULT_SLUG = "hh"

#: Big enough to hold every fixture set in this module, so paging never
#: interferes with what a filter test is actually asserting.
WHOLE_PAGE = 100


# ── seeding helpers ───────────────────────────────────────────────────


async def seed_vacancies(
    repo: VacancyRepository,
    spec: Mapping[str, dict[str, Any]],
    *,
    slugs: Mapping[str, str] | None = None,
) -> dict[str, UUID]:
    """Upsert one vacancy per entry and return their ids keyed by seed name.

    Tests name their fixtures ("fresh", "stale") and assert on those names, so
    a failure says which posting leaked through instead of printing a UUID.
    """
    await repo.bulk_upsert(
        [
            make_upsert_item(name, (slugs or {}).get(name, DEFAULT_SLUG), **kwargs)
            for name, kwargs in spec.items()
        ]
    )
    ids: dict[str, UUID] = {}
    for name in spec:
        stored = await repo.get_by_fingerprint(fingerprint_for(name))
        assert stored is not None
        ids[name] = stored.id
    return ids


async def seed_matches(
    repo: MatchRepository,
    profile_id: UUID,
    ids: Mapping[str, UUID],
    spec: Mapping[str, dict[str, Any]],
) -> None:
    """Store one scoring result per named vacancy."""
    await repo.bulk_upsert(
        [make_match(profile_id, ids[name], **kwargs) for name, kwargs in spec.items()]
    )


async def set_normalized(session: AsyncSession, vacancy_id: UUID, monthly_usd: Decimal) -> None:
    """Write what the normalisation phase would have written.

    VacancyCreate deliberately has no normalised fields -- a connector cannot
    know the exchange rate -- so a test that needs them writes them directly.
    """
    await session.execute(
        update(Vacancy)
        .where(Vacancy.id == vacancy_id)
        .values(salary_min_normalized=monthly_usd, salary_normalized_at=func.now())
    )


async def listed_seeds(
    repo: VacancyRepository,
    filters: VacancyFilter,
    ids: Mapping[str, UUID],
    *,
    profile_id: UUID | None = None,
) -> list[str]:
    """Seed names the filter returns, in the order the repository returned them."""
    page = await repo.list_filtered(filters, profile_id=profile_id, limit=WHOLE_PAGE)
    by_id = {vacancy_id: name for name, vacancy_id in ids.items()}
    return [by_id[item.id] for item in page.items]


# ── fixtures ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def profile_id(profiles: ProfileRepository) -> UUID:
    """One stored profile; every match in this module hangs off it."""
    return (await profiles.create(make_profile())).id


@pytest_asyncio.fixture
async def scored(
    vacancies: VacancyRepository, matches: MatchRepository, profile_id: UUID
) -> dict[str, UUID]:
    """Three vacancies scored 40 / 70 / 90, one per interesting bucket."""
    ids = await seed_vacancies(vacancies, {"low": {}, "mid": {}, "high": {}})
    await seed_matches(
        matches,
        profile_id,
        ids,
        {"low": {"score": 40}, "mid": {"score": 70}, "high": {"score": 90}},
    )
    return ids


@pytest_asyncio.fixture
async def cross_currency(vacancies: VacancyRepository, db_session: AsyncSession) -> dict[str, UUID]:
    """A headline KZT salary that is worth less than a modest USD one.

    The advertised amounts and the normalised amounts rank these two in
    opposite orders, which is the only seeding that can tell the two columns
    apart: any filter or sort reading ``salary_min`` gets the order backwards.
    """
    ids = await seed_vacancies(
        vacancies,
        {
            "kzt-headline": {
                "salary_min": Decimal("5000000.00"),
                "salary_max": Decimal("7000000.00"),
                "currency": "KZT",
            },
            "usd-modest": {
                "salary_min": Decimal("4000.00"),
                "salary_max": Decimal("6000.00"),
                "currency": "USD",
            },
        },
    )
    await set_normalized(db_session, ids["kzt-headline"], Decimal("1000.00"))
    await set_normalized(db_session, ids["usd-modest"], Decimal("4000.00"))
    return ids


# ── schema validation ─────────────────────────────────────────────────


def test_inverted_score_range_is_rejected() -> None:
    """An inverted range matches nothing; failing loudly beats an empty page."""
    with pytest.raises(ValidationError):
        VacancyFilter(score_min=Decimal("80"), score_max=Decimal("20"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("salary_min", Decimal("-1")),
        ("posted_within_days", 0),
        ("posted_within_days", 366),
        ("missing_skills_max", -1),
        ("missing_skills_max", 51),
        ("score_min", Decimal("-1")),
        ("score_max", Decimal("101")),
    ],
)
def test_out_of_range_bounds_are_rejected(field: str, value: object) -> None:
    """Nonsensical bounds come from a broken client, not from a user intent."""
    with pytest.raises(ValidationError):
        VacancyFilter(**{field: value})


def test_currency_is_upper_cased() -> None:
    """Query strings arrive in whatever case the caller typed; storage is upper."""
    assert VacancyFilter(currency="kzt").currency == "KZT"


def test_unknown_currency_code_is_rejected() -> None:
    """A bogus code would silently match no posting at all."""
    with pytest.raises(ValidationError):
        VacancyFilter(currency="XXY")


def test_country_code_is_upper_cased() -> None:
    """The column stores ISO 3166 upper case; a lower-case filter must still hit."""
    assert VacancyFilter(country="kz").country == "KZ"


def test_language_code_is_lower_cased() -> None:
    """ISO 639 codes are stored lower case, so ingestion has to normalise them."""
    assert make_vacancy(language="RU").language == "ru"


@pytest.mark.parametrize("field", ["sort", "direction"])
def test_sort_options_are_a_closed_vocabulary(field: str) -> None:
    """An unknown sort key would otherwise reach SORT_COLUMNS and raise a KeyError."""
    with pytest.raises(ValidationError):
        VacancyFilter(**{field: "sideways"})


def test_vacancy_create_rejects_an_inverted_salary_range() -> None:
    """A floor above the ceiling is a parser bug in the connector, not data."""
    with pytest.raises(ValidationError):
        make_vacancy(salary_min=Decimal("900000.00"), salary_max=Decimal("100000.00"))


# ── behaviour: the baseline ───────────────────────────────────────────


async def test_empty_filter_returns_every_active_vacancy(vacancies: VacancyRepository) -> None:
    """The unfiltered dashboard must show live postings and hide retired ones."""
    ids = await seed_vacancies(vacancies, {"live-a": {}, "live-b": {}, "retired": {}})
    await vacancies.mark_inactive([ids["retired"]])

    assert set(await listed_seeds(vacancies, VacancyFilter(), ids)) == {"live-a", "live-b"}


# ── behaviour: score and bucket ───────────────────────────────────────


@pytest.mark.parametrize(
    ("score_min", "expected"),
    [
        (Decimal("70"), {"mid", "high"}),
        (Decimal("91"), set()),
    ],
)
async def test_score_min_keeps_scores_at_or_above_the_floor(
    vacancies: VacancyRepository,
    scored: dict[str, UUID],
    profile_id: UUID,
    score_min: Decimal,
    expected: set[str],
) -> None:
    """The floor is inclusive: asking for 70+ must not drop the vacancy scored 70."""
    found = await listed_seeds(
        vacancies, VacancyFilter(score_min=score_min), scored, profile_id=profile_id
    )

    assert set(found) == expected


@pytest.mark.parametrize(
    ("score_max", "expected"),
    [
        (Decimal("70"), {"low", "mid"}),
        (Decimal("39"), set()),
    ],
)
async def test_score_max_keeps_scores_at_or_below_the_ceiling(
    vacancies: VacancyRepository,
    scored: dict[str, UUID],
    profile_id: UUID,
    score_max: Decimal,
    expected: set[str],
) -> None:
    """The ceiling is inclusive too, so the two bounds can meet on one value."""
    found = await listed_seeds(
        vacancies, VacancyFilter(score_max=score_max), scored, profile_id=profile_id
    )

    assert set(found) == expected


async def test_equal_score_bounds_pin_the_list_to_one_score(
    vacancies: VacancyRepository, scored: dict[str, UUID], profile_id: UUID
) -> None:
    """Pinning both ends to one score is a legitimate query, not an off-by-one."""
    found = await listed_seeds(
        vacancies,
        VacancyFilter(score_min=Decimal("70"), score_max=Decimal("70")),
        scored,
        profile_id=profile_id,
    )

    assert found == ["mid"]


@pytest.mark.parametrize(
    ("buckets", "expected"),
    [
        ([MatchBucket.STRONG], {"mid"}),
        ([MatchBucket.APPLY_NOW, MatchBucket.SKIP], {"low", "high"}),
    ],
)
async def test_bucket_filter_accepts_several_buckets_at_once(
    vacancies: VacancyRepository,
    scored: dict[str, UUID],
    profile_id: UUID,
    buckets: list[MatchBucket],
    expected: set[str],
) -> None:
    """The sidebar lets the user tick more than one bucket; it must OR them."""
    found = await listed_seeds(
        vacancies, VacancyFilter(bucket=buckets), scored, profile_id=profile_id
    )

    assert set(found) == expected


@pytest.mark.parametrize(
    ("missing_skills_max", "expected"),
    [
        (0, {"clean"}),
        (3, {"clean", "gappy"}),
    ],
)
async def test_missing_skills_max_caps_the_number_of_gaps(
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
    missing_skills_max: int,
    expected: set[str],
) -> None:
    """This is how a user says "only jobs I can actually apply to today"."""
    ids = await seed_vacancies(vacancies, {"clean": {}, "gappy": {}})
    await seed_matches(
        matches,
        profile_id,
        ids,
        {
            "clean": {"score": 80},
            "gappy": {"score": 80, "missing_required": ("go", "rust", "kotlin")},
        },
    )

    found = await listed_seeds(
        vacancies,
        VacancyFilter(missing_skills_max=missing_skills_max),
        ids,
        profile_id=profile_id,
    )

    assert set(found) == expected


@pytest.mark.parametrize(
    ("include_filtered", "expected"),
    [
        (False, {"kept"}),
        (True, {"kept", "junk"}),
    ],
)
async def test_filtered_bucket_is_hidden_unless_explicitly_requested(
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
    include_filtered: bool,
    expected: set[str],
) -> None:
    """Hard-failed vacancies are noise by default but must stay auditable."""
    ids = await seed_vacancies(vacancies, {"kept": {}, "junk": {}})
    await seed_matches(
        matches,
        profile_id,
        ids,
        {
            "kept": {"score": 80},
            "junk": {"score": 5, "bucket": MatchBucket.FILTERED},
        },
    )

    found = await listed_seeds(
        vacancies,
        VacancyFilter(include_filtered=include_filtered),
        ids,
        profile_id=profile_id,
    )

    assert set(found) == expected


# ── behaviour: the vacancy's own columns ──────────────────────────────


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        (["habr"], {"only-habr"}),
        (["hh"], {"only-hh", "also-hh"}),
        (["habr", "hh"], {"only-habr", "only-hh", "also-hh"}),
    ],
)
async def test_source_filter_matches_any_of_the_listed_slugs(
    vacancies: VacancyRepository, sources: list[str], expected: set[str]
) -> None:
    """A vacancy can be cross-posted, so the source test is existence, not equality."""
    ids = await seed_vacancies(
        vacancies,
        {"only-habr": {}, "only-hh": {}, "also-hh": {}},
        slugs={"only-habr": "habr"},
    )

    found = await listed_seeds(vacancies, VacancyFilter(source=sources), ids)

    assert set(found) == expected


@pytest.mark.parametrize(
    ("wanted", "expected"),
    [
        ([RemoteType.FULL], {"remote"}),
        ([RemoteType.HYBRID, RemoteType.FULL], {"hybrid", "remote"}),
    ],
)
async def test_remote_filter_accepts_several_formats(
    vacancies: VacancyRepository, wanted: list[RemoteType], expected: set[str]
) -> None:
    """ "Remote or hybrid" is the single most common thing a candidate asks for."""
    ids = await seed_vacancies(
        vacancies,
        {
            "onsite": {"remote": RemoteType.NO},
            "hybrid": {"remote": RemoteType.HYBRID},
            "remote": {"remote": RemoteType.FULL},
        },
    )

    found = await listed_seeds(vacancies, VacancyFilter(remote=wanted), ids)

    assert set(found) == expected


@pytest.mark.parametrize(
    ("wanted", "expected"),
    [
        ([Seniority.SENIOR], {"senior"}),
        ([Seniority.MIDDLE, Seniority.SENIOR], {"middle", "senior"}),
    ],
)
async def test_seniority_filter_accepts_several_grades(
    vacancies: VacancyRepository, wanted: list[Seniority], expected: set[str]
) -> None:
    """Candidates straddle two grades, so the filter has to be a set, not a value."""
    ids = await seed_vacancies(
        vacancies,
        {
            "junior": {"seniority": Seniority.JUNIOR},
            "middle": {"seniority": Seniority.MIDDLE},
            "senior": {"seniority": Seniority.SENIOR},
        },
    )

    found = await listed_seeds(vacancies, VacancyFilter(seniority=wanted), ids)

    assert set(found) == expected


async def test_city_matches_the_whole_name_ignoring_case(
    vacancies: VacancyRepository,
) -> None:
    """City comes from a facet the user clicks, so it is exact -- but case-blind."""
    ids = await seed_vacancies(
        vacancies,
        {"north": {"city": "Astana"}, "south": {"city": "Almaty"}},
    )

    assert await listed_seeds(vacancies, VacancyFilter(city="astana"), ids) == ["north"]


async def test_country_filter_selects_one_iso_code(vacancies: VacancyRepository) -> None:
    """Relocation searches are country-wide; the code is normalised on the way in."""
    ids = await seed_vacancies(
        vacancies,
        {"local": {"country": "KZ"}, "abroad": {"country": "GE"}},
    )

    assert await listed_seeds(vacancies, VacancyFilter(country="ge"), ids) == ["abroad"]


async def test_company_matches_a_case_insensitive_substring(
    vacancies: VacancyRepository,
) -> None:
    """Nobody types the legal name; a substring is what the search box gives us."""
    ids = await seed_vacancies(
        vacancies,
        {"marketplace": {"company": "Kolesa Group"}, "bank": {"company": "Halyk Bank"}},
    )

    assert await listed_seeds(vacancies, VacancyFilter(company="kolesa"), ids) == ["marketplace"]


async def test_currency_filter_matches_the_advertised_currency(
    vacancies: VacancyRepository, cross_currency: dict[str, UUID]
) -> None:
    """Independent of salary_min: it selects on what the posting actually offers."""
    found = await listed_seeds(vacancies, VacancyFilter(currency="usd"), cross_currency)

    assert found == ["usd-modest"]


@pytest.mark.parametrize(
    ("has_salary", "expected"),
    [
        (True, {"paid"}),
        (False, {"silent"}),
    ],
)
async def test_has_salary_splits_the_list_in_two(
    vacancies: VacancyRepository, has_salary: bool, expected: set[str]
) -> None:
    """Both answers matter: hiding blank salaries, and hunting only for them."""
    ids = await seed_vacancies(
        vacancies,
        {
            "paid": {},
            "silent": {"salary_min": None, "salary_max": None, "currency": None},
        },
    )

    found = await listed_seeds(vacancies, VacancyFilter(has_salary=has_salary), ids)

    assert set(found) == expected


async def test_posted_within_days_keeps_only_recent_postings(
    vacancies: VacancyRepository,
) -> None:
    """The cutoff is relative to now, and an undated posting cannot prove recency."""
    now = datetime.now(UTC)
    ids = await seed_vacancies(
        vacancies,
        {
            "fresh": {"published_at": now - timedelta(days=2)},
            "stale": {"published_at": now - timedelta(days=40)},
            "undated": {"published_at": None},
        },
    )

    found = await listed_seeds(vacancies, VacancyFilter(posted_within_days=7), ids)

    assert set(found) == {"fresh"}


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("разработчик", "russian"),
        ("kubernetes", "english"),
    ],
)
async def test_full_text_search_works_in_both_languages(
    vacancies: VacancyRepository, query: str, expected: str
) -> None:
    """Postings mix Russian and English, which is why the tsvector config is 'simple'."""
    ids = await seed_vacancies(
        vacancies,
        {
            "russian": {
                "title": "Бэкенд инженер",
                "description_raw": "нужен разработчик на Django и PostgreSQL",
            },
            "english": {
                "title": "Platform Engineer",
                "description_raw": "Kubernetes and Terraform experience required",
            },
        },
    )

    assert await listed_seeds(vacancies, VacancyFilter(q=query), ids) == [expected]


async def test_exclude_applied_hides_vacancies_already_in_the_tracker(
    vacancies: VacancyRepository, db_session: AsyncSession
) -> None:
    """Re-reading a job you already applied to is the fastest way to waste an evening."""
    ids = await seed_vacancies(vacancies, {"applied": {}, "untouched": {}})
    db_session.add(Application(vacancy_id=ids["applied"], status=ApplicationStatus.APPLIED))
    await db_session.flush()

    found = await listed_seeds(vacancies, VacancyFilter(exclude_applied=True), ids)

    assert found == ["untouched"]


# ── behaviour: the normalised salary columns ──────────────────────────


async def test_salary_min_filters_on_the_normalised_amount(
    vacancies: VacancyRepository, cross_currency: dict[str, UUID]
) -> None:
    """A 5 000 000 KZT headline is 1 000 USD; a 2 000 USD floor must exclude it."""
    found = await listed_seeds(vacancies, VacancyFilter(salary_min=Decimal("2000")), cross_currency)

    assert found == ["usd-modest"]


async def test_salary_sort_orders_by_the_normalised_amount(
    vacancies: VacancyRepository, cross_currency: dict[str, UUID]
) -> None:
    """Ordering by the advertised figure would put 5 000 000 KZT above 4 000 USD."""
    found = await listed_seeds(
        vacancies,
        VacancyFilter(sort=SortField.SALARY, direction=SortDirection.ASC),
        cross_currency,
    )

    assert found == ["kzt-headline", "usd-modest"]


# ── behaviour: filters compose ────────────────────────────────────────


async def test_two_filters_intersect_rather_than_union(
    vacancies: VacancyRepository,
) -> None:
    """Every clause is ANDed; an OR here would flood the dashboard silently."""
    ids = await seed_vacancies(
        vacancies,
        {
            "both": {"city": "Astana", "remote": RemoteType.FULL},
            "city-only": {"city": "Astana", "remote": RemoteType.NO},
            "remote-only": {"city": "Almaty", "remote": RemoteType.FULL},
        },
    )

    by_city = set(await listed_seeds(vacancies, VacancyFilter(city="Astana"), ids))
    by_remote = set(await listed_seeds(vacancies, VacancyFilter(remote=[RemoteType.FULL]), ids))
    by_both = set(
        await listed_seeds(vacancies, VacancyFilter(city="Astana", remote=[RemoteType.FULL]), ids)
    )

    assert by_city == {"both", "city-only"}
    assert by_remote == {"both", "remote-only"}
    assert by_both == {"both"}
