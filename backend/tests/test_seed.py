"""The development seed is what the frontend is built and demoed against.

Two things have to hold or it stops being useful. It must contain every state
the dashboard can render — all five buckets, a cross-posted vacancy, a posting
with no salary, a posting with no publication date — because a state absent
from the seed is a state nobody looks at until production. And it must be
idempotent: ``make seed`` gets run twice against the same database all the
time, and a seed that doubles its rows quietly invalidates every count on the
screen.

Every query here is scoped to the rows the seed itself owns (its fingerprints,
its profile name, its vacancies) rather than to whole tables. The seed is meant
to run against a database that already holds real postings, so "the seed wrote
60 vacancies" has to stay a true statement about the seed and not about
whatever else lives in the table.
"""

import importlib.util
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import MatchBucket
from app.db.models import (
    Application,
    CandidateProfile,
    Match,
    ProfileSkill,
    Vacancy,
    VacancySource,
)
from helpers import REPO_ROOT


def _load_seed_module() -> ModuleType:
    """Import ``scripts/seed.py`` by path.

    ``scripts/`` is not a package and is not on sys.path — the script is run as
    ``python scripts/seed.py`` — so loading it by file path keeps the test from
    inventing an import route that only exists under pytest.
    """
    path = REPO_ROOT / "scripts" / "seed.py"
    spec = importlib.util.spec_from_file_location("dev_seed", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed_module = _load_seed_module()

#: Rows the seed's own docstring promises, per table.
#:
#: Two profiles rather than one since the vacancy card gained its middle
#: column: "the candidate has this and *this* CV does not say so" can only be
#: seeded from a second resume, because an earlier CV is the only evidence in
#: this database that is not the CV being scored.
EXPECTED_ROWS: dict[str, int] = {
    "candidate_profile": 2,
    "profile_skill": 24,
    "vacancy": 60,
    "vacancy_source": 65,
    "match": 60,
    "application": 6,
}

SEEDED_VACANCIES = select(Vacancy.id).where(Vacancy.fingerprint.in_(seed_module.FINGERPRINTS))
SEEDED_PROFILES = select(CandidateProfile.id).where(
    CandidateProfile.name == seed_module.PROFILE_NAME
)

#: Counting queries restricted to what the seed wrote. Each one is scoped by
#: something a duplicate row would share — the profile's name, the vacancy it
#: hangs off — never by a primary key, which would hide a duplicate instead of
#: exposing it.
COUNT_QUERIES: dict[str, Any] = {
    "candidate_profile": select(func.count()).select_from(SEEDED_PROFILES.subquery()),
    "profile_skill": select(func.count())
    .select_from(ProfileSkill)
    .where(ProfileSkill.profile_id.in_(SEEDED_PROFILES)),
    "vacancy": select(func.count())
    .select_from(Vacancy)
    .where(Vacancy.fingerprint.in_(seed_module.FINGERPRINTS)),
    "vacancy_source": select(func.count())
    .select_from(VacancySource)
    .where(VacancySource.vacancy_id.in_(SEEDED_VACANCIES)),
    # Scoped by the profile too: a match hung on some other profile is invisible
    # to the dashboard, and counting it by vacancy alone would hide that.
    "match": select(func.count())
    .select_from(Match)
    .where(Match.vacancy_id.in_(SEEDED_VACANCIES), Match.profile_id.in_(SEEDED_PROFILES)),
    "application": select(func.count())
    .select_from(Application)
    .where(Application.vacancy_id.in_(SEEDED_VACANCIES)),
}


async def _counts(session: AsyncSession) -> dict[str, int]:
    """How many rows the seed currently owns in each table it writes."""
    return {table: int(await session.scalar(query) or 0) for table, query in COUNT_QUERIES.items()}


async def test_seed_writes_the_row_counts_it_documents(db_session: AsyncSession) -> None:
    """The docstring's promise is the contract the frontend is built against."""
    await seed_module.seed(db_session)

    assert await _counts(db_session) == EXPECTED_ROWS


async def test_summary_matches_what_landed_in_the_database(db_session: AsyncSession) -> None:
    """A summary that drifts from reality turns the seed's log line into a lie."""
    summary = await seed_module.seed(db_session)

    assert {
        "candidate_profile": summary.profiles,
        "profile_skill": summary.profile_skills,
        "vacancy": summary.vacancies,
        "vacancy_source": summary.vacancy_sources,
        "match": summary.matches,
        "application": summary.applications,
    } == await _counts(db_session)


async def test_every_match_bucket_is_represented(db_session: AsyncSession) -> None:
    """Each bucket is a distinct visual state; a missing one is never reviewed.

    ``filtered`` is the one that matters most: the dashboard hides it by
    default, so nothing else would produce a row for the toggle to reveal.
    """
    await seed_module.seed(db_session)

    buckets = set(
        (
            await db_session.execute(
                select(Match.bucket).distinct().where(Match.vacancy_id.in_(SEEDED_VACANCIES))
            )
        ).scalars()
    )

    assert buckets == set(MatchBucket)


async def test_cross_posted_vacancies_carry_two_sources(db_session: AsyncSession) -> None:
    """One job on two boards must stay one vacancy row, or deduplication regressed."""
    await seed_module.seed(db_session)

    per_vacancy = (
        select(VacancySource.vacancy_id, func.count().label("sources"))
        .where(VacancySource.vacancy_id.in_(SEEDED_VACANCIES))
        .group_by(VacancySource.vacancy_id)
        .subquery()
    )
    with_two_sources = await db_session.scalar(
        select(func.count()).select_from(per_vacancy).where(per_vacancy.c.sources == 2)
    )

    assert with_two_sources == len(seed_module.CROSS_POSTED_INDEXES) == 5


@pytest.mark.parametrize("column", [Vacancy.salary_min, Vacancy.published_at])
async def test_some_postings_leave_optional_fields_empty(
    db_session: AsyncSession, column: Any
) -> None:
    """Empty states and the keyset NULL tail need real NULLs to be exercised at all."""
    await seed_module.seed(db_session)

    empty = await db_session.scalar(
        select(func.count())
        .select_from(Vacancy)
        .where(Vacancy.fingerprint.in_(seed_module.FINGERPRINTS), column.is_(None))
    )

    assert empty is not None
    assert 0 < empty < seed_module.VACANCY_COUNT


async def test_salary_is_normalized_exactly_where_there_is_a_salary(
    db_session: AsyncSession,
) -> None:
    """Sorting by salary reads the normalised column, so it must never be a gap.

    A posting with money but no normalised amount sorts into the NULL tail and
    vanishes from the top of the list; a normalised amount with no advertised
    salary would be an invention.
    """
    await seed_module.seed(db_session)

    rows = (
        await db_session.execute(
            select(Vacancy.salary_min, Vacancy.salary_min_normalized).where(
                Vacancy.fingerprint.in_(seed_module.FINGERPRINTS)
            )
        )
    ).all()

    assert len(rows) == seed_module.VACANCY_COUNT
    assert all((row.salary_min is None) == (row.salary_min_normalized is None) for row in rows)


async def test_normalized_salary_is_comparable_across_currencies(
    db_session: AsyncSession,
) -> None:
    """The whole point of the column: 600000 KZT must not outrank 4000 USD."""
    await seed_module.seed(db_session)

    top_by_currency = dict(
        (
            await db_session.execute(
                select(Vacancy.currency, func.max(Vacancy.salary_min_normalized))
                .where(
                    Vacancy.fingerprint.in_(seed_module.FINGERPRINTS),
                    Vacancy.currency.in_(["KZT", "USD"]),
                )
                .group_by(Vacancy.currency)
            )
        ).all()
    )

    assert top_by_currency["KZT"] < top_by_currency["USD"]


async def test_seeding_twice_leaves_the_same_row_counts(db_session: AsyncSession) -> None:
    """``make seed`` gets rerun constantly; the second run must be a no-op, not a copy."""
    await seed_module.seed(db_session)
    after_first_run = await _counts(db_session)

    await seed_module.seed(db_session)

    assert await _counts(db_session) == after_first_run == EXPECTED_ROWS


async def test_seeding_twice_keeps_the_same_profiles(db_session: AsyncSession) -> None:
    """A third profile would silently re-point every dashboard query.

    "The active profile" is a flag, and the dashboard resolves it by asking for
    the newest active row. A second run that inserted another one would make
    that answer depend on insertion order — every screen would then be drawn for
    a resume nobody uploaded.
    """
    first_summary = await seed_module.seed(db_session)
    before = set((await db_session.execute(SEEDED_PROFILES)).scalars().all())

    second_summary = await seed_module.seed(db_session)

    after = set((await db_session.execute(SEEDED_PROFILES)).scalars().all())

    assert first_summary == second_summary
    assert after == before
    assert len(after) == EXPECTED_ROWS["candidate_profile"]


async def test_exactly_one_seeded_resume_is_active(db_session: AsyncSession) -> None:
    """The older one is kept and must never be the one everything scores against."""
    await seed_module.seed(db_session)

    active = (
        (
            await db_session.execute(
                select(CandidateProfile.id).where(
                    CandidateProfile.id.in_(SEEDED_PROFILES),
                    CandidateProfile.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )

    assert list(active) == [seed_module.SEED_PROFILE_ID]


async def test_the_tracker_fills_every_column_of_the_board(db_session: AsyncSession) -> None:
    """A column with no row in the seed is a layout nobody ever looks at.

    The board has two axes — where an application is in this project's pipeline,
    and what hh has since said about it — and a seed that left either half empty
    would be a seed the frontend cannot be built against. ``sent`` is checked on
    ``sent_at`` rather than on the agent's status for the same reason the board
    is: only the process that did the typing writes that column.
    """
    await seed_module.seed(db_session)

    rows = (
        await db_session.execute(
            select(
                Application.status,
                Application.agent_status,
                Application.sent_at,
                Application.hh_last_state,
            ).where(Application.vacancy_id.in_(SEEDED_VACANCIES))
        )
    ).all()

    assert {row.agent_status for row in rows} >= {"queued", "needs_manual", "sent"}
    assert any(row.sent_at is not None for row in rows)
    assert {row.hh_last_state for row in rows} >= {"RESPONSE", "INVITATION", "DISCARD"}
    # A row typed in by hand: no agent ever touched it, and the board shows it
    # apart from the queue rather than pretending it is waiting to be sent.
    assert any(row.agent_status is None for row in rows)
    assert len({row.status for row in rows}) >= 3
