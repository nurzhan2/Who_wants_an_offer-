"""The ORM layer's promises, checked against a real PostgreSQL.

Everything here is about behaviour the database owns rather than Python: the
FK cascades, the native enum representation, ``Numeric`` precision, the unique
constraints, the generated ``search_vector`` and the loader strategies. Those
cannot be verified against the model definitions alone -- a wrong
``values_callable`` or a missing ``ondelete`` still imports fine and only shows
up as corrupt data later.

Two habits this module sticks to:

* Deletes and counts go through SQL, never through the ORM's in-memory state.
  ``session.delete()`` would cascade in Python and prove nothing about the
  schema; a ``COUNT`` after a Core ``DELETE`` proves the constraint exists.
* A statement that violates a constraint poisons the surrounding transaction,
  so every such insert runs inside ``begin_nested()`` and the test can keep
  querying afterwards.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.base import uuid7
from app.db.enums import (
    ENUM_TYPE_NAMES,
    ApplicationStatus,
    EmploymentType,
    MatchBucket,
    ParseStatus,
    PipelineRunStatus,
    RemoteType,
    SalaryPeriod,
    Seniority,
    SkillEvidence,
    SkillLevel,
)
from app.db.models import (
    Application,
    CandidateProfile,
    Match,
    PipelineRun,
    ProfileSkill,
    Vacancy,
    VacancySkill,
    VacancySource,
)
from factories import fingerprint_for

# ── builders ──────────────────────────────────────────────────────────
#
# factories.py builds Pydantic payloads for the repositories; these tests go
# one layer lower and need ORM instances, so the ORM-shaped defaults live here.


def make_vacancy_row(seed: str = "vacancy-1", **overrides: Any) -> Vacancy:
    """A minimally valid vacancy whose fingerprint follows from its seed."""
    values: dict[str, Any] = {
        "fingerprint": fingerprint_for(seed),
        "title": f"Backend Engineer {seed}",
        "company": "Acme",
        "description_raw": "Python, FastAPI, PostgreSQL.",
    }
    values.update(overrides)
    return Vacancy(**values)


def make_match_row(profile_id: UUID, vacancy_id: UUID, **overrides: Any) -> Match:
    """A scoring result with every non-nullable column filled in."""
    values: dict[str, Any] = {
        "profile_id": profile_id,
        "vacancy_id": vacancy_id,
        "score": Decimal("80.00"),
        "rule_score": Decimal("80.00"),
        "bucket": MatchBucket.STRONG,
    }
    values.update(overrides)
    return Match(**values)


async def persist[T](session: AsyncSession, obj: T) -> T:
    """Add and flush one object so its server-side defaults are available."""
    session.add(obj)
    await session.flush()
    return obj


async def count_rows(session: AsyncSession, model: type[Any]) -> int:
    """Rows of a table as the database sees them, not as the session imagines."""
    total = await session.scalar(select(func.count()).select_from(model))
    assert total is not None
    return total


# ── cascade deletes ───────────────────────────────────────────────────


async def test_deleting_a_profile_cascades_to_its_skills_and_matches(
    db_session: AsyncSession,
) -> None:
    """A stale skill or score for a profile that no longer exists is unjoinable garbage."""
    profile = await persist(db_session, CandidateProfile(name="Nurzhan"))
    db_session.add_all(
        [
            ProfileSkill(profile_id=profile.id, canonical_name="python"),
            ProfileSkill(profile_id=profile.id, canonical_name="fastapi"),
        ]
    )
    vacancy = await persist(db_session, make_vacancy_row())
    await persist(db_session, make_match_row(profile.id, vacancy.id))

    await db_session.execute(delete(CandidateProfile).where(CandidateProfile.id == profile.id))

    assert await count_rows(db_session, ProfileSkill) == 0
    assert await count_rows(db_session, Match) == 0
    # The vacancy is a peer, not a child: it must survive.
    assert await count_rows(db_session, Vacancy) == 1


async def test_deleting_a_vacancy_cascades_to_every_child_table(
    db_session: AsyncSession,
) -> None:
    """A vacancy is deleted when it expires; nothing hanging off it may outlive it."""
    profile = await persist(db_session, CandidateProfile(name="Nurzhan"))
    vacancy = await persist(db_session, make_vacancy_row())
    db_session.add_all(
        [
            VacancySource(
                vacancy_id=vacancy.id,
                source_slug="hh",
                external_id="hh-1",
                url="https://example.test/hh/1",
            ),
            VacancySkill(vacancy_id=vacancy.id, canonical_name="python"),
            make_match_row(profile.id, vacancy.id),
            Application(vacancy_id=vacancy.id, status=ApplicationStatus.APPLIED),
        ]
    )
    await db_session.flush()

    await db_session.execute(delete(Vacancy).where(Vacancy.id == vacancy.id))

    assert await count_rows(db_session, VacancySource) == 0
    assert await count_rows(db_session, VacancySkill) == 0
    assert await count_rows(db_session, Match) == 0
    assert await count_rows(db_session, Application) == 0
    # The profile is a peer of the vacancy, not a child.
    assert await count_rows(db_session, CandidateProfile) == 1


# ── native enums ──────────────────────────────────────────────────────

#: Table and column carrying each native enum type, keyed by its PostgreSQL name.
ENUM_LOCATIONS: dict[str, tuple[str, str]] = {
    "skill_level": ("profile_skill", "level"),
    "seniority": ("candidate_profile", "seniority"),
    "remote_type": ("vacancy", "remote"),
    "employment_type": ("vacancy", "employment_type"),
    "salary_period": ("vacancy", "period"),
    "match_bucket": ("match", "bucket"),
    "application_status": ("application", "status"),
    "pipeline_run_status": ("pipeline_run", "status"),
    "parse_status": ("candidate_profile", "parse_status"),
    "skill_evidence": ("profile_skill", "evidence"),
}

ENUM_CASES = [
    pytest.param(type_name, member, id=f"{type_name}-{member.value}")
    for type_name, enum_cls in ENUM_TYPE_NAMES.items()
    for member in enum_cls
]


async def insert_row_carrying(session: AsyncSession, member: StrEnum) -> UUID:
    """Store one enum member in the column that owns its type; return the row id."""
    if isinstance(member, SkillLevel):
        profile = await persist(session, CandidateProfile())
        row: Any = ProfileSkill(profile_id=profile.id, canonical_name="python", level=member)
    elif isinstance(member, SkillEvidence):
        profile = await persist(session, CandidateProfile())
        row = ProfileSkill(profile_id=profile.id, canonical_name="python", evidence=member)
    elif isinstance(member, Seniority):
        row = CandidateProfile(seniority=member)
    elif isinstance(member, ParseStatus):
        row = CandidateProfile(parse_status=member)
    elif isinstance(member, RemoteType):
        row = make_vacancy_row(remote=member)
    elif isinstance(member, EmploymentType):
        row = make_vacancy_row(employment_type=member)
    elif isinstance(member, SalaryPeriod):
        row = make_vacancy_row(period=member)
    elif isinstance(member, MatchBucket):
        profile = await persist(session, CandidateProfile())
        vacancy = await persist(session, make_vacancy_row())
        row = make_match_row(profile.id, vacancy.id, bucket=member)
    elif isinstance(member, ApplicationStatus):
        vacancy = await persist(session, make_vacancy_row())
        row = Application(vacancy_id=vacancy.id, status=member)
    elif isinstance(member, PipelineRunStatus):
        row = PipelineRun(source_slug="hh", status=member)
    else:  # pragma: no cover - a new enum without a home here
        raise AssertionError(f"no column mapped for {type(member).__name__}")
    await persist(session, row)
    row_id: UUID = row.id
    return row_id


@pytest.mark.parametrize(("type_name", "member"), ENUM_CASES)
async def test_enum_member_is_stored_as_its_lowercase_value(
    db_session: AsyncSession, type_name: str, member: StrEnum
) -> None:
    """Without values_callable PostgreSQL would hold 'APPLY_NOW'; the API speaks 'apply_now'."""
    table, column = ENUM_LOCATIONS[type_name]
    row_id = await insert_row_carrying(db_session, member)

    stored = await db_session.scalar(
        text(f"SELECT CAST({column} AS text) FROM {table} WHERE id = :id"),
        {"id": row_id},
    )

    assert stored == member.value
    assert stored != member.name


@pytest.mark.parametrize(("type_name", "member"), ENUM_CASES)
async def test_enum_member_round_trips_back_into_the_python_member(
    db_session: AsyncSession, type_name: str, member: StrEnum
) -> None:
    """Reading has to give back the enum, not the bare string it is stored as."""
    table, _ = ENUM_LOCATIONS[type_name]
    model = {
        "profile_skill": ProfileSkill,
        "candidate_profile": CandidateProfile,
        "vacancy": Vacancy,
        "match": Match,
        "application": Application,
        "pipeline_run": PipelineRun,
    }[table]
    row_id = await insert_row_carrying(db_session, member)
    db_session.expunge_all()

    reloaded = await db_session.get(model, row_id)

    assert reloaded is not None
    _, column = ENUM_LOCATIONS[type_name]
    value = getattr(reloaded, column)
    assert value is member
    assert isinstance(value, type(member))


# ── numeric precision ─────────────────────────────────────────────────


async def test_money_survives_the_round_trip_exactly(db_session: AsyncSession) -> None:
    """A salary that came back as a float would drift on every currency conversion."""
    vacancy = await persist(
        db_session, make_vacancy_row(salary_min=Decimal("123456.78"), currency="KZT")
    )
    db_session.expunge_all()

    reloaded = await db_session.get(Vacancy, vacancy.id)

    assert reloaded is not None
    assert reloaded.salary_min == Decimal("123456.78")
    assert isinstance(reloaded.salary_min, Decimal)
    assert not isinstance(reloaded.salary_min, float)


async def test_score_survives_the_round_trip_exactly(db_session: AsyncSession) -> None:
    """Bucket boundaries are compared exactly, so 87.65 must not become 87.6500000001."""
    profile = await persist(db_session, CandidateProfile())
    vacancy = await persist(db_session, make_vacancy_row())
    match = await persist(
        db_session,
        make_match_row(profile.id, vacancy.id, score=Decimal("87.65"), rule_score=Decimal("87.65")),
    )
    db_session.expunge_all()

    reloaded = await db_session.get(Match, match.id)

    assert reloaded is not None
    assert reloaded.score == Decimal("87.65")
    assert isinstance(reloaded.score, Decimal)
    assert not isinstance(reloaded.score, float)


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        (Decimal("123456.784"), Decimal("123456.78")),
        (Decimal("123456.785"), Decimal("123456.79")),
        (Decimal("999999999.999"), Decimal("1000000000.00")),
    ],
)
async def test_money_beyond_the_declared_scale_is_rounded_not_truncated(
    db_session: AsyncSession, written: Decimal, expected: Decimal
) -> None:
    """Losing the fractional tail is acceptable; losing a digit of the amount is not."""
    vacancy = await persist(db_session, make_vacancy_row(salary_min=written))
    db_session.expunge_all()

    reloaded = await db_session.get(Vacancy, vacancy.id)

    assert reloaded is not None
    assert reloaded.salary_min == expected


async def test_score_beyond_the_declared_scale_is_rounded_not_truncated(
    db_session: AsyncSession,
) -> None:
    """A score silently truncated to its integer part would land in the wrong bucket."""
    profile = await persist(db_session, CandidateProfile())
    vacancy = await persist(db_session, make_vacancy_row())
    match = await persist(
        db_session,
        make_match_row(
            profile.id, vacancy.id, score=Decimal("87.6789"), rule_score=Decimal("87.6789")
        ),
    )
    db_session.expunge_all()

    reloaded = await db_session.get(Match, match.id)

    assert reloaded is not None
    assert reloaded.score == Decimal("87.68")


# ── unique constraints ────────────────────────────────────────────────


async def test_duplicate_vacancy_fingerprint_is_rejected(db_session: AsyncSession) -> None:
    """The fingerprint is the deduplication key; a second row for it defeats the point."""
    await persist(db_session, make_vacancy_row("same-job"))

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(make_vacancy_row("same-job", title="Same job, other wording"))
            await db_session.flush()

    assert await count_rows(db_session, Vacancy) == 1


async def test_duplicate_source_slug_and_external_id_is_rejected(
    db_session: AsyncSession,
) -> None:
    """One posting on one source is one row, even when dedup assigned it elsewhere."""
    first = await persist(db_session, make_vacancy_row("job-1"))
    second = await persist(db_session, make_vacancy_row("job-2"))
    await persist(
        db_session,
        VacancySource(
            vacancy_id=first.id,
            source_slug="hh",
            external_id="12345",
            url="https://example.test/hh/12345",
        ),
    )

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                VacancySource(
                    vacancy_id=second.id,
                    source_slug="hh",
                    external_id="12345",
                    url="https://example.test/hh/12345",
                )
            )
            await db_session.flush()

    assert await count_rows(db_session, VacancySource) == 1


async def test_duplicate_profile_and_vacancy_match_is_rejected(
    db_session: AsyncSession,
) -> None:
    """Re-scoring must update the one row, never stack a second verdict beside it."""
    profile = await persist(db_session, CandidateProfile())
    vacancy = await persist(db_session, make_vacancy_row())
    await persist(db_session, make_match_row(profile.id, vacancy.id))

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(make_match_row(profile.id, vacancy.id, score=Decimal("10.00")))
            await db_session.flush()

    assert await count_rows(db_session, Match) == 1


async def test_duplicate_profile_skill_is_rejected(db_session: AsyncSession) -> None:
    """A skill counted twice inflates coverage and silently skews every score."""
    profile = await persist(db_session, CandidateProfile())
    await persist(db_session, ProfileSkill(profile_id=profile.id, canonical_name="python"))

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                ProfileSkill(
                    profile_id=profile.id, canonical_name="python", level=SkillLevel.EXPERT
                )
            )
            await db_session.flush()

    assert await count_rows(db_session, ProfileSkill) == 1


async def test_duplicate_vacancy_skill_is_rejected(db_session: AsyncSession) -> None:
    """The same requirement twice would double its weight in the rule-based score."""
    vacancy = await persist(db_session, make_vacancy_row())
    await persist(db_session, VacancySkill(vacancy_id=vacancy.id, canonical_name="python"))

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                VacancySkill(vacancy_id=vacancy.id, canonical_name="python", is_required=False)
            )
            await db_session.flush()

    assert await count_rows(db_session, VacancySkill) == 1


# ── loader strategies ─────────────────────────────────────────────────


@pytest.mark.parametrize("attribute", ["matches", "applications"])
async def test_guarded_collections_refuse_to_load_implicitly(
    db_session: AsyncSession, attribute: str
) -> None:
    """lazy='raise' is the N+1 guard: a list page must never emit a query per row."""
    profile = await persist(db_session, CandidateProfile())
    vacancy = await persist(db_session, make_vacancy_row())
    db_session.add_all(
        [
            make_match_row(profile.id, vacancy.id),
            Application(vacancy_id=vacancy.id),
        ]
    )
    await db_session.flush()
    db_session.expunge_all()

    reloaded = await db_session.get(Vacancy, vacancy.id)

    assert reloaded is not None
    with pytest.raises(InvalidRequestError, match=r"is not available due to lazy='raise'"):
        getattr(reloaded, attribute)


async def test_vacancy_sources_and_skills_load_without_an_explicit_option(
    db_session: AsyncSession,
) -> None:
    """Detail views always need both, so selectin spares every caller an options() dance."""
    vacancy = await persist(db_session, make_vacancy_row())
    db_session.add_all(
        [
            VacancySource(
                vacancy_id=vacancy.id,
                source_slug="hh",
                external_id="hh-1",
                url="https://example.test/hh/1",
            ),
            VacancySkill(vacancy_id=vacancy.id, canonical_name="python"),
        ]
    )
    await db_session.flush()
    db_session.expunge_all()

    reloaded = await db_session.get(Vacancy, vacancy.id)

    assert reloaded is not None
    assert [source.external_id for source in reloaded.sources] == ["hh-1"]
    assert [skill.canonical_name for skill in reloaded.skills] == ["python"]


async def test_profile_skills_load_without_an_explicit_option(
    db_session: AsyncSession,
) -> None:
    """A profile without its skills cannot be scored, so it is never worth a second query."""
    profile = await persist(db_session, CandidateProfile(name="Nurzhan"))
    db_session.add_all(
        [
            ProfileSkill(profile_id=profile.id, canonical_name="python"),
            ProfileSkill(profile_id=profile.id, canonical_name="fastapi"),
        ]
    )
    await db_session.flush()
    db_session.expunge_all()

    reloaded = await db_session.get(CandidateProfile, profile.id)

    assert reloaded is not None
    assert {skill.canonical_name for skill in reloaded.skills} == {"python", "fastapi"}


# ── generated full-text search column ─────────────────────────────────

SEARCH_SQL = text(
    "SELECT count(*) FROM vacancy "
    "WHERE id = :id AND search_vector @@ plainto_tsquery('simple', :query)"
)


async def search_hits(session: AsyncSession, vacancy_id: UUID, query: str) -> int:
    """How many times the generated column answers `query` for one vacancy."""
    hits = await session.scalar(SEARCH_SQL, {"id": vacancy_id, "query": query})
    assert hits is not None
    return hits


@pytest.mark.parametrize(
    ("query", "source_column"),
    [
        ("Kubernetes", "title"),
        ("kubernetes", "title (case folded)"),
        ("Acme", "company"),
        ("разработчик", "description"),
        ("PostgreSQL", "description"),
    ],
)
async def test_search_vector_is_generated_from_title_company_and_description(
    db_session: AsyncSession, query: str, source_column: str
) -> None:
    """Nothing writes search_vector by hand, so a broken expression is invisible until search is."""
    vacancy = await persist(
        db_session,
        make_vacancy_row(
            title="Kubernetes Engineer",
            company="Acme",
            description_raw="Нужен разработчик. PostgreSQL, Kubernetes.",
        ),
    )

    assert await search_hits(db_session, vacancy.id, query) == 1, source_column


@pytest.mark.parametrize(
    ("indexed_form", "inflected_form"),
    [("разработчик", "разработчика"), ("Engineer", "engineers")],
)
async def test_simple_configuration_does_not_stem_either_language(
    db_session: AsyncSession, indexed_form: str, inflected_form: str
) -> None:
    """'simple' stores words verbatim, so callers must not expect an inflected query to match.

    The positive assertion is the control. Without it a ``search_vector`` that
    indexed nothing at all would also return zero hits, and this test would go
    on passing while search was completely dead.
    """
    vacancy = await persist(
        db_session,
        make_vacancy_row(
            title="Kubernetes Engineer",
            description_raw="Нужен разработчик.",
        ),
    )

    assert await search_hits(db_session, vacancy.id, indexed_form) == 1
    assert await search_hits(db_session, vacancy.id, inflected_form) == 0


# ── primary keys and timestamps ───────────────────────────────────────


async def test_generated_ids_sort_in_creation_order(db_session: AsyncSession) -> None:
    """Time-ordered ids keep inserts local in the B-tree and double as a pagination tiebreaker."""
    created = [await persist(db_session, make_vacancy_row(f"job-{index}")) for index in range(25)]

    ordered = (await db_session.scalars(select(Vacancy.id).order_by(Vacancy.id))).all()

    assert list(ordered) == [vacancy.id for vacancy in created]


def test_uuid7_values_increase_monotonically() -> None:
    """The ordering guarantee has to hold inside a single millisecond too."""
    generated = [uuid7() for _ in range(1000)]

    assert generated == sorted(generated)


async def test_timestamps_are_timezone_aware(db_session: AsyncSession) -> None:
    """A naive timestamp is read back in whatever zone the reader assumes; that is a bug factory."""
    vacancy = await persist(db_session, make_vacancy_row())
    db_session.expunge_all()

    reloaded = await db_session.get(Vacancy, vacancy.id)

    assert reloaded is not None
    assert reloaded.created_at.tzinfo is not None
    assert reloaded.updated_at.tzinfo is not None
    assert reloaded.created_at.utcoffset() is not None


async def test_updated_at_advances_on_update(async_engine: AsyncEngine) -> None:
    """`updated_at` drives cache invalidation and "changed since" views.

    This one cannot use the rolled-back ``db_session``: ``func.now()`` is the
    *transaction* timestamp, so an UPDATE issued inside the same transaction as
    the INSERT would rewrite the column with the identical value. Two committed
    transactions are the only way to observe the clock moving, hence the
    explicit cleanup.
    """
    vacancy_id: UUID | None = None
    try:
        async with AsyncSession(async_engine, expire_on_commit=False) as session:
            vacancy = make_vacancy_row("updated-at-probe")
            session.add(vacancy)
            await session.commit()
            vacancy_id = vacancy.id
            await session.refresh(vacancy)
            before = vacancy.updated_at
            assert vacancy.created_at == before

            vacancy.title = "Backend Engineer, renamed"
            await session.commit()
            await session.refresh(vacancy)

            assert vacancy.updated_at > before
            assert vacancy.created_at < vacancy.updated_at
    finally:
        if vacancy_id is not None:
            async with AsyncSession(async_engine) as cleanup:
                await cleanup.execute(delete(Vacancy).where(Vacancy.id == vacancy_id))
                await cleanup.commit()
