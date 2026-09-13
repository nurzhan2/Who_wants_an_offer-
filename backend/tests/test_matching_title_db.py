"""The title formula against PostgreSQL: vectors read, similarity stored, gaps said.

``test_matching_title.py`` tests the arithmetic without a database. This is the
half that can only be wrong in SQL: that the title distance is computed against
the profile's *headline* vector and not its resume vector, that the stored match
carries the title component, and that the selection offering titles to the model
is exact — a row leaves it when its vector is written and comes back when its
title changes.

Vectors come from ``FakeEmbeddingProvider.vector_for``: meaningless geometry, but
the same text gives the same unit vector, so identical texts have cosine 1.
"""

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import MatchBucket
from app.db.models import Match, Vacancy
from app.db.repositories.profile import ProfileRepository
from app.db.repositories.vacancy import EmbeddedVacancy, VacancyRepository
from app.matching.embeddings import FakeEmbeddingProvider
from app.matching.rules import Formula
from app.matching.scorer import score_corpus
from app.normalize.sync import sync_requirements
from app.pipeline.embedding import text_hash
from factories import make_profile, make_upsert_item

pytestmark = pytest.mark.db

SAME = FakeEmbeddingProvider.vector_for("the same text")
OTHER = FakeEmbeddingProvider.vector_for("a different text")


async def a_profile(db_session: AsyncSession, *, headline_vector: list[float] | None) -> Any:
    """An active profile with a resume vector, and a headline vector if given."""
    profiles = ProfileRepository(db_session)
    created = await profiles.create(make_profile())
    await profiles.activate(created.id)
    await profiles.set_embedding(created.id, SAME)
    if headline_vector is not None:
        await profiles.set_headline_embedding(created.id, headline_vector)
    await db_session.flush()
    return created


async def a_vacancy(
    db_session: AsyncSession, seed: str, *, title_vector: list[float] | None
) -> Any:
    """A stored vacancy with a description vector, and a title vector if given."""
    item = make_upsert_item(seed, "hh", description_raw="Требуется водитель категории B.")
    vacancies = VacancyRepository(db_session)
    result = await vacancies.bulk_upsert([item])
    vacancy_id = result.vacancy_ids[0]
    await sync_requirements(db_session, vacancy_ids=result.vacancy_ids)
    await vacancies.set_embeddings([EmbeddedVacancy(id=vacancy_id, vector=SAME, text_hash="x")])
    if title_vector is not None:
        title = (
            await db_session.execute(select(Vacancy.title).where(Vacancy.id == vacancy_id))
        ).scalar_one()
        await vacancies.set_title_embeddings(
            [EmbeddedVacancy(id=vacancy_id, vector=title_vector, text_hash=text_hash(title))]
        )
    await db_session.flush()
    return vacancy_id


async def stored(db_session: AsyncSession, vacancy_id: Any) -> Match:
    """The match row written for one vacancy."""
    db_session.expire_all()
    return (
        await db_session.execute(select(Match).where(Match.vacancy_id == vacancy_id))
    ).scalar_one()


async def test_the_title_is_compared_with_the_headline_and_stored(
    db_session: AsyncSession,
) -> None:
    """Both vectors identical to their counterparts: 100, with the reason named."""
    await a_profile(db_session, headline_vector=SAME)
    vacancy_id = await a_vacancy(db_session, "title-same", title_vector=SAME)

    outcome = await score_corpus(db_session)

    assert outcome.formula is Formula.TITLE
    row = await stored(db_session, vacancy_id)
    assert row.component_scores["title_similarity"] == "100.00"
    assert str(row.score) == "100.00"
    assert row.bucket == MatchBucket.APPLY_NOW
    assert row.verdict is not None
    assert "названию" in row.verdict


async def test_the_headline_vector_is_what_the_title_is_compared_with(
    db_session: AsyncSession,
) -> None:
    """Not the resume vector: the title matches that one and must not score as if it did.

    The resume vector is ``SAME`` and so is the title's; the headline is
    ``OTHER``. Reading the wrong profile column would store 100 here.
    """
    await a_profile(db_session, headline_vector=OTHER)
    vacancy_id = await a_vacancy(db_session, "title-other", title_vector=SAME)

    await score_corpus(db_session)

    row = await stored(db_session, vacancy_id)
    assert row.component_scores["title_similarity"] != "100.00"
    assert row.component_scores["semantic_similarity"] == "100.00"


async def test_a_vacancy_without_a_title_vector_says_so(db_session: AsyncSession) -> None:
    """The window between a crawl and its title vector is real; the card names it."""
    await a_profile(db_session, headline_vector=SAME)
    vacancy_id = await a_vacancy(db_session, "title-missing", title_vector=None)

    outcome = await score_corpus(db_session)

    assert outcome.without_title_embedding == 1
    row = await stored(db_session, vacancy_id)
    assert any("название не сравнено" in flag for flag in row.red_flags)
    # The description still counts; the title's weight stays in the divisor.
    assert str(row.score) == "30.00"


async def test_the_component_formula_is_reproducible_end_to_end(
    db_session: AsyncSession,
) -> None:
    """``--formula components`` writes a score the title does not move."""
    await a_profile(db_session, headline_vector=OTHER)
    vacancy_id = await a_vacancy(db_session, "components", title_vector=SAME)

    outcome = await score_corpus(db_session, formula=Formula.COMPONENTS)

    assert outcome.formula is Formula.COMPONENTS
    row = await stored(db_session, vacancy_id)
    assert row.verdict is not None
    assert "названию" not in row.verdict
    assert not any("название не сравнено" in flag for flag in row.red_flags)


async def test_the_title_selection_is_exact(db_session: AsyncSession) -> None:
    """Offered without a vector, gone once written, back when the title changes."""
    vacancies = VacancyRepository(db_session)
    vacancy_id = await a_vacancy(db_session, "selection", title_vector=None)

    offered = {row.id for row in await vacancies.needs_title_embedding(limit=1000)}
    assert vacancy_id in offered
    before = await vacancies.count_needing_title_embedding()

    title = (
        await db_session.execute(select(Vacancy.title).where(Vacancy.id == vacancy_id))
    ).scalar_one()
    await vacancies.set_title_embeddings(
        [EmbeddedVacancy(id=vacancy_id, vector=SAME, text_hash=text_hash(title))]
    )
    offered = {row.id for row in await vacancies.needs_title_embedding(limit=1000)}
    assert vacancy_id not in offered
    assert await vacancies.count_needing_title_embedding() == before - 1

    await db_session.execute(
        sa_update(Vacancy).where(Vacancy.id == vacancy_id).values(title=f"{title} (renamed)")
    )
    await db_session.flush()
    offered = {row.id for row in await vacancies.needs_title_embedding(limit=1000)}
    assert vacancy_id in offered
