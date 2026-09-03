"""VacancyRepository: idempotent ingestion, cross-posting, listing and facets.

The repository is the only thing between a re-crawl and a duplicated dashboard,
so most of what is asserted here is about feeding the *same* data in twice: a
connector re-run has to refresh a posting without ever creating a second one,
and without rewriting the moment the posting was first seen.

One environment detail shapes the timestamp tests. ``func.now()`` is the
transaction timestamp, and every test runs inside a single transaction that is
rolled back afterwards, so two upserts in one test write the identical instant.
Where a test needs to watch a timestamp actually move, it backdates the row
first and then asserts the refresh pulled it forward.
"""

from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Row, func, select, text
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Vacancy, VacancySkill, VacancySource
from app.db.repositories import MatchRepository, ProfileRepository, VacancyRepository
from app.db.repositories.vacancy import BulkUpsertResult, UpsertResult
from app.schemas.common import CursorPage
from app.schemas.vacancy import VacancyFilter, VacancyListItem
from factories import (
    fingerprint_for,
    make_match,
    make_profile,
    make_upsert_item,
    make_vacancy,
    published_at,
)

#: Enough postings that a small page size forces several keyset round trips.
BATCH_SIZE = 4
PAGE_SIZE = 2
PAGED_TOTAL = 5


# ── helpers ───────────────────────────────────────────────────────────


async def _upsert_one(
    repository: VacancyRepository,
    seed: str,
    slug: str = "hh",
    **vacancy_kwargs: Any,
) -> UpsertResult:
    """Upsert a single posting built by the factories, keyword plumbing aside."""
    vacancy, source_slug, external_id, url, raw = make_upsert_item(seed, slug, **vacancy_kwargs)
    return await repository.upsert_by_external_id(
        vacancy,
        source_slug=source_slug,
        external_id=external_id,
        url=url,
        raw=raw,
    )


async def _count_vacancies(session: AsyncSession, fingerprint: str) -> int:
    """How many vacancy rows carry that deduplication key."""
    stmt = select(func.count()).select_from(Vacancy).where(Vacancy.fingerprint == fingerprint)
    return int(await session.scalar(stmt) or 0)


async def _count_sources(session: AsyncSession, vacancy_id: UUID) -> int:
    """How many source links point at one vacancy."""
    stmt = (
        select(func.count())
        .select_from(VacancySource)
        .where(VacancySource.vacancy_id == vacancy_id)
    )
    return int(await session.scalar(stmt) or 0)


async def _stored(session: AsyncSession, vacancy_id: UUID) -> Row[Any]:
    """Read the columns straight from the table, bypassing the identity map."""
    stmt = select(
        Vacancy.title,
        Vacancy.salary_min,
        Vacancy.published_at,
        Vacancy.first_seen_at,
        Vacancy.last_seen_at,
        Vacancy.is_active,
    ).where(Vacancy.id == vacancy_id)
    return (await session.execute(stmt)).one()


async def _backdate_seen_timestamps(session: AsyncSession, vacancy_id: UUID) -> None:
    """Push both "seen" timestamps into the past so a refresh becomes visible."""
    await session.execute(
        sa_update(Vacancy)
        .where(Vacancy.id == vacancy_id)
        .values(
            first_seen_at=text("now() - interval '2 days'"),
            last_seen_at=text("now() - interval '2 days'"),
        )
    )


def _item_for(page: CursorPage[VacancyListItem], vacancy_id: UUID) -> VacancyListItem:
    """The single page row for one vacancy, failing loudly if it is not there."""
    found = [item for item in page.items if item.id == vacancy_id]
    assert len(found) == 1, f"expected exactly one row for {vacancy_id}, got {len(found)}"
    return found[0]


class _RefusingSession:
    """Stand-in session that fails on contact, to prove nothing was executed."""

    async def execute(self, *_: Any, **__: Any) -> Any:
        raise AssertionError("an empty batch must not issue a statement")

    async def flush(self, *_: Any, **__: Any) -> Any:
        raise AssertionError("an empty batch must not flush")


# ── writes: idempotency ───────────────────────────────────────────────


async def test_upserting_the_same_posting_twice_leaves_a_single_row(
    vacancies: VacancyRepository, db_session: AsyncSession
) -> None:
    """Re-running a connector must not double the dashboard.

    The whole ingestion design leans on this: the pipeline re-crawls the same
    pages every few hours and never checks first whether it has seen them.
    """
    vacancy, slug, external_id, url, raw = make_upsert_item("idempotent")

    first = await vacancies.upsert_by_external_id(
        vacancy, source_slug=slug, external_id=external_id, url=url, raw=raw
    )
    second = await vacancies.upsert_by_external_id(
        vacancy, source_slug=slug, external_id=external_id, url=url, raw=raw
    )

    assert first.created is True
    assert second.created is False
    assert second.vacancy_id == first.vacancy_id
    assert await _count_vacancies(db_session, vacancy.fingerprint) == 1
    assert await _count_sources(db_session, first.vacancy_id) == 1


async def test_reupsert_refreshes_the_posting_without_moving_first_seen_at(
    vacancies: VacancyRepository, db_session: AsyncSession
) -> None:
    """This one property is what makes re-crawling safe rather than destructive.

    A refreshed posting must pick up its new title, salary and publication date
    and prove it is still alive by bumping ``last_seen_at`` — while keeping the
    id every match and application already references, and keeping the original
    ``first_seen_at`` that "new this week" is computed from.
    """
    seed = "refreshed"
    original, slug, external_id, url, raw = make_upsert_item(seed)
    first = await vacancies.upsert_by_external_id(
        original, source_slug=slug, external_id=external_id, url=url, raw=raw
    )
    await _backdate_seen_timestamps(db_session, first.vacancy_id)
    before = await _stored(db_session, first.vacancy_id)

    changed = make_vacancy(
        seed,
        title="Senior Backend Engineer",
        salary_min=Decimal("900000.00"),
        salary_max=Decimal("1200000.00"),
        published_at=published_at(1),
    )
    second = await vacancies.upsert_by_external_id(
        changed, source_slug=slug, external_id=external_id, url=url, raw=raw
    )
    after = await _stored(db_session, first.vacancy_id)

    assert second.created is False
    assert second.vacancy_id == first.vacancy_id
    assert after.title == "Senior Backend Engineer"
    assert after.salary_min == Decimal("900000.00")
    assert after.published_at == published_at(1)
    assert after.first_seen_at == before.first_seen_at
    assert after.last_seen_at > before.last_seen_at


# ── writes: batches ───────────────────────────────────────────────────


async def test_bulk_upsert_reports_new_then_updated_for_the_same_batch(
    vacancies: VacancyRepository,
) -> None:
    """A pipeline run reports "found N new"; a second run of the same page must not.

    The counts come from ``xmax = 0``, and the ids have to stay put or every
    match written against the first run would point at nothing.
    """
    batch = [make_upsert_item(f"batch-{index}") for index in range(BATCH_SIZE)]

    first = await vacancies.bulk_upsert(batch)
    second = await vacancies.bulk_upsert(batch)

    assert (first.created, first.updated) == (BATCH_SIZE, 0)
    assert (second.created, second.updated) == (0, BATCH_SIZE)
    assert set(second.vacancy_ids) == set(first.vacancy_ids)
    assert len(set(first.vacancy_ids)) == BATCH_SIZE


async def test_bulk_upsert_collapses_a_repeated_source_posting_inside_one_batch(
    vacancies: VacancyRepository, db_session: AsyncSession
) -> None:
    """An overlapping connector page or a retry can hand the same
    (source_slug, external_id) to one batch twice. ON CONFLICT cannot touch a
    row the statement just inserted, so without a second deduplication — on the
    source key, not the fingerprint — PostgreSQL raises CardinalityViolationError
    and the entire batch is lost, not just the duplicate."""
    first = make_vacancy("repeat-a")
    second = make_vacancy("repeat-b", title="Refetched title")

    result = await vacancies.bulk_upsert(
        [
            (first, "hh", "hh-repeat", "https://example.test/hh/repeat", {"page": 1}),
            (second, "hh", "hh-repeat", "https://example.test/hh/repeat", {"page": 2}),
        ]
    )

    assert result.total == 2
    rows = await db_session.execute(
        select(func.count())
        .select_from(VacancySource)
        .where(VacancySource.source_slug == "hh", VacancySource.external_id == "hh-repeat")
    )
    assert rows.scalar_one() == 1


async def test_bulk_upsert_collapses_duplicate_fingerprints_inside_one_batch(
    vacancies: VacancyRepository, db_session: AsyncSession
) -> None:
    """Two sources in one batch can carry the same job, and PostgreSQL will not have it.

    ``ON CONFLICT DO UPDATE`` refuses to touch a row the same statement just
    inserted ("cannot affect row a second time"), so the batch has to be
    deduplicated before it is sent, while still linking both source rows.
    """
    seed = "same-job-two-sources"
    batch = [make_upsert_item(seed, "hh"), make_upsert_item(seed, "jsearch")]

    result = await vacancies.bulk_upsert(batch)

    assert (result.created, result.updated) == (1, 0)
    assert len(set(result.vacancy_ids)) == 1
    assert await _count_vacancies(db_session, fingerprint_for(seed)) == 1
    assert await _count_sources(db_session, result.vacancy_ids[0]) == 2


async def test_empty_write_batches_never_reach_the_database() -> None:
    """A source that returned nothing must cost zero round trips, not an empty INSERT.

    The session used here fails on any contact, so reaching the database at all
    is the failure.
    """
    repository = VacancyRepository(cast("AsyncSession", _RefusingSession()))

    assert await repository.bulk_upsert([]) == BulkUpsertResult(
        created=0, updated=0, vacancy_ids=()
    )
    assert await repository.mark_inactive([]) == 0


# ── cross-posting ─────────────────────────────────────────────────────


async def test_cross_posted_vacancy_is_one_row_listing_both_sources(
    vacancies: VacancyRepository, db_session: AsyncSession
) -> None:
    """The same job on hh and jsearch is one card with two links, not two cards.

    Deduplication happens on the fingerprint, so the second source arriving in
    a later run has to attach to the vacancy the first run created.
    """
    seed = "cross-posted"
    first = await _upsert_one(vacancies, seed, "hh")
    second = await _upsert_one(vacancies, seed, "jsearch")

    page = await vacancies.list_filtered(VacancyFilter())

    assert second.vacancy_id == first.vacancy_id
    assert second.created is False
    assert await _count_vacancies(db_session, fingerprint_for(seed)) == 1
    assert await _count_sources(db_session, first.vacancy_id) == 2
    assert sorted(_item_for(page, first.vacancy_id).source_slugs) == ["hh", "jsearch"]


# ── reads: lookups ────────────────────────────────────────────────────


async def test_get_by_fingerprint_finds_the_posting(vacancies: VacancyRepository) -> None:
    """The deduplicator looks postings up by fingerprint before deciding they are new."""
    seed = "findable"
    upserted = await _upsert_one(vacancies, seed)

    found = await vacancies.get_by_fingerprint(fingerprint_for(seed))

    assert found is not None
    assert found.id == upserted.vacancy_id


@pytest.mark.parametrize(
    "fingerprint",
    [fingerprint_for("never-ingested"), ""],
    ids=["unknown", "empty"],
)
async def test_get_by_fingerprint_returns_none_when_nothing_matches(
    vacancies: VacancyRepository, fingerprint: str
) -> None:
    """A miss is an ordinary answer, not an exception the caller has to catch."""
    assert await vacancies.get_by_fingerprint(fingerprint) is None


async def test_get_returns_none_for_an_unknown_id(vacancies: VacancyRepository) -> None:
    """A deleted vacancy still reachable from a stale link must read as absent."""
    assert await vacancies.get(uuid4()) is None


async def test_get_eager_loads_sources_and_skills(
    vacancies: VacancyRepository, db_session: AsyncSession
) -> None:
    """The vacancy card renders both collections, and a lazy load here would raise.

    Under async SQLAlchemy an unloaded relationship touched outside a greenlet
    is an error, not a slow query — so the eager loading is the contract.
    """
    upserted = await _upsert_one(vacancies, "eager")
    db_session.add(VacancySkill(vacancy_id=upserted.vacancy_id, canonical_name="python"))
    await db_session.flush()
    # Force a real query: anything left in the identity map would hide a
    # missing eager load.
    db_session.expunge_all()

    vacancy = await vacancies.get(upserted.vacancy_id)

    assert vacancy is not None
    assert [source.source_slug for source in vacancy.sources] == ["hh"]
    assert [skill.canonical_name for skill in vacancy.skills] == ["python"]


# ── reads: listing ────────────────────────────────────────────────────


async def test_mark_inactive_hides_the_vacancy_from_the_list(
    vacancies: VacancyRepository, db_session: AsyncSession
) -> None:
    """Retiring a posting a source stopped returning must remove it from the dashboard.

    Rows are retired rather than deleted so applications keep their history,
    which only works if every read filters on ``is_active``.
    """
    retired = await _upsert_one(vacancies, "retired")
    kept = await _upsert_one(vacancies, "kept")

    touched = await vacancies.mark_inactive([retired.vacancy_id])

    page = await vacancies.list_filtered(VacancyFilter())
    listed = {item.id for item in page.items}
    assert touched == 1
    assert retired.vacancy_id not in listed
    assert kept.vacancy_id in listed
    assert (await _stored(db_session, retired.vacancy_id)).is_active is False


async def test_unscored_vacancy_is_listed_with_a_null_score(
    vacancies: VacancyRepository, matches: MatchRepository, profiles: ProfileRepository
) -> None:
    """An unscored vacancy is a state the dashboard shows, not a reason to disappear.

    Scoring runs after ingestion, so between the two phases every fresh posting
    has no match row; an inner join here would empty the dashboard.
    """
    profile = await profiles.create(make_profile())
    scored = await _upsert_one(vacancies, "scored")
    unscored = await _upsert_one(vacancies, "unscored")
    await matches.bulk_upsert([make_match(profile.id, scored.vacancy_id, Decimal("80.00"))])

    page = await vacancies.list_filtered(VacancyFilter(), profile_id=profile.id)

    assert _item_for(page, unscored.vacancy_id).score is None
    assert _item_for(page, unscored.vacancy_id).bucket is None
    assert _item_for(page, scored.vacancy_id).score == Decimal("80.00")


async def test_count_agrees_with_walking_every_page(vacancies: VacancyRepository) -> None:
    """The reported total and the pages must describe the same set of rows.

    Keyset pagination is where a total quietly stops matching: a wrong boundary
    either repeats a row or drops the tail, and only walking to the end shows it.
    """
    await vacancies.bulk_upsert([make_upsert_item(f"page-{index}") for index in range(PAGED_TOTAL)])
    filters = VacancyFilter()

    seen: list[UUID] = []
    cursor: str | None = None
    for _ in range(PAGED_TOTAL + 1):
        page = await vacancies.list_filtered(filters, cursor=cursor, limit=PAGE_SIZE)
        seen.extend(item.id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    else:
        pytest.fail("keyset pagination never reported a last page")

    assert len(seen) == len(set(seen))
    assert len(seen) == await vacancies.count(filters) == PAGED_TOTAL


# ── reads: facets ─────────────────────────────────────────────────────


async def test_facets_count_sources_buckets_and_cities(
    vacancies: VacancyRepository, matches: MatchRepository, profiles: ProfileRepository
) -> None:
    """The sidebar numbers are what a user trusts before clicking a filter.

    All three dimensions come out of one union, so a mistake in any branch
    shows up as a count that does not match the rows actually stored.
    """
    profile = await profiles.create(make_profile())
    almaty_hh = await _upsert_one(vacancies, "facet-almaty-hh", "hh")
    astana_hh = await _upsert_one(vacancies, "facet-astana-hh", "hh", city="Астана")
    almaty_js = await _upsert_one(vacancies, "facet-almaty-js", "jsearch")
    await matches.bulk_upsert(
        [
            make_match(profile.id, almaty_hh.vacancy_id, Decimal("90.00")),
            make_match(profile.id, astana_hh.vacancy_id, Decimal("60.00")),
            make_match(profile.id, almaty_js.vacancy_id, Decimal("90.00")),
        ]
    )

    facets = await vacancies.facets(VacancyFilter(), profile_id=profile.id)

    assert facets.sources == {"hh": 2, "jsearch": 1}
    assert facets.buckets == {"apply_now": 2, "stretch": 1}
    assert facets.cities == {"Алматы": 2, "Астана": 1}


async def test_facets_omit_vacancies_that_have_no_city(vacancies: VacancyRepository) -> None:
    """A remote posting with no city must not invent an empty city bucket.

    ``GROUP BY city`` produces a NULL group, and rendering it would give the
    sidebar a blank, unclickable entry.
    """
    await _upsert_one(vacancies, "has-city")
    await _upsert_one(vacancies, "no-city", city=None)

    facets = await vacancies.facets(VacancyFilter())

    assert facets.cities == {"Алматы": 1}
    assert "" not in facets.cities
    # Still counted everywhere a city is not involved.
    assert facets.sources == {"hh": 2}


async def test_source_facet_counts_vacancies_not_source_rows(
    vacancies: VacancyRepository, db_session: AsyncSession
) -> None:
    """The sidebar number has to match the number of cards clicking it will show.

    One job posted twice on the same board collapses to a single vacancy that
    then carries two ``hh`` source rows. Counting source rows instead of
    distinct vacancies would advertise "hh (2)" and then render one card.
    """
    seed = "same-job-twice-on-hh"
    vacancy = make_vacancy(seed)
    upserted = [
        await vacancies.upsert_by_external_id(
            vacancy,
            source_slug="hh",
            external_id=external_id,
            url=f"https://example.test/hh/{external_id}",
        )
        for external_id in ("hh-first", "hh-second")
    ]
    vacancy_id = upserted[0].vacancy_id

    facets = await vacancies.facets(VacancyFilter())

    assert await _count_vacancies(db_session, fingerprint_for(seed)) == 1
    assert await _count_sources(db_session, vacancy_id) == 2
    assert facets.sources == {"hh": 1}


async def test_facets_respect_the_filter_they_are_given(vacancies: VacancyRepository) -> None:
    """Counts describe the current result set, not the whole table.

    Facets computed over everything would tell the user a filter has matches
    that the filtered list does not show.
    """
    await _upsert_one(vacancies, "filtered-almaty", "hh")
    await _upsert_one(vacancies, "filtered-astana", "jsearch", city="Астана")

    facets = await vacancies.facets(VacancyFilter(city="Астана"))

    assert facets.sources == {"jsearch": 1}
    assert facets.cities == {"Астана": 1}
