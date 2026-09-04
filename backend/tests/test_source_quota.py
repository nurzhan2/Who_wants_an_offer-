"""The metered-request ledger, and the two repository reads a crawl depends on.

Three things are defended here, and each of them costs real money or real data
when it breaks.

**Credits are counted when a request is sent, not when one succeeds.** A 500
has already been billed. A counter that only tracks successes walks past the
daily limit during exactly the episode that produces failures, and the reward
is a 429 with no explanation.

**A source is asked "which of these do you already have?" before it pays for
another page.** If that lookup silently answers "none", the early exit never
fires: the run stays green and simply costs more every time, which is the kind
of regression nobody notices until the quota runs out at noon.

**Completeness only improves.** A source that carries headlines must not be
able to overwrite a posting we already hold in full — the description would be
gone, with nothing recording that it had ever been there.
"""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import VacancyCompleteness
from app.db.models import Vacancy
from app.db.repositories.source_quota import SourceQuotaRepository, utc_day
from app.db.repositories.vacancy import VacancyRepository
from factories import make_source, make_upsert_item, make_vacancy

pytestmark = pytest.mark.db

YESTERDAY = date(2026, 9, 4)
TODAY = date(2026, 9, 5)


@pytest.fixture
def quotas(db_session: AsyncSession) -> SourceQuotaRepository:
    """The credit ledger."""
    return SourceQuotaRepository(db_session)


# ── the ledger ────────────────────────────────────────────────────────


async def test_the_first_request_of_the_day_opens_the_row(
    quotas: SourceQuotaRepository,
) -> None:
    """A source that has never run today has spent nothing, and saying so must
    not require a row to exist first."""
    assert await quotas.used_today("jsearch", day=TODAY) == 0
    assert await quotas.spend("jsearch", day=TODAY) == 1
    assert await quotas.used_today("jsearch", day=TODAY) == 1


async def test_spending_accumulates_within_the_day(quotas: SourceQuotaRepository) -> None:
    """The counter is what stands between us and a 429, so it has to add up."""
    for expected in (1, 2, 3):
        assert await quotas.spend("jsearch", day=TODAY) == expected


async def test_yesterday_does_not_count_against_today(
    quotas: SourceQuotaRepository,
) -> None:
    """The vendors reset at midnight UTC, so the ledger keys on the day.

    Sharing one counter across days would leave a source permanently exhausted
    after its first busy afternoon."""
    await quotas.spend("jsearch", day=YESTERDAY, amount=100)

    assert await quotas.used_today("jsearch", day=TODAY) == 0
    assert await quotas.used_today("jsearch", day=YESTERDAY) == 100


async def test_two_sources_have_separate_allowances(quotas: SourceQuotaRepository) -> None:
    """A chatty source must not spend a careful one's budget."""
    await quotas.spend("jsearch", day=TODAY, amount=5)
    await quotas.spend("remotive", day=TODAY)

    assert await quotas.used_today("jsearch", day=TODAY) == 5
    assert await quotas.used_today("remotive", day=TODAY) == 1


async def test_remaining_counts_down_and_stops_at_zero(
    quotas: SourceQuotaRepository,
) -> None:
    """The number the runner reads to decide whether a source may run at all."""
    assert await quotas.remaining("jsearch", 3, day=TODAY) == 3
    await quotas.spend("jsearch", day=TODAY, amount=2)
    assert await quotas.remaining("jsearch", 3, day=TODAY) == 1

    # Overspending is possible — a retry is a real request — and must report
    # zero rather than a negative allowance that reads as credit.
    await quotas.spend("jsearch", day=TODAY, amount=5)
    assert await quotas.remaining("jsearch", 3, day=TODAY) == 0


async def test_an_unmetered_source_has_no_allowance_to_report(
    quotas: SourceQuotaRepository,
) -> None:
    """None means "not metered", which is different from "nothing left"."""
    assert await quotas.remaining("arbeitnow", None, day=TODAY) is None


def test_the_day_is_the_utc_day() -> None:
    """Not the local one. The vendors reset at midnight UTC, and a developer in
    Almaty is five hours ahead of that boundary."""
    late = datetime(2026, 9, 5, 21, 30, tzinfo=UTC)
    assert utc_day(late) == date(2026, 9, 5)


# ── the lookup the early exit depends on ──────────────────────────────


@pytest.fixture
def vacancies(db_session: AsyncSession) -> VacancyRepository:
    """The vacancy repository."""
    return VacancyRepository(db_session)


async def test_known_external_ids_answers_in_both_directions(
    vacancies: VacancyRepository, db_session: AsyncSession
) -> None:
    """Both halves, because the easy way to get this wrong returns an empty set
    every time — the early exit then never fires, the run stays green, and every
    crawl quietly costs more pages than it needs to."""
    await vacancies.bulk_upsert(
        [make_upsert_item("known-1", slug="jsearch"), make_upsert_item("known-2", slug="jsearch")]
    )
    await db_session.flush()

    _, known_1, *_ = make_source("known-1", "jsearch")
    _, known_2, *_ = make_source("known-2", "jsearch")
    found = await vacancies.known_external_ids("jsearch", [known_1, known_2, "never-seen"])

    assert found == {known_1, known_2}
    assert "never-seen" not in found


async def test_known_external_ids_does_not_cross_sources(
    vacancies: VacancyRepository, db_session: AsyncSession
) -> None:
    """The same posting on two boards has two ids, and one source's history is
    not the other's. Crossing them would make a source skip pages of postings it
    has never actually seen."""
    await vacancies.bulk_upsert([make_upsert_item("cross-1", slug="arbeitnow")])
    await db_session.flush()

    _, external_id, *_ = make_source("cross-1", "arbeitnow")
    assert await vacancies.known_external_ids("arbeitnow", [external_id]) == {external_id}
    assert await vacancies.known_external_ids("jsearch", [external_id]) == set()


async def test_known_external_ids_is_empty_for_an_empty_question(
    vacancies: VacancyRepository,
) -> None:
    """Asked nothing, it must not issue a query with an empty IN clause."""
    assert await vacancies.known_external_ids("jsearch", []) == set()


async def test_known_external_ids_chunks_a_long_list(
    vacancies: VacancyRepository, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long run asks about a whole page-set at once, and an unbounded IN
    clause becomes a query with thousands of bind parameters."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "external_id_lookup_batch", 2)
    await vacancies.bulk_upsert(
        [make_upsert_item(f"chunk-{index}", slug="jsearch") for index in range(5)]
    )
    await db_session.flush()

    ids = [make_source(f"chunk-{index}", "jsearch")[1] for index in range(5)]
    assert await vacancies.known_external_ids("jsearch", [*ids, "absent"]) == set(ids)


# ── completeness only improves ────────────────────────────────────────


async def test_a_headline_only_source_cannot_erase_a_full_description(
    vacancies: VacancyRepository, db_session: AsyncSession
) -> None:
    """The failure this rule exists to prevent is silent and irreversible: the
    description is overwritten and nothing records that it was ever there.

    PostgreSQL orders an enum by declaration and vacancy_completeness is
    declared best-first, so the upsert takes LEAST of the two."""
    full = make_vacancy("same-job", completeness=VacancyCompleteness.FULL)
    await vacancies.bulk_upsert([(full, *make_source("same-job", "jsearch"))])
    await db_session.flush()

    stub = make_vacancy(
        "same-job", completeness=VacancyCompleteness.STUB, description_raw="Anons only."
    )
    await vacancies.bulk_upsert([(stub, *make_source("same-job", "job_alerts"))])
    await db_session.flush()

    stored = (
        await db_session.execute(
            select(Vacancy.completeness).where(Vacancy.fingerprint == full.fingerprint)
        )
    ).scalar_one()
    assert stored is VacancyCompleteness.FULL


async def test_a_stub_is_upgraded_when_the_full_posting_arrives(
    vacancies: VacancyRepository, db_session: AsyncSession
) -> None:
    """The other direction has to work, or a posting first seen as a headline
    would stay a headline for ever."""
    stub = make_vacancy("later-full", completeness=VacancyCompleteness.STUB)
    await vacancies.bulk_upsert([(stub, *make_source("later-full", "job_alerts"))])
    await db_session.flush()

    full = make_vacancy("later-full", completeness=VacancyCompleteness.FULL)
    await vacancies.bulk_upsert([(full, *make_source("later-full", "jsearch"))])
    await db_session.flush()

    stored = (
        await db_session.execute(
            select(Vacancy.completeness).where(Vacancy.fingerprint == stub.fingerprint)
        )
    ).scalar_one()
    assert stored is VacancyCompleteness.FULL


async def test_a_cross_posted_job_is_one_vacancy_with_two_sources(
    vacancies: VacancyRepository, db_session: AsyncSession
) -> None:
    """The same job from two publishers shares a fingerprint, so it must produce
    one vacancy row and two provenance rows — not two vacancies competing for
    the same place in the dashboard."""
    result = await vacancies.bulk_upsert(
        [
            (make_vacancy("cross-post"), *make_source("cross-post-a", "jsearch")),
            (make_vacancy("cross-post"), *make_source("cross-post-b", "arbeitnow")),
        ]
    )
    await db_session.flush()

    assert len(result.vacancy_ids) == 1
    assert result.created == 1
