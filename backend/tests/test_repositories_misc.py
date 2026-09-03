"""Profile, match and pipeline-run repositories.

These three carry the parts of the schema the vacancy repository does not: the
resume side of the matching contract, the JSONB explanation attached to every
score, and the bookkeeping the sources page reads. The behaviours pinned here
are the ones whose regressions are silent — an update that nulls untouched
columns, a rescore that duplicates instead of refreshing, an ordering that only
looks right because the rows happened to be inserted in that order.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import MatchBucket, PipelineRunStatus, Seniority, SkillLevel
from app.db.models import CandidateProfile, Match, PipelineRun, ProfileSkill
from app.db.repositories import (
    MatchRepository,
    PipelineRunRepository,
    ProfileRepository,
    VacancyRepository,
)
from app.schemas.match import MatchComponentScores, MatchCreate, MatchedSkill, MissingSkill
from app.schemas.pipeline import PipelineRunCreate, PipelineRunFinish
from app.schemas.profile import CandidateProfileUpdate, SkillCreate
from factories import EPOCH, make_match, make_profile, make_source, make_vacancy

# ── helpers ───────────────────────────────────────────────────────────


async def make_stored_profile(
    profiles: ProfileRepository, session: AsyncSession, **kwargs: Any
) -> CandidateProfile:
    """A persisted profile, flushed and ready to be referenced by matches."""
    profile = await profiles.create(make_profile(**kwargs))
    await session.flush()
    return profile


async def make_stored_vacancy(vacancies: VacancyRepository, seed: str) -> UUID:
    """Id of a persisted posting a match can point at."""
    slug, external_id, url, raw = make_source(seed)
    result = await vacancies.upsert_by_external_id(
        make_vacancy(seed), source_slug=slug, external_id=external_id, url=url, raw=raw
    )
    return result.vacancy_id


async def skill_names_in_database(session: AsyncSession, profile_id: UUID) -> set[str]:
    """Skill rows actually on disk, read past the identity map."""
    rows = await session.execute(
        select(ProfileSkill.canonical_name).where(ProfileSkill.profile_id == profile_id)
    )
    return set(rows.scalars().all())


async def count_matches(session: AsyncSession, profile_id: UUID) -> int:
    """How many match rows the database holds for a profile."""
    stmt = select(func.count()).select_from(Match).where(Match.profile_id == profile_id)
    return int((await session.execute(stmt)).scalar_one())


async def open_run_at(
    pipeline_runs: PipelineRunRepository,
    session: AsyncSession,
    slug: str,
    moment: datetime,
) -> PipelineRun:
    """A run whose ``started_at`` is chosen rather than defaulted.

    ``now()`` in PostgreSQL is the transaction timestamp, and the whole test
    runs in one transaction, so every run opened here would otherwise share a
    single ``started_at`` and no ordering assertion would prove anything.
    """
    run = await pipeline_runs.start(PipelineRunCreate(source_slug=slug))
    run.started_at = moment
    await session.flush()
    return run


async def finished_run_at(
    pipeline_runs: PipelineRunRepository,
    session: AsyncSession,
    slug: str,
    moment: datetime,
    status: PipelineRunStatus,
) -> PipelineRun:
    """A closed run with a chosen start time and a given outcome."""
    run = await open_run_at(pipeline_runs, session, slug, moment)
    await pipeline_runs.finish(run.id, PipelineRunFinish(status=status))
    return run


# ── ProfileRepository ─────────────────────────────────────────────────


async def test_create_persists_the_profile_together_with_its_skills(
    profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """The extractor emits a profile and its skills as one result; persisting
    them in two steps would leave a skill-less profile visible after a crash."""
    created = await profiles.create(make_profile(skills=("python", "sql")))

    # Read columns, not the ORM instance: the identity map would hand back the
    # object we just built and prove nothing about what reached the table.
    stored_name = await db_session.scalar(
        select(CandidateProfile.name).where(CandidateProfile.id == created.id)
    )

    assert stored_name == "Nurzhan"
    assert await skill_names_in_database(db_session, created.id) == {"python", "sql"}


async def test_get_active_returns_the_newest_active_profile(
    profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """The dashboard scores against exactly one profile: re-uploading a resume
    must switch it over, not leave the old one in charge."""
    older = await make_stored_profile(profiles, db_session, name="Older")
    newer = await make_stored_profile(profiles, db_session, name="Newer")
    older.created_at = EPOCH
    newer.created_at = EPOCH + timedelta(days=1)
    await db_session.flush()

    active = await profiles.get_active()

    assert active is not None
    assert active.id == newer.id


async def test_get_active_ignores_deactivated_profiles(
    profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """Deactivating the newest profile has to hand the dashboard back to the
    previous one, otherwise the flag does nothing."""
    kept = await make_stored_profile(profiles, db_session, name="Kept")
    retired = await make_stored_profile(profiles, db_session, name="Retired")
    kept.created_at = EPOCH
    retired.created_at = EPOCH + timedelta(days=1)
    await profiles.update(retired.id, CandidateProfileUpdate(is_active=False))

    active = await profiles.get_active()

    assert active is not None
    assert active.id == kept.id


async def test_update_leaves_fields_that_were_not_sent_alone(
    profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """The UI patches one field at a time. A naive ``model_dump()`` would send
    every unset optional as None and wipe the rest of the profile."""
    created = await profiles.create(make_profile(name="Nurzhan"))

    await profiles.update(created.id, CandidateProfileUpdate(name="Corrected"))

    row = (
        await db_session.execute(
            select(
                CandidateProfile.name,
                CandidateProfile.headline,
                CandidateProfile.seniority,
                CandidateProfile.total_years,
                CandidateProfile.salary_currency,
                CandidateProfile.locations,
            ).where(CandidateProfile.id == created.id)
        )
    ).one()

    assert row.name == "Corrected"
    assert row.headline == "Backend Engineer"
    assert row.seniority is Seniority.MIDDLE
    assert row.total_years == Decimal("4.0")
    assert row.salary_currency == "USD"
    assert row.locations == ["Алматы"]


async def test_update_can_set_a_field_to_none_when_it_is_sent(
    profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """Clearing a wrongly extracted field must still be possible: exclude_unset
    has to distinguish "not sent" from "sent as null"."""
    created = await profiles.create(make_profile())

    await profiles.update(created.id, CandidateProfileUpdate(headline=None))

    stored_headline = await db_session.scalar(
        select(CandidateProfile.headline).where(CandidateProfile.id == created.id)
    )
    assert stored_headline is None


async def test_update_of_an_unknown_profile_reports_the_miss(
    profiles: ProfileRepository,
) -> None:
    """A stale id from the UI must surface as a 404, not as a silent no-op."""
    assert await profiles.update(uuid4(), CandidateProfileUpdate(name="X")) is None


async def test_replace_skills_deletes_the_rows_it_dropped(
    profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """A re-extraction produces a complete skill set. Skills the candidate no
    longer claims have to leave the table, not merely the session, or matching
    keeps scoring against them."""
    created = await profiles.create(make_profile(skills=("python", "django")))

    await profiles.replace_skills(
        created.id,
        [SkillCreate(canonical_name="go", level=SkillLevel.WORKING)],
    )

    assert await skill_names_in_database(db_session, created.id) == {"go"}


async def test_replace_skills_keeps_a_skill_that_appears_in_both_sets(
    profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """Re-extraction usually overlaps heavily with the previous set. Replacing
    a skill the profile already has must not trip the unique constraint on
    (profile_id, canonical_name)."""
    created = await profiles.create(make_profile(skills=("python", "django")))

    await profiles.replace_skills(
        created.id,
        [
            SkillCreate(canonical_name="python", level=SkillLevel.EXPERT),
            SkillCreate(canonical_name="go", level=SkillLevel.WORKING),
        ],
    )

    assert await skill_names_in_database(db_session, created.id) == {"python", "go"}


async def test_replace_skills_of_an_unknown_profile_reports_the_miss(
    profiles: ProfileRepository,
) -> None:
    """Replacing skills on a deleted profile must not create an orphan set."""
    assert await profiles.replace_skills(uuid4(), []) is None


async def test_set_embedding_stores_a_full_length_vector(
    profiles: ProfileRepository,
) -> None:
    """The HNSW index is declared for a fixed dimension: a vector that comes
    back short or truncated means every similarity search is wrong."""
    created = await profiles.create(make_profile())
    embedding = [i / 1024 for i in range(1024)]

    await profiles.set_embedding(created.id, embedding)

    stored = await profiles.get(created.id)
    assert stored is not None
    assert stored.embedding is not None
    assert len(stored.embedding) == 1024
    assert float(stored.embedding[0]) == pytest.approx(0.0)
    assert float(stored.embedding[-1]) == pytest.approx(1023 / 1024)


async def test_delete_takes_skills_and_matches_with_it(
    profiles: ProfileRepository,
    matches: MatchRepository,
    vacancies: VacancyRepository,
    db_session: AsyncSession,
) -> None:
    """Nothing may outlive the profile it explains: an orphaned match would
    still be joined into the dashboard and scored against a resume that is
    gone."""
    profile = await make_stored_profile(profiles, db_session)
    vacancy_id = await make_stored_vacancy(vacancies, "delete-cascade")
    await matches.bulk_upsert([make_match(profile.id, vacancy_id)])

    assert await profiles.delete(profile.id) is True

    assert await profiles.get(profile.id) is None
    assert await skill_names_in_database(db_session, profile.id) == set()
    assert await count_matches(db_session, profile.id) == 0


async def test_delete_of_an_unknown_profile_returns_false(
    profiles: ProfileRepository,
) -> None:
    """Deleting twice is a normal race, not an error: the second call reports
    that there was nothing to remove."""
    assert await profiles.delete(uuid4()) is False


# ── MatchRepository ───────────────────────────────────────────────────


async def test_bulk_upsert_reports_how_many_rows_it_wrote(
    profiles: ProfileRepository,
    matches: MatchRepository,
    vacancies: VacancyRepository,
    db_session: AsyncSession,
) -> None:
    """The pipeline logs this number as "scored"; if it were the batch size
    instead of the row count, a partially failed rescore would look complete."""
    profile = await make_stored_profile(profiles, db_session)
    batch = [
        make_match(profile.id, await make_stored_vacancy(vacancies, f"bulk-{index}"))
        for index in range(3)
    ]

    assert await matches.bulk_upsert(batch) == 3
    assert await count_matches(db_session, profile.id) == 3


async def test_rescoring_updates_the_existing_row_instead_of_duplicating(
    profiles: ProfileRepository,
    matches: MatchRepository,
    vacancies: VacancyRepository,
    db_session: AsyncSession,
) -> None:
    """Matching runs on every pipeline pass. Without the upsert the dashboard
    would show one row per run for the same posting."""
    profile = await make_stored_profile(profiles, db_session)
    vacancy_id = await make_stored_vacancy(vacancies, "rescore")
    await matches.bulk_upsert([make_match(profile.id, vacancy_id, 60)])

    assert await matches.bulk_upsert([make_match(profile.id, vacancy_id, 91)]) == 1

    assert await count_matches(db_session, profile.id) == 1
    stored = await matches.get(profile.id, vacancy_id)
    assert stored is not None
    assert stored.score == Decimal("91.00")
    assert stored.bucket is MatchBucket.APPLY_NOW


async def test_bulk_upsert_collapses_duplicates_inside_one_batch(
    profiles: ProfileRepository,
    matches: MatchRepository,
    vacancies: VacancyRepository,
    db_session: AsyncSession,
) -> None:
    """The same posting can reach the scorer twice in one run through two
    sources. ON CONFLICT cannot update a row the same statement inserted, so a
    duplicate pair must be dropped before the write rather than raise."""
    profile = await make_stored_profile(profiles, db_session)
    vacancy_id = await make_stored_vacancy(vacancies, "duplicate-pair")

    written = await matches.bulk_upsert(
        [make_match(profile.id, vacancy_id, 60), make_match(profile.id, vacancy_id, 88)]
    )

    assert written == 1
    assert await count_matches(db_session, profile.id) == 1


async def test_bulk_upsert_of_an_empty_batch_writes_nothing(
    matches: MatchRepository,
) -> None:
    """A profile with no candidate vacancies is normal; an empty VALUES list
    would be a syntax error, so the early return is load-bearing."""
    assert await matches.bulk_upsert([]) == 0


async def test_the_explanation_payloads_survive_the_round_trip(
    profiles: ProfileRepository,
    matches: MatchRepository,
    vacancies: VacancyRepository,
    db_session: AsyncSession,
) -> None:
    """Everything the UI shows besides the number lives in JSONB. If a list
    came back as a string, or a nested Decimal as something unparseable, the
    match detail page would break only for scores that have content."""
    profile = await make_stored_profile(profiles, db_session)
    vacancy_id = await make_stored_vacancy(vacancies, "payloads")
    written = MatchCreate(
        profile_id=profile.id,
        vacancy_id=vacancy_id,
        score=Decimal("72.50"),
        rule_score=Decimal("72.50"),
        bucket=MatchBucket.STRONG,
        component_scores=MatchComponentScores(
            skill_coverage_required=Decimal("80.00"),
            semantic_similarity=Decimal("61.25"),
        ),
        matched_skills=[MatchedSkill(canonical_name="python", coverage=Decimal("1.0"))],
        missing_required=[MissingSkill(canonical_name="kubernetes", weight=Decimal("0.8"))],
        missing_nice=[MissingSkill(canonical_name="terraform", weight=Decimal("0.3"))],
        red_flags=["unpaid overtime", "no salary"],
    )

    await matches.bulk_upsert([written])

    stored = await matches.get(profile.id, vacancy_id)
    assert stored is not None
    assert stored.red_flags == ["unpaid overtime", "no salary"]
    assert [entry["canonical_name"] for entry in stored.matched_skills] == ["python"]
    assert [entry["canonical_name"] for entry in stored.missing_required] == ["kubernetes"]
    assert [entry["canonical_name"] for entry in stored.missing_nice] == ["terraform"]
    assert MatchComponentScores.model_validate(stored.component_scores) == written.component_scores
    assert [MatchedSkill.model_validate(e) for e in stored.matched_skills] == written.matched_skills
    assert [
        MissingSkill.model_validate(entry) for entry in stored.missing_required
    ] == written.missing_required


async def test_top_for_profile_returns_the_best_scores_first(
    profiles: ProfileRepository,
    matches: MatchRepository,
    vacancies: VacancyRepository,
    db_session: AsyncSession,
) -> None:
    """This feeds the LLM re-rank shortlist, so the order is the selection:
    scoring high and being cut from the top of the list is the failure."""
    profile = await make_stored_profile(profiles, db_session)
    scores = [55, 91, 73]
    batch = [
        make_match(profile.id, await make_stored_vacancy(vacancies, f"top-{score}"), score)
        for score in scores
    ]
    await matches.bulk_upsert(batch)

    found = await matches.top_for_profile(profile.id)

    assert [match.score for match in found] == [
        Decimal("91.00"),
        Decimal("73.00"),
        Decimal("55.00"),
    ]


@pytest.mark.parametrize(
    ("limit", "min_score", "expected"),
    [
        (2, None, [Decimal("91.00"), Decimal("73.00")]),
        (30, 70.0, [Decimal("91.00"), Decimal("73.00")]),
        (1, 70.0, [Decimal("91.00")]),
        (30, 95.0, []),
    ],
)
async def test_top_for_profile_honours_limit_and_min_score(
    profiles: ProfileRepository,
    matches: MatchRepository,
    vacancies: VacancyRepository,
    db_session: AsyncSession,
    limit: int,
    min_score: float | None,
    expected: list[Decimal],
) -> None:
    """The re-rank budget is a token budget: a shortlist that ignores its cap
    or its floor sends hopeless matches to the LLM and costs real money."""
    profile = await make_stored_profile(profiles, db_session)
    batch = [
        make_match(profile.id, await make_stored_vacancy(vacancies, f"cap-{score}"), score)
        for score in (55, 91, 73)
    ]
    await matches.bulk_upsert(batch)

    found = await matches.top_for_profile(profile.id, limit=limit, min_score=min_score)

    assert [match.score for match in found] == expected


async def test_top_for_profile_eager_loads_the_vacancy(
    profiles: ProfileRepository,
    matches: MatchRepository,
    vacancies: VacancyRepository,
    db_session: AsyncSession,
) -> None:
    """The caller needs the posting text of every shortlisted match. Lazy
    loading it would be an N+1 under async, which fails rather than degrades."""
    profile = await make_stored_profile(profiles, db_session)
    vacancy_id = await make_stored_vacancy(vacancies, "eager")
    await matches.bulk_upsert([make_match(profile.id, vacancy_id)])

    found = await matches.top_for_profile(profile.id)

    assert found[0].vacancy.id == vacancy_id
    assert found[0].vacancy.title == make_vacancy("eager").title
    # Vacancy.skills is lazy="selectin" on the mapper, so this does not pin the
    # nested option; it pins the weaker but real contract that the whole
    # shortlist can be walked without a single lazy load under async.
    assert found[0].vacancy.skills == []


async def test_delete_for_profile_spares_other_profiles(
    profiles: ProfileRepository,
    matches: MatchRepository,
    vacancies: VacancyRepository,
    db_session: AsyncSession,
) -> None:
    """A full rescore clears the old matches first. Clearing the table instead
    of one profile's rows would silently wipe a second candidate's dashboard."""
    mine = await make_stored_profile(profiles, db_session, name="Mine")
    theirs = await make_stored_profile(profiles, db_session, name="Theirs")
    vacancy_id = await make_stored_vacancy(vacancies, "shared")
    await matches.bulk_upsert([make_match(mine.id, vacancy_id), make_match(theirs.id, vacancy_id)])

    assert await matches.delete_for_profile(mine.id) == 1

    assert await count_matches(db_session, mine.id) == 0
    assert await count_matches(db_session, theirs.id) == 1


async def test_missing_skill_counts_ranks_the_most_common_gap_first(
    profiles: ProfileRepository,
    matches: MatchRepository,
    vacancies: VacancyRepository,
    db_session: AsyncSession,
) -> None:
    """This is the "what should I learn next" answer. It is only useful if the
    counts aggregate across matches and the biggest blocker comes first."""
    profile = await make_stored_profile(profiles, db_session)
    gaps: Sequence[Sequence[str]] = (
        ("kubernetes", "go"),
        ("kubernetes", "rust"),
        ("kubernetes",),
        ("go",),
    )
    batch = [
        make_match(
            profile.id,
            await make_stored_vacancy(vacancies, f"gap-{index}"),
            80,
            missing_required=missing,
        )
        for index, missing in enumerate(gaps)
    ]
    await matches.bulk_upsert(batch)

    counts = await matches.missing_skill_counts(profile.id)

    assert counts == [("kubernetes", 3), ("go", 2), ("rust", 1)]


async def test_missing_skill_counts_ignores_matches_below_min_score(
    profiles: ProfileRepository,
    matches: MatchRepository,
    vacancies: VacancyRepository,
    db_session: AsyncSession,
) -> None:
    """A skill missing only from vacancies that were never reachable anyway is
    not a gap worth learning, so the floor has to actually filter."""
    profile = await make_stored_profile(profiles, db_session)
    reachable = await make_stored_vacancy(vacancies, "reachable")
    hopeless = await make_stored_vacancy(vacancies, "hopeless")
    await matches.bulk_upsert(
        [
            make_match(profile.id, reachable, 80, missing_required=("kubernetes",)),
            make_match(profile.id, hopeless, 20, missing_required=("cobol",)),
        ]
    )

    counts = await matches.missing_skill_counts(profile.id, min_score=55.0)

    assert counts == [("kubernetes", 1)]


# ── PipelineRunRepository ─────────────────────────────────────────────


async def test_start_opens_a_running_record(
    pipeline_runs: PipelineRunRepository, db_session: AsyncSession
) -> None:
    """The record exists before the fetch, so a crashed run is visible as one
    that never finished instead of leaving no trace at all."""
    run = await pipeline_runs.start(PipelineRunCreate(source_slug="hh"))

    row = (
        await db_session.execute(
            select(
                PipelineRun.source_slug,
                PipelineRun.status,
                PipelineRun.started_at,
                PipelineRun.finished_at,
            ).where(PipelineRun.id == run.id)
        )
    ).one()

    assert row.source_slug == "hh"
    assert row.status is PipelineRunStatus.RUNNING
    assert row.started_at is not None
    assert row.finished_at is None


async def test_finish_records_the_counters_the_errors_and_the_end_time(
    pipeline_runs: PipelineRunRepository, db_session: AsyncSession
) -> None:
    """A broken source must not abort the pipeline, which means its failures
    only ever surface here. Losing them loses the entire diagnosis."""
    run = await pipeline_runs.start(PipelineRunCreate(source_slug="hh"))

    stored = await pipeline_runs.finish(
        run.id,
        PipelineRunFinish(
            status=PipelineRunStatus.PARTIAL,
            found=10,
            new=7,
            updated=3,
            errors=[{"page": 2, "error": "timeout"}],
        ),
    )

    assert stored is not None
    # Read the row, not the returned instance: ``finish`` sets these attributes
    # in memory, so asserting on the object it hands back would pass even if the
    # UPDATE never reached the database.
    row = (
        await db_session.execute(
            select(
                PipelineRun.status,
                PipelineRun.found,
                PipelineRun.new,
                PipelineRun.updated,
                PipelineRun.errors,
                PipelineRun.finished_at,
            ).where(PipelineRun.id == run.id)
        )
    ).one()

    assert row.status is PipelineRunStatus.PARTIAL
    assert (row.found, row.new, row.updated) == (10, 7, 3)
    assert row.errors == [{"page": 2, "error": "timeout"}]
    assert row.finished_at is not None


async def test_finish_of_an_unknown_run_reports_the_miss(
    pipeline_runs: PipelineRunRepository,
) -> None:
    """A run id from a previous deploy must not resurrect a row."""
    outcome = PipelineRunFinish(status=PipelineRunStatus.SUCCESS)

    assert await pipeline_runs.finish(uuid4(), outcome) is None


async def test_recent_returns_the_newest_run_first(
    pipeline_runs: PipelineRunRepository, db_session: AsyncSession
) -> None:
    """The sources page is a history view; insertion order is not chronology
    once runs are opened concurrently."""
    oldest = await open_run_at(pipeline_runs, db_session, "hh", EPOCH)
    newest = await open_run_at(pipeline_runs, db_session, "hh", EPOCH + timedelta(hours=2))
    middle = await open_run_at(pipeline_runs, db_session, "hh", EPOCH + timedelta(hours=1))

    found = await pipeline_runs.recent()

    assert [run.id for run in found] == [newest.id, middle.id, oldest.id]


async def test_recent_can_be_narrowed_to_one_source(
    pipeline_runs: PipelineRunRepository, db_session: AsyncSession
) -> None:
    """Debugging a flaky connector means reading its runs only; other sources
    in the list bury the ones that matter."""
    await open_run_at(pipeline_runs, db_session, "hh", EPOCH)
    mine = await open_run_at(pipeline_runs, db_session, "habr", EPOCH)

    found = await pipeline_runs.recent(source_slug="habr")

    assert [run.id for run in found] == [mine.id]


async def test_recent_never_returns_more_than_the_limit(
    pipeline_runs: PipelineRunRepository, db_session: AsyncSession
) -> None:
    """Run history grows without bound; an unbounded read would eventually
    load every run the system ever performed."""
    for hour in range(4):
        await open_run_at(pipeline_runs, db_session, "hh", EPOCH + timedelta(hours=hour))

    found = await pipeline_runs.recent(limit=2)

    assert len(found) == 2


@pytest.mark.parametrize("status", [PipelineRunStatus.SUCCESS, PipelineRunStatus.PARTIAL])
async def test_last_successful_counts_partial_runs_too(
    pipeline_runs: PipelineRunRepository, db_session: AsyncSession, status: PipelineRunStatus
) -> None:
    """Incremental crawling resumes from this timestamp. A partial run did
    fetch something, so treating it as a failure would refetch that window and
    treating a failure as success would skip a window entirely."""
    await finished_run_at(pipeline_runs, db_session, "hh", EPOCH, PipelineRunStatus.SUCCESS)
    latest = await finished_run_at(
        pipeline_runs, db_session, "hh", EPOCH + timedelta(hours=1), status
    )

    found = await pipeline_runs.last_successful("hh")

    assert found is not None
    assert found.id == latest.id


async def test_last_successful_skips_failed_and_still_running_attempts(
    pipeline_runs: PipelineRunRepository, db_session: AsyncSession
) -> None:
    """Resuming from a run that fetched nothing would silently drop every
    posting published since the last run that did."""
    good = await finished_run_at(pipeline_runs, db_session, "hh", EPOCH, PipelineRunStatus.SUCCESS)
    await finished_run_at(
        pipeline_runs, db_session, "hh", EPOCH + timedelta(hours=1), PipelineRunStatus.FAILED
    )
    await open_run_at(pipeline_runs, db_session, "hh", EPOCH + timedelta(hours=2))

    found = await pipeline_runs.last_successful("hh")

    assert found is not None
    assert found.id == good.id


async def test_latest_per_source_returns_exactly_one_newest_row_each(
    pipeline_runs: PipelineRunRepository, db_session: AsyncSession
) -> None:
    """The sources page shows one line per connector. Repeating a source, or
    showing its stale run next to a fresh one, misreports whether it works."""
    await open_run_at(pipeline_runs, db_session, "hh", EPOCH)
    newest_hh = await open_run_at(pipeline_runs, db_session, "hh", EPOCH + timedelta(hours=3))
    only_habr = await open_run_at(pipeline_runs, db_session, "habr", EPOCH + timedelta(hours=1))

    found = await pipeline_runs.latest_per_source()

    assert {run.source_slug for run in found} == {"hh", "habr"}
    assert {run.id for run in found} == {newest_hh.id, only_habr.id}


async def test_latest_per_source_can_be_narrowed_to_known_slugs(
    pipeline_runs: PipelineRunRepository, db_session: AsyncSession
) -> None:
    """A connector removed from the registry still has rows in the table; the
    page must not resurrect it as a source that exists."""
    await open_run_at(pipeline_runs, db_session, "retired", EPOCH)
    live = await open_run_at(pipeline_runs, db_session, "hh", EPOCH)

    found = await pipeline_runs.latest_per_source(["hh"])

    assert [run.id for run in found] == [live.id]
