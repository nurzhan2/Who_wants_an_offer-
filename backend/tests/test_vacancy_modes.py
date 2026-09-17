"""Ranking modes: one list, four orders, the same rows in every one of them.

A mode picks which number already stored on ``match`` the list is ranked by.
These tests pin the three promises the screen makes about that: the order
follows the chosen number, what is listed does not depend on the mode, and
nothing is computed to answer — least of all by the embedding model.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import MatchBucket
from app.db.models import Vacancy
from app.db.repositories import MatchRepository, ProfileRepository, VacancyRepository
from app.matching import embeddings
from app.schemas.common import MatchMode, SortField
from app.schemas.match import MatchComponentScores
from app.schemas.vacancy import VacancyFilter, VacancyListItem
from app.services import vacancies as vacancies_service
from factories import fingerprint_for, make_match, make_profile, make_upsert_item

MAX_PAGES = 50


@dataclass(frozen=True, slots=True)
class Spec:
    """One scored vacancy: the combined score and the three stored components.

    ``requirements`` is False for a posting that names none, which the scoring
    pass stores as a coverage of 0.00 with both requirement lists empty.
    """

    seed: str
    combined: str
    title: str
    description: str
    skills: str
    requirements: bool = True
    bucket: MatchBucket | None = None


#: Four vacancies whose four orders all differ, plus one with no requirements
#: whose stored coverage (0.00) would otherwise tie with a real miss.
SPECS: tuple[Spec, ...] = (
    Spec("backend", combined="82.00", title="85.00", description="75.00", skills="20.00"),
    Spec("barista", combined="60.00", title="55.00", description="70.00", skills="50.00"),
    Spec("devops", combined="75.00", title="72.00", description="82.00", skills="40.00"),
    Spec("network", combined="70.00", title="74.00", description="61.00", skills="0.00"),
    Spec(
        "silent",
        combined="78.00",
        title="80.00",
        description="72.00",
        skills="0.00",
        requirements=False,
    ),
)


@pytest_asyncio.fixture
async def profile_id(profiles: ProfileRepository) -> UUID:
    """An active profile, because the list service ranks for the active one."""
    profile = await profiles.create(make_profile())
    await profiles.activate(profile.id)
    await profiles.deactivate_others(profile.id)
    return profile.id


async def seed(
    session: AsyncSession,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
    specs: Sequence[Spec],
) -> dict[str, UUID]:
    """Write the vacancies and their matches exactly as a scoring pass would."""
    await vacancies.bulk_upsert([make_upsert_item(spec.seed) for spec in specs])
    rows = (
        await session.execute(
            select(Vacancy.id, Vacancy.fingerprint).where(
                Vacancy.fingerprint.in_([fingerprint_for(spec.seed) for spec in specs])
            )
        )
    ).all()
    by_fingerprint = {row.fingerprint: row.id for row in rows}
    ids = {spec.seed: by_fingerprint[fingerprint_for(spec.seed)] for spec in specs}
    await matches.bulk_upsert(
        [
            make_match(
                profile_id,
                ids[spec.seed],
                Decimal(spec.combined),
                bucket=spec.bucket,
                matched=("python",) if spec.requirements else (),
                missing_required=("kubernetes",) if spec.requirements else (),
                component_scores=MatchComponentScores(
                    title_similarity=Decimal(spec.title),
                    semantic_similarity=Decimal(spec.description),
                    skill_coverage_required=Decimal(spec.skills),
                ),
            )
            for spec in specs
        ]
    )
    return ids


async def ranked(
    vacancies: VacancyRepository, profile_id: UUID, mode: MatchMode, **filters: Any
) -> list[VacancyListItem]:
    """The whole list in one mode, walked page by page."""
    collected: list[VacancyListItem] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        page = await vacancies.list_filtered(
            VacancyFilter(sort=SortField.SCORE, mode=mode, **filters),
            profile_id=profile_id,
            cursor=cursor,
            limit=2,
        )
        collected.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            return collected
    raise AssertionError("cursor never exhausted")


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (MatchMode.COMBINED, ["backend", "silent", "devops", "network", "barista"]),
        (MatchMode.TITLE, ["backend", "silent", "network", "devops", "barista"]),
        (MatchMode.DESCRIPTION, ["devops", "backend", "silent", "barista", "network"]),
        # The posting with no requirements is unmeasured, not a miss: it goes
        # after «network», whose one stated requirement the profile lacks.
        (MatchMode.SKILLS, ["barista", "devops", "backend", "network", "silent"]),
    ],
)
async def test_each_mode_ranks_by_its_own_stored_number(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
    mode: MatchMode,
    expected: list[str],
) -> None:
    """Pages of two walk the list; the order is the mode's, across every boundary."""
    ids = await seed(db_session, vacancies, matches, profile_id, SPECS)
    names = {vacancy_id: name for name, vacancy_id in ids.items()}

    items = await ranked(vacancies, profile_id, mode)

    assert [names[item.id] for item in items] == expected


async def test_every_row_carries_the_mode_number_and_the_combined_one(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
) -> None:
    """The screen shows both, so that a reader sees how far the modes disagree."""
    await seed(db_session, vacancies, matches, profile_id, SPECS)
    by_spec = {spec.title: spec for spec in SPECS}

    for item in await ranked(vacancies, profile_id, MatchMode.TITLE):
        assert item.mode_score is not None
        spec = by_spec[str(item.mode_score)]
        assert item.score == Decimal(spec.combined)

    for item in await ranked(vacancies, profile_id, MatchMode.COMBINED):
        assert item.mode_score == item.score


async def test_a_posting_without_requirements_has_no_skills_number(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
) -> None:
    """Stored as 0.00, read as unmeasured — and a real 0.00 stays a number."""
    ids = await seed(db_session, vacancies, matches, profile_id, SPECS)

    items = {item.id: item for item in await ranked(vacancies, profile_id, MatchMode.SKILLS)}

    assert items[ids["silent"]].mode_score is None
    assert items[ids["network"]].mode_score == Decimal("0.00")


async def test_the_mode_changes_the_order_and_never_the_rows(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
) -> None:
    """Filters read the combined score in every mode, filtered rows stay hidden."""
    specs = (
        *SPECS,
        Spec(
            "kazakh",
            combined="90.00",
            title="95.00",
            description="95.00",
            skills="95.00",
            bucket=MatchBucket.FILTERED,
        ),
    )
    await seed(db_session, vacancies, matches, profile_id, specs)

    listed = {
        mode: {item.id for item in await ranked(vacancies, profile_id, mode, score_min=70)}
        for mode in MatchMode
    }

    assert len(listed[MatchMode.COMBINED]) == 4
    assert all(rows == listed[MatchMode.COMBINED] for rows in listed.values())
    counts = {
        mode: await vacancies.count(VacancyFilter(mode=mode, score_min=70), profile_id=profile_id)
        for mode in MatchMode
    }
    assert set(counts.values()) == {4}


async def test_ranking_in_any_mode_never_touches_the_embedding_model(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mode is a read of stored numbers; opening the page must not encode anything."""
    await seed(db_session, vacancies, matches, profile_id, SPECS)

    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("the list called the embedding model")

    monkeypatch.setattr(embeddings, "get_provider", refuse)
    monkeypatch.setattr(embeddings, "encode_texts", refuse)
    monkeypatch.setattr(embeddings, "encode_profile", refuse)

    for mode in MatchMode:
        page = await vacancies_service.list_page(db_session, VacancyFilter(mode=mode))
        assert len(page.items) == len(SPECS)


async def test_the_endpoint_takes_the_mode_from_the_query_string(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    profile_id: UUID,
    async_client: AsyncClient,
) -> None:
    """``?mode=skills`` reaches the repository, an unknown mode is a 422."""
    ids = await seed(db_session, vacancies, matches, profile_id, SPECS)

    response = await async_client.get("/api/v1/vacancies", params={"mode": "skills"})
    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["id"] == str(ids["barista"])
    assert body["items"][0]["mode_score"] == "50.00"
    assert body["items"][0]["score"] == "60.00"

    wrong = await async_client.get("/api/v1/vacancies", params={"mode": "salary"})
    assert wrong.status_code == 422


def test_the_default_mode_is_the_combined_score() -> None:
    """Nobody who does not ask for a mode sees a different list than before."""
    assert VacancyFilter().mode is MatchMode.COMBINED
