"""Keyset pagination must return every matching row exactly once.

Pagination bugs are invisible on tidy data: with unique, non-NULL sort values
and a page size that happens to divide the row count, a broken cursor still
looks perfect. The datasets here are built to be untidy on purpose — duplicate
sort values across a page boundary, NULL tails, and rows inserted or deleted
mid-walk — because those are the shapes that actually reach production.

Every walk is bounded by ``MAX_PAGES``: a cursor that fails to advance would
otherwise loop forever and hang the whole suite instead of failing one test.
"""

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Vacancy
from app.db.repositories import MatchRepository, ProfileRepository, VacancyRepository
from app.db.repositories.cursor import Cursor, InvalidCursorError
from app.db.repositories.vacancy import MAX_PAGE_SIZE
from app.schemas.common import SortDirection, SortField
from app.schemas.vacancy import VacancyFilter, VacancyListItem
from factories import (
    EPOCH,
    fingerprint_for,
    make_match,
    make_profile,
    make_upsert_item,
    published_at,
)

#: Hard stop for every walk. Generous enough for the largest dataset here
#: (201 rows, smallest page 7) and small enough to fail fast on a stuck cursor.
MAX_PAGES = 120

#: A syntactically valid id to build cursor payloads from, so the decode tests
#: exercise the failure they name and not an unrelated one.
ROW_ID = "0192f3d4-5678-7abc-8def-0123456789ab"


# ── seeding ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Spec:
    """One vacancy, described by the three columns pagination can sort on.

    ``None`` means the column is NULL, which is the interesting case: a score
    of ``None`` is a vacancy with no match row at all.
    """

    seed: str
    score: Decimal | None = None
    salary: Decimal | None = None
    published: datetime | None = EPOCH


@pytest_asyncio.fixture
async def profile_id(profiles: ProfileRepository) -> UUID:
    """The profile every scored vacancy in this module is scored against."""
    return (await profiles.create(make_profile())).id


async def seed(
    session: AsyncSession,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
    specs: Sequence[Spec],
) -> dict[str, UUID]:
    """Insert the specs and return their ids keyed by seed name.

    ``salary_min_normalized`` is written directly because it is produced by the
    normalisation phase, not by ingestion, and is therefore absent from
    ``VacancyCreate``.
    """
    await vacancies.bulk_upsert(
        [make_upsert_item(spec.seed, published_at=spec.published) for spec in specs]
    )

    rows = (
        await session.execute(
            select(Vacancy.id, Vacancy.fingerprint).where(
                Vacancy.fingerprint.in_([fingerprint_for(spec.seed) for spec in specs])
            )
        )
    ).all()
    id_by_fingerprint = {row.fingerprint: row.id for row in rows}
    ids = {spec.seed: id_by_fingerprint[fingerprint_for(spec.seed)] for spec in specs}

    await session.execute(
        sa_update(Vacancy),
        [{"id": ids[spec.seed], "salary_min_normalized": spec.salary} for spec in specs],
    )

    scored = [
        make_match(profile_id, ids[spec.seed], spec.score)
        for spec in specs
        if spec.score is not None
    ]
    if scored:
        await matches.bulk_upsert(scored)
    return ids


# ── walking ───────────────────────────────────────────────────────────


async def walk(
    vacancies: VacancyRepository,
    filters: VacancyFilter,
    *,
    profile_id: UUID,
    limit: int,
    cursor: str | None = None,
) -> list[VacancyListItem]:
    """Follow ``next_cursor`` to the end and return every item seen."""
    collected: list[VacancyListItem] = []
    for _ in range(MAX_PAGES):
        page = await vacancies.list_filtered(
            filters, profile_id=profile_id, cursor=cursor, limit=limit
        )
        collected.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            return collected
    raise AssertionError(f"cursor never exhausted after {MAX_PAGES} pages")


SORT_VALUE = {
    SortField.SCORE: lambda item: item.score,
    SortField.PUBLISHED_AT: lambda item: item.published_at,
    SortField.SALARY: lambda item: item.salary_min_normalized,
}


def assert_ordered(
    items: Sequence[VacancyListItem], sort: SortField, direction: SortDirection
) -> None:
    """Values are monotonic in ``direction`` and every NULL comes after them."""
    values = [SORT_VALUE[sort](item) for item in items]
    first_null = next((i for i, value in enumerate(values) if value is None), len(values))
    assert all(value is None for value in values[first_null:]), "a NULL sorted before a value"

    head = values[:first_null]
    pairs = list(pairwise(head))
    if direction is SortDirection.DESC:
        assert all(left >= right for left, right in pairs), "values are not non-increasing"
    else:
        assert all(left <= right for left, right in pairs), "values are not non-decreasing"


# ── the full walk ─────────────────────────────────────────────────────


async def test_walking_every_page_returns_each_row_exactly_once(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
) -> None:
    """The dashboard scrolls the whole result set; losing a row loses a job offer.

    200 rows in pages of 17 puts a boundary in an awkward place 12 times over,
    and the scores repeat five times each so most boundaries land on a tie.
    """
    specs = [Spec(seed=f"v{i:03d}", score=Decimal(i % 40) * Decimal("2.5")) for i in range(200)]
    ids = await seed(db_session, vacancies, matches, profile_id, specs)

    items = await walk(
        vacancies,
        VacancyFilter(sort=SortField.SCORE, direction=SortDirection.DESC),
        profile_id=profile_id,
        limit=17,
    )

    seen = [item.id for item in items]
    assert len(seen) == 200
    assert len(set(seen)) == 200
    assert set(seen) == set(ids.values())
    assert_ordered(items, SortField.SCORE, SortDirection.DESC)


async def test_rows_sharing_one_score_are_all_returned(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
) -> None:
    """The tiebreaker test: fifty vacancies with an identical score.

    Every page boundary here falls inside a run of equal scores. If the cursor
    comparison were reduced from the composite ``(score, id)`` to a plain
    ``score < cursor.value``, the second page would ask for rows with a
    *strictly smaller* score — of which there are none — and 40 of the 50 rows
    would silently vanish. This test fails the moment that happens.
    """
    specs = [Spec(seed=f"tie{i:02d}", score=Decimal("77.00")) for i in range(50)]
    ids = await seed(db_session, vacancies, matches, profile_id, specs)

    items = await walk(
        vacancies,
        VacancyFilter(sort=SortField.SCORE, direction=SortDirection.DESC),
        profile_id=profile_id,
        limit=10,
    )

    seen = [item.id for item in items]
    assert sorted(seen) == sorted(ids.values())
    assert len(set(seen)) == 50


# ── NULL tails ────────────────────────────────────────────────────────


async def test_vacancies_without_a_normalised_salary_are_still_paginated(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
) -> None:
    """The NULL test for salary: postings in an unpriced currency must still appear.

    ``salary_min_normalized`` is NULL whenever the normaliser has no rate, and
    ``value < NULL`` is NULL — i.e. false. Without the dedicated ``is_null``
    branch in the cursor, the first page that ends inside the NULL tail asks
    for rows "below NULL" and the entire tail disappears from the list.
    """
    specs = [
        Spec(
            seed=f"sal{i:02d}",
            score=Decimal("50.00"),
            salary=None if i % 2 else Decimal(1000 + i * 10),
        )
        for i in range(40)
    ]
    ids = await seed(db_session, vacancies, matches, profile_id, specs)

    items = await walk(
        vacancies,
        VacancyFilter(sort=SortField.SALARY, direction=SortDirection.DESC),
        profile_id=profile_id,
        limit=7,
    )

    seen = [item.id for item in items]
    assert sorted(seen) == sorted(ids.values())
    assert sum(1 for item in items if item.salary_min_normalized is None) == 20
    assert_ordered(items, SortField.SALARY, SortDirection.DESC)


async def test_vacancies_with_no_match_row_are_still_paginated(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
) -> None:
    """The NULL test for score: an unscored vacancy has no match row at all.

    The outer join hands those rows a NULL score, so they form the same NULL
    tail as an unpriced salary does. Without the ``is_null`` branch every
    unscored vacancy — exactly the freshly crawled ones a user most wants to
    see — is dropped from the list the moment a page boundary reaches them.
    """
    specs = [
        Spec(seed=f"sco{i:02d}", score=None if i % 3 == 0 else Decimal(i) * Decimal("2.00"))
        for i in range(30)
    ]
    ids = await seed(db_session, vacancies, matches, profile_id, specs)

    items = await walk(
        vacancies,
        VacancyFilter(sort=SortField.SCORE, direction=SortDirection.DESC),
        profile_id=profile_id,
        limit=6,
    )

    seen = [item.id for item in items]
    assert sorted(seen) == sorted(ids.values())
    assert sum(1 for item in items if item.score is None) == 10
    assert_ordered(items, SortField.SCORE, SortDirection.DESC)


@pytest.mark.parametrize("direction", [SortDirection.DESC, SortDirection.ASC])
@pytest.mark.parametrize("sort", [SortField.SCORE, SortField.PUBLISHED_AT, SortField.SALARY])
async def test_every_sort_field_and_direction_walks_its_null_tail(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
    sort: SortField,
    direction: SortDirection,
) -> None:
    """All three sortable columns are nullable, so each needs the same guarantees.

    One dataset with a NULL tail in every column: whichever column is sorted
    on, the walk must still return each row exactly once, ordered, with the
    NULLs last. Ascending is not symmetric for free — NULLS LAST has to be
    spelled out in both directions or the tail moves to the front.
    """
    specs = [
        Spec(
            seed=f"mix{i:02d}",
            score=None if i % 5 == 0 else Decimal(i) * Decimal("2.00"),
            salary=None if i % 3 == 0 else Decimal(2000 + i * 7),
            published=None if i % 4 == 0 else published_at(i),
        )
        for i in range(40)
    ]
    ids = await seed(db_session, vacancies, matches, profile_id, specs)

    items = await walk(
        vacancies,
        VacancyFilter(sort=sort, direction=direction),
        profile_id=profile_id,
        limit=9,
    )

    seen = [item.id for item in items]
    assert sorted(seen) == sorted(ids.values())
    assert any(SORT_VALUE[sort](item) is None for item in items), "dataset has no NULL tail"
    assert_ordered(items, sort, direction)


async def test_ascending_salary_walks_symmetrically(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
) -> None:
    """ "Cheapest first" is a real dashboard option and must lose nothing either.

    The ascending comparison is a separate branch of the cursor: it uses ``>``
    against the value but still needs NULLS LAST, so the two halves cannot be
    derived from one another by flipping a sign.
    """
    specs = [
        Spec(
            seed=f"asc{i:02d}",
            score=Decimal("60.00"),
            salary=None if i >= 25 else Decimal(3000 + i * 11),
        )
        for i in range(35)
    ]
    ids = await seed(db_session, vacancies, matches, profile_id, specs)

    items = await walk(
        vacancies,
        VacancyFilter(sort=SortField.SALARY, direction=SortDirection.ASC),
        profile_id=profile_id,
        limit=8,
    )

    seen = [item.id for item in items]
    assert sorted(seen) == sorted(ids.values())
    assert sum(1 for item in items if item.salary_min_normalized is None) == 10
    assert_ordered(items, SortField.SALARY, SortDirection.ASC)


# ── the result set changing under the walk ────────────────────────────


async def test_a_row_inserted_mid_walk_neither_repeats_nor_hides_anything(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
) -> None:
    """Crawlers insert while a user is scrolling; that is the whole point of keyset.

    The newcomer deliberately outranks every existing row, so it lands *before*
    the page-1 boundary. That is the only insertion position that distinguishes
    keyset from OFFSET: OFFSET 10 would now start one row too late, handing back
    the last row of page 1 a second time and pushing the true last row off the
    end. A keyset cursor pins the position to a concrete ``(score, id)`` pair,
    so every pre-existing row keeps its place and the newcomer — which sorts
    ahead of the cursor — is correctly not revisited.
    """
    specs = [Spec(seed=f"ins{i:02d}", score=Decimal(i) * Decimal("3.00")) for i in range(30)]
    original = await seed(db_session, vacancies, matches, profile_id, specs)
    filters = VacancyFilter(sort=SortField.SCORE, direction=SortDirection.DESC)

    first = await vacancies.list_filtered(filters, profile_id=profile_id, cursor=None, limit=10)
    assert first.next_cursor is not None

    newcomer = await seed(
        db_session,
        vacancies,
        matches,
        profile_id,
        [Spec(seed="ins-newcomer", score=Decimal("99.00"))],
    )

    rest = await walk(vacancies, filters, profile_id=profile_id, limit=10, cursor=first.next_cursor)

    page_one = [item.id for item in first.items]
    later = [item.id for item in rest]
    assert set(page_one).isdisjoint(later), "a row from page 1 came back on a later page"
    assert newcomer["ins-newcomer"] not in later, "a row ahead of the cursor was served again"
    everything = page_one + later
    for vacancy_id in original.values():
        assert everything.count(vacancy_id) == 1
    assert len(everything) == 30


async def test_deleting_the_row_a_cursor_points_at_does_not_truncate_the_scan(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
) -> None:
    """A posting can be pruned between two page requests — the cursor row included.

    The row the cursor was built from is the sharp case: an implementation that
    resumes by looking the cursor row up again finds nothing and truncates the
    list from that point on. A keyset cursor carries the ``(score, id)`` pair
    itself, so the position survives the row it came from and the remaining
    twenty rows still arrive, in order, exactly once.

    The delete is a real ``DELETE`` relying on ``ON DELETE CASCADE`` for the
    match and source rows, not the softer ``mark_inactive`` path.
    """
    specs = [Spec(seed=f"del{i:02d}", score=Decimal(i) * Decimal("3.00")) for i in range(30)]
    ids = await seed(db_session, vacancies, matches, profile_id, specs)
    filters = VacancyFilter(sort=SortField.SCORE, direction=SortDirection.DESC)

    first = await vacancies.list_filtered(filters, profile_id=profile_id, cursor=None, limit=10)
    assert first.next_cursor is not None

    page_one = [item.id for item in first.items]
    doomed = page_one[-1]  # exactly the row next_cursor was built from
    await db_session.execute(sa_delete(Vacancy).where(Vacancy.id == doomed))
    await db_session.flush()

    rest = await walk(vacancies, filters, profile_id=profile_id, limit=10, cursor=first.next_cursor)

    later = [item.id for item in rest]
    assert doomed not in later, "the deleted cursor row came back"
    assert sorted(later) == sorted(set(ids.values()) - set(page_one)), (
        "the scan lost rows after its cursor row was deleted"
    )
    assert_ordered(rest, SortField.SCORE, SortDirection.DESC)


# ── page size ─────────────────────────────────────────────────────────


async def test_page_size_is_clamped_to_the_maximum(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
) -> None:
    """An unbounded limit is a denial-of-service handed to any caller."""
    specs = [
        Spec(seed=f"cap{i:03d}", score=Decimal(i % 50) * Decimal("2.00"))
        for i in range(MAX_PAGE_SIZE + 1)
    ]
    await seed(db_session, vacancies, matches, profile_id, specs)

    page = await vacancies.list_filtered(VacancyFilter(), profile_id=profile_id, limit=10_000)

    assert len(page.items) == MAX_PAGE_SIZE
    assert page.next_cursor is not None


@pytest.mark.parametrize("limit", [0, -1, -1000])
async def test_page_size_is_clamped_to_at_least_one_row(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
    limit: int,
) -> None:
    """A limit of zero would make the walk advance nowhere and never terminate."""
    specs = [Spec(seed=f"min{i}", score=Decimal(i) * Decimal("10.00")) for i in range(5)]
    await seed(db_session, vacancies, matches, profile_id, specs)

    page = await vacancies.list_filtered(VacancyFilter(), profile_id=profile_id, limit=limit)

    assert len(page.items) == 1
    assert page.next_cursor is not None


# ── the cursor token itself ───────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(Decimal("87.25"), id="decimal"),
        pytest.param(datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC), id="datetime"),
        pytest.param(None, id="null"),
    ],
)
def test_a_cursor_survives_the_round_trip(value: Decimal | datetime | None) -> None:
    """A cursor crosses the network as text; anything lost there mis-positions the scan."""
    row_id = UUID(ROW_ID)

    cursor = Cursor.from_row(row_id=row_id, value=value)

    assert Cursor.decode(cursor.encode()) == cursor


def encode_payload(payload: object) -> str:
    """Encode an arbitrary payload the way a genuine cursor is encoded."""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("*** not base64 ***", id="not-base64"),
        pytest.param(
            base64.urlsafe_b64encode(b"plain text, not json").decode().rstrip("="),
            id="base64-of-non-json",
        ),
        pytest.param(encode_payload([1, 2, 3]), id="json-that-is-not-an-object"),
        pytest.param(encode_payload({"v": "1", "k": "decimal"}), id="missing-id"),
        pytest.param(encode_payload({"id": "nope", "v": "1", "k": "decimal"}), id="invalid-id"),
        pytest.param(encode_payload({"id": ROW_ID, "v": "1", "k": "float"}), id="unknown-kind"),
        pytest.param(encode_payload({"id": ROW_ID, "v": "1"}), id="missing-kind"),
        pytest.param(
            encode_payload({"id": ROW_ID, "v": "twelve", "k": "decimal"}),
            id="malformed-number",
        ),
        pytest.param(
            encode_payload({"id": ROW_ID, "v": "yesterday", "k": "datetime"}),
            id="malformed-timestamp",
        ),
    ],
)
def test_decode_rejects_anything_this_application_did_not_produce(token: str) -> None:
    """A cursor is user input: every failure has to be a client error, not a 500."""
    with pytest.raises(InvalidCursorError):
        Cursor.decode(token)


async def test_listing_with_an_unparsable_cursor_raises_rather_than_guessing(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
) -> None:
    """Falling back to "start from the top" would silently serve the wrong page."""
    specs = [Spec(seed=f"bad{i}", score=Decimal(i) * Decimal("10.00")) for i in range(5)]
    await seed(db_session, vacancies, matches, profile_id, specs)

    with pytest.raises(InvalidCursorError):
        await vacancies.list_filtered(
            VacancyFilter(), profile_id=profile_id, cursor="*** not a cursor ***"
        )
