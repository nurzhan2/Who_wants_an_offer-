"""Scoring the corpus: what gets read, what gets written, and what gets left alone.

The formula is tested in ``test_matching_rules.py`` without a database. This is
the other half — reading the facts out of the right columns, computing
similarity in PostgreSQL, and storing an explanation rather than a bare number.

The seed rule has a test of its own because it is the kind of thing that works
until somebody runs the real thing once: ``scripts/seed.py`` writes 60 ``match``
rows with invented scores, and a real pass that overwrote them would silently
turn demo data into data that looks measured.
"""

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import MatchBucket
from app.db.models import Match, ProfileSkill, VacancySkill
from app.db.repositories.match import MatchRepository
from app.db.repositories.profile import ProfileRepository
from app.db.repositories.vacancy import VacancyRepository
from app.matching.scorer import ProfileNotReadyError, score_corpus
from app.normalize.sync import sync_requirements
from app.schemas.match import MatchCreate
from factories import make_profile, make_upsert_item

pytestmark = pytest.mark.db


async def a_profile(db_session: AsyncSession, **overrides: Any) -> Any:
    """An active profile with backend skills, matching the measured one."""
    profiles = ProfileRepository(db_session)
    created = await profiles.create(
        make_profile(skills=("python", "postgresql", "docker"), **overrides)
    )
    await profiles.activate(created.id)
    await db_session.flush()
    return created


#: A description naming no technology, so that a test about ``key_skills`` is
#: about ``key_skills``. The factory's own default says «Python, FastAPI,
#: PostgreSQL», which ``app.normalize.description`` now reads as requirements —
#: correctly, and irrelevantly to what these tests are asking.
NOTHING_TECHNICAL = "Требуется водитель категории B. График 5/2."


async def a_vacancy(
    db_session: AsyncSession,
    seed: str,
    derived: dict[str, Any] | None,
    description: str = NOTHING_TECHNICAL,
) -> Any:
    """One stored vacancy whose payload the scorer will read."""
    item = make_upsert_item(seed, "hh", description_raw=description)
    raw = dict(item[4])
    if derived is not None:
        raw["_derived"] = derived
    result = await VacancyRepository(db_session).bulk_upsert([(*item[:4], raw)])
    await sync_requirements(db_session, vacancy_ids=result.vacancy_ids)
    await db_session.flush()
    return result.vacancy_ids[0]


async def stored(db_session: AsyncSession, vacancy_id: Any) -> Match | None:
    """The match row written for one vacancy."""
    return (
        await db_session.execute(select(Match).where(Match.vacancy_id == vacancy_id))
    ).scalar_one_or_none()


async def test_a_score_is_stored_with_the_reasons_behind_it(db_session: AsyncSession) -> None:
    """A number with no explanation is useless in all three places that read it.

    The letter generator builds the letter around the overlap, the dashboard
    prints a «почему» column, and a person reads the card before an application
    goes out. None of them can do anything with a bare 62.

    **Before ``UNSTATED_REQUIREMENT``** one of two held was stored as "50.00".
    The divisor now carries one requirement the employer did not write down,
    so the same overlap is 1/3 and is stored as "33.33"; the two lists behind
    it are unchanged.
    """
    profile = await a_profile(db_session)
    vacancy_id = await a_vacancy(
        db_session, "explained", {"key_skills": ["Python", "Kafka"], "work_experience": "moreThan6"}
    )

    outcome = await score_corpus(db_session)

    assert outcome.written == 1
    row = await stored(db_session, vacancy_id)
    assert row is not None
    assert row.profile_id == profile.id
    assert [item["canonical_name"] for item in row.matched_skills] == ["python"]
    assert [item["canonical_name"] for item in row.missing_required] == ["kafka"]
    assert row.component_scores["skill_coverage_required"] == "33.33"
    assert row.verdict is not None


async def test_a_vacancy_without_an_embedding_says_so_rather_than_scoring_zero(
    db_session: AsyncSession,
) -> None:
    """The brief's requirement, asserted where a reader would see it.

    Nothing in this test embeds anything, so every vacancy here is in the state
    128 of the corpus's 643 rows are in. The score has to come from the skills,
    and the card has to say which half of the formula it is missing.
    """
    await a_profile(db_session)
    vacancy_id = await a_vacancy(db_session, "no-vector", {"key_skills": ["Python"]})

    await score_corpus(db_session)

    row = await stored(db_session, vacancy_id)
    assert row is not None
    assert row.score > 0, "a missing vector must not become a silent zero"
    assert row.semantic_score is None
    assert any("семантика не посчитана" in flag for flag in row.red_flags)


async def test_a_vacancy_whose_employer_listed_no_skills_says_that_too(
    db_session: AsyncSession,
) -> None:
    """449 of 643 rows, so the card would otherwise be silently empty."""
    await a_profile(db_session)
    vacancy_id = await a_vacancy(db_session, "no-skills", {"work_experience": "between1And3"})

    outcome = await score_corpus(db_session)

    assert outcome.without_skills == 1
    row = await stored(db_session, vacancy_id)
    assert row is not None
    assert any("не указал ключевые навыки" in flag for flag in row.red_flags)


async def test_a_requirement_read_from_the_text_says_so_on_the_stored_match(
    db_session: AsyncSession,
) -> None:
    """The third state, where a person actually reads it.

    "The employer listed nothing" and "the employer listed these" were the only
    two answers this card could give. A vacancy scored on requirements nobody
    stated is neither, and showing it as the second would put our reading of a
    sentence under the employer's name — on the card seen just before an
    application goes out.
    """
    await a_profile(db_session)
    vacancy_id = await a_vacancy(
        db_session,
        "from-text",
        {"work_experience": "between1And3"},
        description="Требования: Python, PostgreSQL, Kafka.",
    )

    await score_corpus(db_session)

    row = await stored(db_session, vacancy_id)
    assert row is not None
    assert [item["canonical_name"] for item in row.matched_skills] == ["python", "postgresql"]
    assert {item["source"] for item in row.matched_skills} == {"description_text"}
    assert [item["source"] for item in row.missing_required] == ["description_text"]
    assert any("выведены из текста описания" in flag for flag in row.red_flags)
    # Not the flag it would have had before: it does have requirements now.
    assert not any("не указал ключевые навыки" in flag for flag in row.red_flags)


async def test_a_partly_inferred_requirement_list_says_how_much_of_it_is_inferred(
    db_session: AsyncSession,
) -> None:
    """Mixed lists are the common case once descriptions are read at all.

    The count is in the flag rather than a bare "some of this was inferred",
    because "one of six" and "five of six" are different vacancies to trust.
    """
    await a_profile(db_session)
    vacancy_id = await a_vacancy(
        db_session,
        "mixed",
        {"key_skills": ["Python"], "work_experience": "between1And3"},
        description="Также используем Docker и Kafka.",
    )

    await score_corpus(db_session)

    row = await stored(db_session, vacancy_id)
    assert row is not None
    assert any("часть требований выведена из текста описания: 2 из 3" in f for f in row.red_flags)


async def test_a_language_the_candidate_lacks_files_the_vacancy_and_keeps_the_row(
    db_session: AsyncSession,
) -> None:
    """42 of 294 hh postings ask for Kazakh; this profile does not claim it.

    The row is stored rather than dropped. The document hides it in the
    dashboard by default, which is a display decision — deleting the evidence
    would mean nobody could ever ask why a job stopped appearing.
    """
    await a_profile(db_session)
    vacancy_id = await a_vacancy(
        db_session,
        "kazakh",
        {
            "key_skills": ["Python"],
            "language_requirements": ["Казахский — C1 — Продвинутый"],
        },
    )

    outcome = await score_corpus(db_session)

    assert outcome.filtered == 1
    row = await stored(db_session, vacancy_id)
    assert row is not None
    assert row.bucket == MatchBucket.FILTERED
    assert row.verdict is not None
    assert "казахский" in row.verdict


async def test_a_seeded_match_is_never_overwritten_by_a_real_pass(
    db_session: AsyncSession,
) -> None:
    """scripts/seed.py invents 60 of these, and invented data must stay labelled.

    Overwriting them would put real numbers on rows the queue already refuses to
    serve, which is the quiet way demo data becomes data that looks measured.
    """
    profile = await a_profile(db_session)
    item = make_upsert_item("dev-000", "hh")
    result = await VacancyRepository(db_session).bulk_upsert(
        [(*item[:4], {"_derived": {"key_skills": ["Python"]}})]
    )
    seeded = result.vacancy_ids[0]
    await MatchRepository(db_session).bulk_upsert(
        [
            MatchCreate(
                profile_id=profile.id,
                vacancy_id=seeded,
                score=Decimal("92.00"),
                rule_score=Decimal("92.00"),
                bucket=MatchBucket.APPLY_NOW,
            )
        ]
    )
    await db_session.flush()

    outcome = await score_corpus(db_session)

    assert outcome.skipped_seeds == 1
    row = await stored(db_session, seeded)
    assert row is not None
    assert row.score == Decimal("92.00"), "the seeded score was rewritten"


async def test_scoring_twice_updates_the_row_rather_than_duplicating_it(
    db_session: AsyncSession,
) -> None:
    """(profile_id, vacancy_id) is unique, and a rescore is the ordinary case."""
    await a_profile(db_session)
    await a_vacancy(db_session, "rescored", {"key_skills": ["Python"]})

    await score_corpus(db_session)
    await score_corpus(db_session)

    count = (await db_session.execute(select(Match))).scalars().all()
    assert len(count) == 1


async def test_the_candidates_skill_level_reaches_the_score(db_session: AsyncSession) -> None:
    """The level multiplier is read from profile_skill, not assumed.

    Asserted through the whole path rather than on the pure function, because
    the column it comes from is the one thing this layer can get wrong.

    **Before ``UNSTATED_REQUIREMENT``** one requirement held at ``basic`` was
    stored as "70.00" — the multiplier itself. It is now 0.7 over a divisor of
    1 + 1, "35.00". The level still reaches the score in full: held at
    ``strong`` the same row would store "50.00", and 35 is 0.7 of that.
    """
    profile = await a_profile(db_session)
    await db_session.execute(
        ProfileSkill.__table__.update()
        .where(ProfileSkill.profile_id == profile.id, ProfileSkill.canonical_name == "python")
        .values(level="basic")
    )
    vacancy_id = await a_vacancy(db_session, "levelled", {"key_skills": ["Python"]})

    await score_corpus(db_session)

    row = await stored(db_session, vacancy_id)
    assert row is not None
    assert row.component_scores["skill_coverage_required"] == "35.00"


async def test_no_active_profile_is_an_error_with_a_way_out_not_an_empty_result(
    db_session: AsyncSession,
) -> None:
    """«Nothing matched» and «nothing could be compared» must not look alike.

    `wwao match` already set this example while the scorer did not exist: when
    a link is missing, name it rather than returning success and no rows.
    """
    await a_vacancy(db_session, "orphan", {"key_skills": ["Python"]})

    with pytest.raises(ProfileNotReadyError) as raised:
        await score_corpus(db_session)

    assert "профил" in str(raised.value)


async def test_only_the_required_skills_of_the_vacancy_are_read(
    db_session: AsyncSession,
) -> None:
    """A row marked not-required must not silently become a hard requirement.

    **Before ``UNSTATED_REQUIREMENT``** this asserted "100.00": Python alone,
    held. Python alone is now 1 / (1 + 1), "50.00". The number still tells the
    two cases apart — had Kafka been read as required it would be "33.33".
    """
    await a_profile(db_session)
    vacancy_id = await a_vacancy(db_session, "optional", {"key_skills": ["Python", "Kafka"]})
    await db_session.execute(
        VacancySkill.__table__.update()
        .where(VacancySkill.vacancy_id == vacancy_id, VacancySkill.canonical_name == "kafka")
        .values(is_required=False)
    )
    await db_session.flush()

    await score_corpus(db_session)

    row = await stored(db_session, vacancy_id)
    assert row is not None
    assert [item["canonical_name"] for item in row.missing_required] == []
    assert row.component_scores["skill_coverage_required"] == "50.00"
