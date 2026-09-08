"""Deriving scoring inputs from what the crawl already stored.

Two layers, tested separately because they fail differently. ``requirements``
is pure and its questions are about text — is «Английский язык» a skill, is
«Язык разметки» one. ``sync`` is about rows, and its questions are about a
second pass: whether re-running duplicates, whether a posting that dropped a
requirement stops asking for it.

The spellings used here are ones measured in the live corpus of 643 hh
postings, not invented ones. Where a count appears in a comment it came from a
query against that corpus.
"""

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Vacancy, VacancySkill
from app.db.repositories.vacancy import VacancyRepository
from app.normalize.requirements import (
    EXPERIENCE_YEARS,
    MAX_NAME,
    dedupe,
    is_language,
    language_requirements,
    min_years,
    readable_fold,
    skill_names,
)
from app.normalize.sync import sync_requirements
from factories import make_upsert_item

# No ``pytestmark``: ``asyncio_mode = "auto"`` runs the async ones, and
# ``conftest.pytest_collection_modifyitems`` marks whatever asks for a session
# as ``db``. Marking the module ``asyncio`` would apply it to the pure tests too.


def derived(**fields: Any) -> dict[str, Any]:
    """A ``_derived`` payload of the shape the hh connector writes."""
    return dict(fields)


# ── the text layer ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        # Measured in the corpus: 14 postings carry the first, one the second.
        "Английский язык",
        "Казахский язык",
        "Русский язык",
        # The rendered form the connector splits off. It has not been seen
        # reaching key_skills, but it is the same field and the same fix.
        "Казахский — B2 — Средне-продвинутый",
        "Английский — C1 — Продвинутый",
        # Case and spacing must not be a way past the check.
        "  английский  ",
        "АНГЛИЙСКИЙ ЯЗЫК",
    ],
)
def test_a_language_requirement_is_not_a_skill(raw: str) -> None:
    """Because a candidate who speaks it would read as missing a skill.

    The failure is quiet and one-directional: the language never appears in
    ``profile_skill`` in this spelling, so the vacancy gains a requirement that
    nobody can satisfy and the candidate loses coverage for a language they
    have. It is the exact thing the connector's split was written to prevent,
    arriving by the other door.
    """
    assert is_language(raw)


@pytest.mark.parametrize(
    "raw",
    [
        # Both of the first two are real skills in this corpus, and both would
        # be swallowed by a lazier check — anything containing "язык".
        "Язык разметки HTML",
        "Языки программирования",
        "Английская литература",
        "Python",
        "Деловая переписка на английском",
    ],
)
def test_a_skill_that_merely_mentions_language_survives(raw: str) -> None:
    """The check matches a named language, not the word for language."""
    assert not is_language(raw)


def test_the_fold_is_case_and_spacing_only() -> None:
    """Nothing is invented, so an unknown skill keeps its own words.

    The dictionary recognises 9 of this corpus's 645 distinct spellings — it is
    a backend dictionary and the corpus is largely sales and construction — so
    the fold is what most rows go through. If it rewrote meaning, most of
    ``vacancy_skill`` would be a rewrite.
    """
    assert readable_fold("  Активные   Продажи ") == "активные продажи"
    assert readable_fold("Активные продажи") == readable_fold("активные  продажи")


def test_a_very_long_skill_still_fits_its_column() -> None:
    """``canonical_name`` is varchar(100), and hh lets employers type freely."""
    assert len(readable_fold("а" * 400)) == MAX_NAME


def test_skills_come_back_canonical_where_the_dictionary_knows_them() -> None:
    """A known spelling is folded onto its canonical name, an unknown one kept."""
    assert skill_names(derived(key_skills=["PostgreSQL", "Активные продажи"])) == [
        "postgresql",
        "активные продажи",
    ]


def test_two_spellings_of_one_skill_do_not_become_two_rows() -> None:
    """``vacancy_skill`` is unique on (vacancy_id, canonical_name).

    A duplicate would not cost one row to a constraint violation; the insert is
    one statement per chunk, so it would cost the whole batch.
    """
    assert skill_names(derived(key_skills=["Python", "python", " PYTHON "])) == ["python"]


def test_languages_are_dropped_on_the_way_to_the_rows() -> None:
    """The whole point, asserted where the database sees it.

    «REST API» comes back as ``rest`` rather than as itself: the dictionary
    lists it as an alias, and a dictionary hit beats the fold. That is the
    behaviour worth having — it is what makes a candidate who wrote «REST»
    match a vacancy that wrote «REST API» — so it is asserted rather than
    worked around.
    """
    names = skill_names(derived(key_skills=["Python", "Английский язык", "REST API"]))
    assert "английский язык" not in names
    assert names == ["python", "rest"]


@pytest.mark.parametrize(
    ("stated", "years"),
    [
        ("noExperience", Decimal("0")),
        ("between1And3", Decimal("1")),
        ("between3And6", Decimal("3")),
        ("moreThan6", Decimal("6")),
    ],
)
def test_experience_is_read_as_the_lower_bound_of_its_band(stated: str, years: Decimal) -> None:
    """«between1And3» is an employer saying one year will do.

    Reading it as three would fail a candidate the vacancy would have accepted,
    and ``docs/MATCHING.md`` scores the gap from this number.
    """
    assert min_years(derived(work_experience=stated)) == years
    assert EXPERIENCE_YEARS[stated] == years


def test_an_unstated_experience_is_not_zero_experience() -> None:
    """None and 0 score differently, and collapsing them is a quiet lie.

    «This employer wants no experience» is a vacancy a junior should see at the
    top. «This employer did not say» is one where the requirement is unknown.
    Mapping the second onto the first makes every silent posting look
    entry-level — and most postings are silent.
    """
    assert min_years(derived()) is None
    assert min_years(derived(work_experience=None)) is None
    assert min_years(derived(work_experience="somethingNew")) is None
    assert min_years(None) is None
    assert min_years(derived(work_experience="noExperience")) == Decimal("0")


def test_a_language_requirement_keeps_its_level() -> None:
    """Parsed from the rendered string, whose third part repeats the second."""
    parsed = language_requirements(
        derived(language_requirements=["Казахский — B2 — Средне-продвинутый", "Английский — C1"])
    )
    assert parsed == [("казахский", "B2"), ("английский", "C1")]


def test_a_payload_that_is_not_what_we_expect_yields_nothing() -> None:
    """A connector change must not take the crawl down with it.

    Everything here is read out of JSONB written under somebody else's schema,
    so every read is defensive on purpose rather than by habit.
    """
    assert skill_names(None) == []
    assert skill_names(derived(key_skills="Python")) == []
    assert skill_names(derived(key_skills=[None, "", "   ", 7, "Python"])) == ["python"]
    assert language_requirements(derived(language_requirements=["Казахский", 3, None])) == []


def test_dedupe_keeps_the_first_spelling_it_saw() -> None:
    """Order matters: it is the order a letter lists the overlap in."""
    assert dedupe(["python", "sql", "python", "rest api"]) == ["python", "sql", "rest api"]


# ── the row layer ────────────────────────────────────────────────────────────


async def store(
    db_session: AsyncSession, seed: str, payload: dict[str, Any] | None, slug: str = "hh"
) -> UUID:
    """One vacancy carrying a ``_derived`` payload, written as the crawl writes it."""
    item = make_upsert_item(seed, slug)
    raw = dict(item[4])
    if payload is not None:
        raw["_derived"] = payload
    result = await VacancyRepository(db_session).bulk_upsert([(*item[:4], raw)])
    return result.vacancy_ids[0]


async def names_of(db_session: AsyncSession, vacancy_id: UUID) -> list[str]:
    """The skill rows stored for one vacancy."""
    rows = await db_session.execute(
        select(VacancySkill.canonical_name).where(VacancySkill.vacancy_id == vacancy_id)
    )
    return sorted(row[0] for row in rows.all())


async def test_a_stored_payload_becomes_rows_scoring_can_read(db_session: AsyncSession) -> None:
    """The end of the gap: 643 vacancies stored, ``vacancy_skill`` empty."""
    vacancy_id = await store(
        db_session,
        "with-skills",
        derived(
            key_skills=["Python", "PostgreSQL", "Английский язык"], work_experience="between1And3"
        ),
    )

    outcome = await sync_requirements(db_session, vacancy_ids=[vacancy_id])

    assert await names_of(db_session, vacancy_id) == ["postgresql", "python"]
    assert outcome.skills_written == 2
    assert outcome.years_written == 1
    stored = await db_session.get(Vacancy, vacancy_id)
    assert stored is not None
    assert stored.min_years == Decimal("1")


async def test_running_it_twice_does_not_double_the_rows(db_session: AsyncSession) -> None:
    """The backfill is re-runnable, which is what makes it usable at all.

    The derivation will change — another spelling recognised as a language, a
    synonym added — and applying that to the corpus must not mean crawling it
    again at four seconds a page.
    """
    vacancy_id = await store(db_session, "twice", derived(key_skills=["Python", "SQL"]))

    await sync_requirements(db_session, vacancy_ids=[vacancy_id])
    await sync_requirements(db_session, vacancy_ids=[vacancy_id])

    assert await names_of(db_session, vacancy_id) == ["python", "sql"]


async def test_a_requirement_the_employer_removed_stops_being_asked_for(
    db_session: AsyncSession,
) -> None:
    """Skills are replaced, not merged.

    hh postings are edited in place and the sitemap's ``lastmod`` moves when it
    happens, so the crawl re-reads them. A merge would accumulate every
    requirement a posting ever had, and a job that changed its mind twice would
    read as wanting everything.
    """
    vacancy_id = await store(db_session, "edited", derived(key_skills=["Python", "Django"]))
    await sync_requirements(db_session, vacancy_ids=[vacancy_id])

    await store(db_session, "edited", derived(key_skills=["Python", "FastAPI"]))
    await sync_requirements(db_session, vacancy_ids=[vacancy_id])

    assert await names_of(db_session, vacancy_id) == ["fastapi", "python"]


async def test_a_cross_posted_job_asks_for_what_both_postings_asked_for(
    db_session: AsyncSession,
) -> None:
    """One vacancy can carry several source rows, and none of them wins arbitrarily."""
    item = make_upsert_item("cross", "hh")
    await VacancyRepository(db_session).bulk_upsert(
        [(*item[:4], {"_derived": derived(key_skills=["Python"])})]
    )
    twin = make_upsert_item("cross", "arbeitnow")
    result = await VacancyRepository(db_session).bulk_upsert(
        [(twin[0], "arbeitnow", *twin[2:4], {"_derived": derived(key_skills=["SQL"])})]
    )

    await sync_requirements(db_session, vacancy_ids=[result.vacancy_ids[0]])

    assert await names_of(db_session, result.vacancy_ids[0]) == ["python", "sql"]


async def test_a_vacancy_with_no_skills_is_counted_not_dropped(db_session: AsyncSession) -> None:
    """Most of the corpus is here: 449 of 643 rows end with no skill row.

    On hh the key-skills field is optional and most employers leave it blank.
    That is a fact about the source rather than a failure, so it is a counter a
    report prints rather than an error — but it has to be visible, because
    otherwise a run that derived nothing and a run that had nothing to derive
    from print the same zero.

    Three different ways to get there, deliberately: no payload at all, a
    payload without the field, and a list that was entirely languages.
    """
    empty = await store(db_session, "no-skills", derived(work_experience="moreThan6"))
    bare = await store(db_session, "no-payload", None)
    only_language = await store(
        db_session, "language-only", derived(key_skills=["Английский язык"])
    )

    outcome = await sync_requirements(db_session, vacancy_ids=[empty, bare, only_language])

    assert outcome.considered == 3
    assert outcome.without_skills == 3
    assert outcome.skills_written == 0
    assert outcome.years_written == 1
    assert await names_of(db_session, only_language) == []


async def test_the_backfill_reaches_everything_stored(db_session: AsyncSession) -> None:
    """``vacancy_ids=None`` means the whole corpus, which is what the script passes."""
    first = await store(db_session, "all-1", derived(key_skills=["Python"]))
    second = await store(db_session, "all-2", derived(key_skills=["SQL"]))

    outcome = await sync_requirements(db_session)

    assert outcome.considered >= 2
    assert await names_of(db_session, first) == ["python"]
    assert await names_of(db_session, second) == ["sql"]
