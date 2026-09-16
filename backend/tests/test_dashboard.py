"""The dashboard: its four screens, and the three places they could lie.

Grouped by the claim under test rather than by endpoint, because the endpoints
are thin and the claims are not:

* **not measured is not zero.** A sitemap file nobody counted, a vacancy nobody
  scored, an application hh has not answered — every one of them has to reach
  the screen as an absence and never as a number.
* **the list is filtered by what the caller typed.** FastAPI expands a Pydantic
  model into query parameters only while it is the handler's only query field,
  and the failure mode when it is not is silent: every filter ignored, 200 OK.
  That regression is worth a test of its own.
* **sent means sent.** One column, one counter and one screen depend on
  ``sent_at``, which only the process that did the typing writes.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ApplicationStatus, MatchBucket, PipelineRunStatus, RuleScope
from app.db.models import Application, CandidateProfile, ProfileSkill
from app.db.repositories.match import MatchRepository
from app.db.repositories.pipeline_run import PipelineRunRepository
from app.db.repositories.profile import ProfileRepository
from app.db.repositories.source_state import SourceStateRepository
from app.db.repositories.vacancy import VacancyRepository
from app.documents.rules import version as _rules_version
from app.llm.router import LLMRouter
from app.schemas.crawl import SavedState
from app.schemas.pipeline import PipelineRunCreate, PipelineRunFinish
from app.schemas.vacancy import VacancyFilter, VacancyQuery
from app.services import documents as documents_service
from app.services import overview as overview_service
from app.services import tracker as tracker_service
from app.services import vacancies as vacancies_service
from app.services import workshop as workshop_service
from app.sources.hh import CENSUS_PREFIX, POSITION_PREFIX, HHSource
from app.workshop.rules import BUILTIN_RULES
from factories import make_match, make_profile, make_upsert_item, make_vacancy

pytestmark = pytest.mark.db

#: What a letter written with no rules of the owner's own is stamped with: the
#: built-in guard plus the workshop's two undeletable rules, which is exactly
#: what ``workshop.store.active_rules`` hands the writer on a fresh database.
#:
#: The fingerprint rather than the guard's integer since the merge: a letter is
#: judged by the built-in checks *and* the owner's rules, and a value naming only
#: the first would report two letters written either side of an edit as written
#: under the same rules.
RULES_VERSION = _rules_version(
    tuple(rule for rule in BUILTIN_RULES if rule.applies_to(RuleScope.COVER_LETTER)),
    scope=RuleScope.COVER_LETTER,
)

EPOCH = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# ── fixtures shared by the screens ────────────────────────────────────


async def a_profile(
    profiles: ProfileRepository, *, skills: Sequence[str] = ("python", "fastapi")
) -> CandidateProfile:
    """The active profile every screen is drawn for."""
    profile = await profiles.create(make_profile(skills=skills))
    await profiles.session.flush()
    return profile


async def a_vacancy(vacancies: VacancyRepository, seed: str = "dash-1", **kwargs: Any) -> UUID:
    """One posting, upserted the way a connector would."""
    result = await vacancies.bulk_upsert([make_upsert_item(seed, **kwargs)])
    return result.vacancy_ids[0]


# ── overview: absences stay absences ──────────────────────────────────


async def test_counts_come_from_the_database_and_separate_scored_from_stored(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """The gap between "in the corpus" and "scored" is the point of the panel.

    A crawl runs for twenty minutes and scoring follows it, so the two numbers
    are never equal in practice. A screen that showed one of them would be
    describing a database that does not exist.
    """
    profile = await a_profile(profiles)
    first = await a_vacancy(vacancies, "dash-scored")
    await a_vacancy(vacancies, "dash-unscored")
    await matches.bulk_upsert([make_match(profile.id, first, 80)])

    counts = (await overview_service.build(db_session)).vacancies

    assert counts.total == 2
    assert counts.scored == 1
    # No embedding step has run over these rows, and the panel says so rather
    # than reporting a complete corpus.
    assert counts.embedded == 0
    assert counts.needs_embedding == 2


async def test_a_run_stopped_by_a_check_for_robots_is_its_own_outcome(
    db_session: AsyncSession, pipeline_runs: PipelineRunRepository
) -> None:
    """``partial`` covers two situations that need opposite reactions.

    One is rescheduled and nothing in the code is wrong; the other sends
    somebody to read a connector. The runner already separates them by
    recording the challenge under a stage of its own, and the screen has to
    carry that distinction rather than re-derive it from an error string.
    """
    challenged = await pipeline_runs.start(PipelineRunCreate(source_slug="hh"))
    await pipeline_runs.finish(
        challenged.id,
        PipelineRunFinish(
            status=PipelineRunStatus.PARTIAL,
            found=172,
            errors=[{"stage": "challenge", "error": "HHChallengedError", "detail": "captcha"}],
        ),
    )
    broken = await pipeline_runs.start(PipelineRunCreate(source_slug="jsearch"))
    await pipeline_runs.finish(
        broken.id,
        PipelineRunFinish(
            status=PipelineRunStatus.PARTIAL,
            errors=[{"stage": "crawl", "error": "SourceError", "detail": "no key"}],
        ),
    )

    runs = {run.slug: run for run in (await overview_service.build(db_session)).runs}

    assert runs["hh"].stopped_by_robot_check is True
    assert runs["jsearch"].stopped_by_robot_check is False
    # Both are still `partial`: the flag adds a distinction, it does not
    # overwrite the status the runner recorded.
    assert runs["hh"].status is PipelineRunStatus.PARTIAL


async def test_the_harvest_window_opens_at_the_latest_run_not_the_earliest(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    pipeline_runs: PipelineRunRepository,
) -> None:
    """Sources run on their own cadences, so "the last run" has to be a choice.

    Taking the earliest of the latest starts would open the window at whenever
    the sleepiest source last ran — a fortnight — and list a fortnight of
    postings as what the last crawl bought.
    """
    old = await pipeline_runs.start(PipelineRunCreate(source_slug="arbeitnow"))
    old.started_at = EPOCH - timedelta(days=14)
    recent = await pipeline_runs.start(PipelineRunCreate(source_slug="hh"))
    recent.started_at = datetime.now(UTC) - timedelta(minutes=5)
    await db_session.flush()
    await a_vacancy(vacancies, "dash-fresh")

    harvest = (await overview_service.build(db_session)).harvest

    assert harvest.since == recent.started_at
    assert harvest.total == 1
    assert [item.title for item in harvest.items] == ["Backend Engineer dash-fresh"]


async def test_the_harvest_names_the_postings_a_run_bought(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
    pipeline_runs: PipelineRunRepository,
) -> None:
    """Titles, because the counters cannot answer the question this panel is for.

    hh's sitemap carries a URL and a date, so a page is paid for before anyone
    can tell what it advertises. "41 new postings" says nothing about whether
    the budget went on this candidate's field; the titles do.
    """
    run = await pipeline_runs.start(PipelineRunCreate(source_slug="hh"))
    run.started_at = datetime.now(UTC) - timedelta(minutes=5)
    await db_session.flush()

    profile = await a_profile(profiles)
    wanted = await a_vacancy(vacancies, "dash-python", title="Python Developer")
    await a_vacancy(vacancies, "dash-welder", title="Сварщик")
    await matches.bulk_upsert([make_match(profile.id, wanted, 91)])

    harvest = (await overview_service.build(db_session)).harvest
    by_title = {item.title: item for item in harvest.items}

    assert set(by_title) == {"Python Developer", "Сварщик"}
    assert by_title["Python Developer"].score == Decimal("91.00")
    # Not zero: nothing has scored it, and a zero here would read as a verdict
    # this scorer is perfectly capable of producing.
    assert by_title["Сварщик"].score is None


async def test_the_crawl_position_is_decoded_by_the_connector_that_wrote_it(
    db_session: AsyncSession,
) -> None:
    """The overview never looks inside a stored value.

    Keys are the connector's invention — one per sitemap file per host here, one
    per feed somewhere else — so the service hands each source its own rows and
    renders what comes back. That is CLAUDE.md rule 5 applied to reading: adding
    a source must not mean editing the overview.
    """
    states = SourceStateRepository(db_session)
    await states.set(
        "hh",
        f"{POSITION_PREFIX}almaty.hh.kz:vacancy0",
        {
            "covered": [
                {
                    "low_lastmod": "2026-01-01T00:00:00Z",
                    "low_id": "1",
                    "high_lastmod": "2026-01-05T00:00:00Z",
                    "high_id": "9",
                }
            ]
        },
    )
    await states.set(
        "hh", f"{CENSUS_PREFIX}almaty.hh.kz:vacancy0", {"total": 900, "outstanding": 40}
    )
    await states.set("hh", f"{POSITION_PREFIX}astana.hh.kz:vacancy0", {"covered": []})
    await db_session.flush()

    crawl = (await overview_service.build(db_session)).crawl
    positions = {(p.scope, p.label): p for source in crawl for p in source.positions}

    counted = positions[("almaty.hh.kz", "vacancy0")]
    assert (counted.total, counted.outstanding, counted.stretches) == (900, 40, 1)
    assert counted.title == "Алматы"
    # The file no run has counted since counting was added. Unknown, not
    # finished: reporting 0 outstanding here would announce a completed
    # backfill that never happened.
    uncounted = positions[("astana.hh.kz", "vacancy0")]
    assert uncounted.total is None
    assert uncounted.outstanding is None


@pytest.mark.unit
def test_a_state_row_from_another_scheme_is_ignored_rather_than_guessed_at() -> None:
    """A key this connector does not recognise is not a position.

    Inventing a reading for it would put a number on the screen that nothing
    produced, which is worse than the row being invisible.
    """
    described = HHSource().describe_position(
        [
            SavedState(key="something:else", value={"covered": []}, updated_at=EPOCH),
            SavedState(key=f"{CENSUS_PREFIX}almaty.hh.kz:vacancy0", value={}, updated_at=EPOCH),
        ]
    )

    # The census row is unreadable rather than absent — an empty object is not a
    # valid census — so it is dropped with a warning and nothing is rendered.
    assert described == []


# ── the list: the filters have to actually apply ──────────────────────


async def test_every_filter_reaches_the_query(
    async_client: AsyncClient,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """A regression test for a silent failure, not a test of the repository.

    FastAPI expands a Pydantic model into individual query parameters **only
    while it is the handler's only query field**. Declared beside a plain
    ``limit``, the same model becomes one opaque parameter: every filter is
    then ignored and the endpoint answers 200 with the unfiltered list. Nothing
    logs it and no status code shows it — the only way it is caught is by
    asking for a filtered list and counting.
    """
    profile = await a_profile(profiles)
    wanted = await a_vacancy(vacancies, "dash-astana", city="Астана")
    other = await a_vacancy(vacancies, "dash-almaty", city="Алматы")
    await matches.bulk_upsert(
        [make_match(profile.id, wanted, 90), make_match(profile.id, other, 90)]
    )

    response = await async_client.get("/api/v1/vacancies", params={"city": "Астана"})

    assert response.status_code == 200
    assert [item["city"] for item in response.json()["items"]] == ["Астана"]


async def test_a_salary_floor_keeps_the_postings_that_name_no_salary(
    vacancies: VacancyRepository,
) -> None:
    """Measured: five vacancies in six on this corpus advertise nothing.

    ``salary_min_normalized >= x`` is false for NULL, so a floor on its own
    answers with the sixth — silently, and a person reading that list concludes
    the market is empty rather than that the field is.
    """
    await vacancies.bulk_upsert(
        [
            make_upsert_item("dash-paid", salary_min=Decimal("5000"), currency="USD"),
            make_upsert_item("dash-silent", salary_min=None, salary_max=None, currency=None),
        ]
    )
    for seed in ("dash-paid",):
        found = await vacancies.get_by_fingerprint(make_upsert_item(seed)[0].fingerprint)
        assert found is not None
        found.salary_min_normalized = Decimal("5000")
    await vacancies.session.flush()

    kept = await vacancies.list_filtered(VacancyFilter(salary_min=Decimal("1000")))
    strict = await vacancies.list_filtered(
        VacancyFilter(salary_min=Decimal("1000"), include_unpriced=False)
    )

    assert {item.title for item in kept.items} == {
        "Backend Engineer dash-paid",
        "Backend Engineer dash-silent",
    }
    assert {item.title for item in strict.items} == {"Backend Engineer dash-paid"}


@pytest.mark.unit
def test_the_query_model_hands_the_repository_only_the_filtering_half() -> None:
    """Paging travels with the filters and stops at the service boundary.

    They share a model because FastAPI expands exactly one; they must not share
    a contract, or the repository would start taking a cursor twice.
    """
    query = VacancyQuery(city="Алматы", limit=5, cursor="abc", with_total=True)

    filters = query.filters()

    assert filters.city == "Алматы"
    assert not hasattr(filters, "cursor")


# ── the card: missing is not the same as absent ───────────────────────


async def test_a_requirement_the_older_resume_claims_is_not_reported_as_absent(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """The distinction the whole card exists for.

    "Spark is not in this CV" is a line to add before applying; "I have never
    written Scala" is a job to skip. The scorer counts both as missing because a
    score has to come from somewhere, and the screen has to separate them from
    evidence — here, an earlier resume of the same owner.
    """
    active = await a_profile(profiles, skills=("python", "fastapi"))
    older = await profiles.create(make_profile(name="Older", skills=("spark",)))
    older.is_active = False
    older.resume_filename = "resume-2024.pdf"
    await db_session.flush()

    vacancy_id = await a_vacancy(vacancies, "dash-card")
    await matches.bulk_upsert(
        [
            make_match(
                active.id,
                vacancy_id,
                60,
                matched=("python",),
                missing_required=("spark", "scala"),
            )
        ]
    )

    card = await vacancies_service.card(db_session, vacancy_id)

    assert card is not None
    assert [row.canonical_name for row in card.requirements.absent] == ["scala"]
    held = card.requirements.not_in_this_cv
    assert [row.canonical_name for row in held] == ["spark"]
    assert held[0].evidence == vacancies_service.FROM_OTHER_PROFILE
    assert held[0].evidence_detail == "resume-2024.pdf"


async def test_a_requirement_named_in_this_resumes_text_is_evidence_too(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """The CV does say so and extraction missed it, which is a different fix.

    The claim is only made for spellings the dictionary already knows — a resume
    that says "kafka-like queues" produces nothing here, because inventing a
    skill from a near-miss is the failure this column would otherwise cause.
    """
    profile = await profiles.create(
        make_profile(
            skills=("python",),
            raw_text="Бэкенд на Python. Пробовал Rust в одном сервисе обработки событий.",
        )
    )
    await db_session.flush()
    vacancy_id = await a_vacancy(vacancies, "dash-text")
    await matches.bulk_upsert(
        [
            make_match(
                profile.id,
                vacancy_id,
                55,
                matched=("python",),
                missing_required=("rust", "scala"),
            )
        ]
    )

    card = await vacancies_service.card(db_session, vacancy_id)

    assert card is not None
    held = {row.canonical_name: row for row in card.requirements.not_in_this_cv}
    assert set(held) == {"rust"}
    assert held["rust"].evidence == vacancies_service.FROM_RESUME_TEXT
    assert "Rust" in (held["rust"].evidence_detail or "")
    assert [row.canonical_name for row in card.requirements.absent] == ["scala"]


async def test_the_card_shows_the_spelling_the_resume_used(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """``canonical_name`` is a lookup key, not a word anybody wrote."""
    profile = await a_profile(profiles, skills=("postgresql",))
    vacancy_id = await a_vacancy(vacancies, "dash-spelling")
    await matches.bulk_upsert([make_match(profile.id, vacancy_id, 88, matched=("postgresql",))])

    card = await vacancies_service.card(db_session, vacancy_id)

    assert card is not None
    assert [row.spelling for row in card.requirements.covered] == ["Postgresql"]


async def test_a_vacancy_nobody_scored_still_has_a_card(
    db_session: AsyncSession, vacancies: VacancyRepository, profiles: ProfileRepository
) -> None:
    """A posting bought minutes ago has no match row, and that is not an error."""
    await a_profile(profiles)
    vacancy_id = await a_vacancy(vacancies, "dash-unscored-card")

    card = await vacancies_service.card(db_session, vacancy_id)

    assert card is not None
    assert card.match is None
    assert card.requirements.covered == []


async def test_an_unknown_vacancy_is_a_404(async_client: AsyncClient) -> None:
    """A missing row is not an empty card."""
    response = await async_client.get(f"/api/v1/vacancies/{UUID(int=0)}")

    assert response.status_code == 404


# ── the board: sent means sent ────────────────────────────────────────


async def _application(db_session: AsyncSession, vacancy_id: UUID, **columns: Any) -> Application:
    """One tracker row with whatever columns the case under test needs."""
    row = Application(vacancy_id=vacancy_id, status=ApplicationStatus.SAVED, **columns)
    db_session.add(row)
    await db_session.flush()
    return row


async def test_only_a_recorded_send_reaches_the_sent_column(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """``agent_status`` is a report; ``sent_at`` is the evidence behind it.

    A row claiming ``sent`` with no timestamp is a report that lost its own
    evidence. Counting it would make the column unusable as the answer to "how
    many applications actually went out", which is the one number here that can
    be verified at all.
    """
    vacancy_id = await a_vacancy(vacancies, "dash-board")
    await _application(
        db_session, vacancy_id, agent_status="sent", sent_at=EPOCH, hh_negotiations_total=1
    )
    await _application(db_session, vacancy_id, agent_status="sent")
    await _application(db_session, vacancy_id, agent_status="queued")

    board = await tracker_service.board(db_session)
    columns = {column.key: len(column.cards) for column in board.stages}

    assert columns["sent"] == 1
    assert columns["sent_unconfirmed"] == 0
    assert columns["queued"] == 1
    # The claim without evidence is not silently promoted into a stage; it is
    # visible, in the column for rows that are in none.
    assert len(board.other.cards) == 1


async def test_a_send_hh_has_not_confirmed_is_not_in_the_sent_column(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """The first real run (2026-09-16): four sends, no count, no state.

    The agent wrote ``sent_at`` for each of them, and hh's own count never
    reached the tracker. Such a row is shown, under its own heading, and never
    beside a send hh confirmed. A count of zero is hh saying there is nothing,
    so it confirms nothing either; a state for the conversation does.
    """
    vacancy_id = await a_vacancy(vacancies, "dash-unconfirmed")
    await _application(db_session, vacancy_id, agent_status="sent", sent_at=EPOCH)
    await _application(db_session, vacancy_id, sent_at=EPOCH, hh_negotiations_total=0)
    await _application(db_session, vacancy_id, sent_at=EPOCH, hh_last_state="RESPONSE")

    board = await tracker_service.board(db_session)
    columns = {column.key: column.cards for column in board.stages}
    counts = (await overview_service.build(db_session)).applications

    assert [column.key for column in board.stages] == [
        "queued",
        "needs_manual",
        "sent_unconfirmed",
        "sent",
    ]
    assert len(columns["sent_unconfirmed"]) == 2
    assert [card.send_confirmed for card in columns["sent"]] == [True]
    assert (counts.sent, counts.sent_confirmed) == (3, 1)


async def test_a_board_card_says_how_old_the_posting_is_and_whether_it_is_there(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """A third of the first real queue was archived by the time it was sent."""
    vacancy_id = await a_vacancy(vacancies, "dash-fresh")
    await _application(db_session, vacancy_id, agent_status="queued")

    [card] = [
        card for column in (await tracker_service.board(db_session)).stages for card in column.cards
    ]

    assert card.vacancy_active is True
    assert card.vacancy_last_seen_at is not None


async def test_hh_states_are_grouped_and_unknown_ones_survive_verbatim(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """hh's vocabulary is open, and freezing it would hide a new outcome.

    A state the grouping does not know still belongs on the board — under
    "other", with hh's own word intact on the card.
    """
    vacancy_id = await a_vacancy(vacancies, "dash-outcomes")
    await _application(db_session, vacancy_id, sent_at=EPOCH, hh_last_state="DISCARD")
    await _application(db_session, vacancy_id, sent_at=EPOCH, hh_last_state="INVITATION")
    await _application(db_session, vacancy_id, sent_at=EPOCH, hh_last_state="SOMETHING_NEW")

    board = await tracker_service.board(db_session)
    outcomes = {column.key: column.cards for column in board.outcomes}

    assert set(outcomes) == {"rejection", "invitation", tracker_service.UNKNOWN_OUTCOME}
    assert outcomes[tracker_service.UNKNOWN_OUTCOME][0].hh_last_state == "SOMETHING_NEW"


async def test_an_outcome_nobody_reported_creates_no_column(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """An empty column labelled «приглашение» reads as a measurement.

    With two applications sent, "no invitations" is not a fact this data can
    support, so the column is absent rather than shown at zero.
    """
    vacancy_id = await a_vacancy(vacancies, "dash-silent-outcome")
    await _application(db_session, vacancy_id, sent_at=EPOCH)

    board = await tracker_service.board(db_session)

    assert board.outcomes == []


async def test_the_board_carries_the_letter_that_was_actually_typed(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """``sent_letter`` and ``cover_letter`` are different strings after a rewrite.

    The question the board answers is what the employer read; showing the
    current draft under that heading would present an unsent letter as the one
    that got an answer.
    """
    vacancy_id = await a_vacancy(vacancies, "dash-letters")
    await _application(
        db_session,
        vacancy_id,
        sent_at=EPOCH,
        sent_letter="Что отправили.",
        cover_letter="Что сгенерировали заново.",
        hh_warning="Такой отклик может получить отказ.",
        hh_negotiations_total=1,
    )

    board = await tracker_service.board(db_session)
    card = next(column for column in board.stages if column.key == "sent").cards[0]

    assert card.sent_letter == "Что отправили."
    assert card.cover_letter == "Что сгенерировали заново."
    assert card.hh_warning == "Такой отклик может получить отказ."


# ── documents: the rules that judged it, and the rules today ──────────


async def test_a_letter_records_the_rules_that_wrote_it(
    db_session: AsyncSession, vacancies: VacancyRepository, profiles: ProfileRepository
) -> None:
    """And a letter written before the record says so, rather than claiming zero."""
    await a_profile(profiles)
    vacancy_id = await a_vacancy(vacancies, "dash-docs")
    await _application(db_session, vacancy_id, cover_letter="a" * 300)
    await _application(
        db_session, vacancy_id, cover_letter="b" * 300, letter_rules_version=RULES_VERSION
    )

    documents = await documents_service.build(db_session)
    versions = sorted(
        (letter.rules_version for letter in documents.letters), key=lambda v: (v is None, v)
    )

    assert versions == [RULES_VERSION, None]
    assert documents.current_rules_version == RULES_VERSION


async def test_todays_rules_are_re_applied_to_a_stored_letter(
    db_session: AsyncSession, vacancies: VacancyRepository, profiles: ProfileRepository
) -> None:
    """The interesting row is the one whose answer has changed since it was saved.

    hh filters letters carrying an address, so a letter somebody edited a link
    into is one to fix before the next send — and nothing else in the project
    would ever mention it.
    """
    await a_profile(profiles)
    vacancy_id = await a_vacancy(vacancies, "dash-docs-link")
    await _application(
        db_session,
        vacancy_id,
        cover_letter="Здравствуйте. " + "текст " * 40 + "Примеры: github.com/example",
        letter_rules_version=RULES_VERSION,
    )

    documents = await documents_service.build(db_session)

    assert [problem.code for problem in documents.letters[0].problems] == ["contains_link"]


async def test_a_resume_carries_its_audit_and_a_profile_without_one_says_so(
    db_session: AsyncSession, profiles: ProfileRepository
) -> None:
    """NULL is "no audit was recorded", which is not the same as a clean one."""
    await a_profile(profiles)

    documents = await documents_service.build(db_session)

    assert [resume.ats for resume in documents.resumes] == [None]
    assert documents.resumes[0].parse_status is not None


# ── the endpoints answer at all ───────────────────────────────────────


async def test_the_overview_endpoint_answers_on_an_empty_database(
    async_client: AsyncClient,
) -> None:
    """No resume, no crawl, no applications — and still a screen, not a 500.

    The state a fresh installation is in for its first ten minutes, and the one
    a dashboard is most likely to have never been opened in.
    """
    response = await async_client.get("/api/v1/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["profile"] is None
    assert body["harvest"]["since"] is None
    assert body["applications"]["sent"] == 0


async def test_the_active_profile_has_a_url_that_needs_no_id(
    async_client: AsyncClient, profiles: ProfileRepository
) -> None:
    """Every screen needs the profile and none of them has an id to start from."""
    profile = await a_profile(profiles)

    response = await async_client.get("/api/v1/profile/active")

    assert response.status_code == 200
    assert response.json()["id"] == str(profile.id)


async def test_there_is_no_active_profile_before_a_resume_is_uploaded(
    async_client: AsyncClient,
) -> None:
    """A 404 that says which of the two 404s it is."""
    response = await async_client.get("/api/v1/profile/active")

    assert response.status_code == 404
    assert "resume" in response.json()["detail"].lower()


async def test_generating_a_letter_without_a_profile_is_a_conflict(
    async_client: AsyncClient, vacancies: VacancyRepository
) -> None:
    """About the installation, not about this vacancy — so not a skip.

    Every other outcome of that endpoint is a fact about the vacancy asked for
    and comes back as a 200 naming itself. This one stays true for every vacancy
    until somebody uploads a resume, and a screen that rendered it as "skipped"
    would invite the user to try the next row.
    """
    vacancy_id = await a_vacancy(vacancies, "dash-noprofile")

    response = await async_client.post(
        "/api/v1/documents/letters", json={"vacancy_id": str(vacancy_id)}
    )

    assert response.status_code == 409


async def test_the_dashboard_has_no_way_to_send_an_application(async_client: AsyncClient) -> None:
    """The product rule, asserted rather than described.

    No route under the dashboard's own prefixes sends anything: the API has no
    browser and no hh session. The routes that write are an exact set, so a new
    one fails here whatever it is called:

    * the generators — a letter onto the tracker row, phase 10's CV and cover
      letter as downloadable versions;
    * since 2026-09-16, the owner's confirmation of one card (recorded bound to
      the card's digest; the agent sends it later, on the owner's machine,
      after re-reading the page — see ``app/services/confirmations.py``);
    * the operations panel, which starts backend work and *records* the two
      requests only the local agent can act on.
    """
    # Read off the published schema rather than off ``app.routes``: an included
    # router appears there as one opaque object with no path, so a scan of that
    # list finds nothing and passes whatever is behind it.
    spec = (await async_client.get("/openapi.json")).json()
    writing = {
        (path, method)
        for path, operations in spec["paths"].items()
        if path.startswith(("/api/v1/tracker", "/api/v1/documents", "/api/v1/operations"))
        for method in set(operations) - {"get", "head", "options"}
    }

    assert writing == {
        ("/api/v1/documents/letters", "post"),
        ("/api/v1/documents/cv/{vacancy_id}", "post"),
        ("/api/v1/documents/cover-letter/{vacancy_id}", "post"),
        ("/api/v1/tracker/confirmations/{vacancy_id}", "post"),
        ("/api/v1/tracker/confirmations/{vacancy_id}", "delete"),
        ("/api/v1/operations", "post"),
        ("/api/v1/operations/{operation_id}", "delete"),
    }


async def test_the_agent_queue_is_still_behind_its_token(async_client: AsyncClient) -> None:
    """The dashboard's tracker read must not have opened the seam beside it.

    ``/applications`` hands out the letters about to be sent under the owner's
    name and is guarded by a local token; ``/tracker`` is a screen. They are
    separate prefixes precisely so that adding the second could not weaken the
    first, and this asserts that it did not.
    """
    guarded = await async_client.get("/api/v1/applications/queue")
    screen = await async_client.get("/api/v1/tracker/board")

    assert guarded.status_code in {401, 503}
    assert screen.status_code == 200


async def test_a_profile_with_no_skills_still_renders_a_board(
    async_client: AsyncClient, db_session: AsyncSession, profiles: ProfileRepository
) -> None:
    """The empty states are the ones a new installation actually shows."""
    profile = await profiles.create(make_profile(skills=()))
    db_session.add(ProfileSkill(profile_id=profile.id, canonical_name="python"))
    await db_session.flush()

    response = await async_client.get("/api/v1/tracker/board")

    assert response.status_code == 200
    assert response.json()["stages"][0]["cards"] == []


async def test_a_run_row_with_a_malformed_error_entry_does_not_break_the_screen(
    db_session: AsyncSession, pipeline_runs: PipelineRunRepository
) -> None:
    """``pipeline_run.errors`` is free JSONB, so the screen has to survive it.

    A connector records whatever it failed with; the column has no shape and
    cannot be given one without losing failures nobody predicted. What the
    overview promises is that an entry it cannot read costs one line, not the
    panel.
    """
    run = await pipeline_runs.start(PipelineRunCreate(source_slug="hh"))
    await pipeline_runs.finish(
        run.id,
        PipelineRunFinish(status=PipelineRunStatus.FAILED, errors=[{"unexpected": "shape"}]),
    )

    runs = (await overview_service.build(db_session)).runs

    assert runs[0].errors[0].stage is None
    assert runs[0].stopped_by_robot_check is False


async def test_the_counters_read_what_the_agent_wrote(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """One query, five numbers, and each of them from the column that means it."""
    vacancy_id = await a_vacancy(vacancies, "dash-counts")
    await _application(db_session, vacancy_id, sent_at=EPOCH, hh_last_state="RESPONSE")
    await _application(db_session, vacancy_id, agent_status="queued", cover_letter="x" * 300)
    await _application(db_session, vacancy_id, agent_status="needs_manual")

    counts = (await overview_service.build(db_session)).applications

    assert counts.sent == 1
    assert counts.sent_confirmed == 1
    assert counts.queued == 1
    assert counts.needs_manual == 1
    assert counts.with_letter == 1
    assert counts.answered == 1


async def test_a_match_bucket_the_filter_hides_is_still_reachable_on_request(
    async_client: AsyncClient,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """``filtered`` is noise by default and evidence when somebody asks for it."""
    profile = await a_profile(profiles)
    vacancy_id = await a_vacancy(vacancies, "dash-filtered")
    await matches.bulk_upsert([make_match(profile.id, vacancy_id, 10, bucket=MatchBucket.FILTERED)])

    hidden = await async_client.get("/api/v1/vacancies")
    shown = await async_client.get("/api/v1/vacancies", params={"include_filtered": True})

    assert hidden.json()["items"] == []
    assert len(shown.json()["items"]) == 1


# ── the workshop: the one write, and what it records ──────────────────


class NoProviderRouter(LLMRouter):
    """A real router with nothing behind it.

    The honest way to exercise the branch that matters: no model is reachable,
    the generator falls back to the rule-based letter, and the screen has to
    say which of the two a person is about to send. Built from the real class
    with an empty provider map rather than a stub, so the failure it produces is
    the one production produces.
    """

    def __init__(self) -> None:
        super().__init__(providers={})


async def test_a_generated_letter_is_saved_and_reported_as_the_fallback(
    db_session: AsyncSession, vacancies: VacancyRepository, profiles: ProfileRepository
) -> None:
    """A letter written by the fallback and one written by a model are not the same run.

    They reach the database looking identical — one ``cover_letter`` column —
    so the workshop reports which happened. Without that a screen would credit
    the model, and the feedback loop, for text assembled out of the profile.
    """
    await profiles.create(make_profile())
    await db_session.flush()
    vacancy = await vacancies.upsert_by_external_id(
        make_vacancy("workshop-1"),
        source_slug="hh",
        external_id="hh-workshop-1",
        url="https://e.test/workshop-1",
        raw={"_derived": {"key_skills": ["Python", "Kubernetes"]}},
    )

    written = await workshop_service.write(
        db_session, vacancy.vacancy_id, router=NoProviderRouter()
    )
    repeated = await workshop_service.write(
        db_session, vacancy.vacancy_id, router=NoProviderRouter()
    )

    assert written.saved is True
    assert written.from_model is False
    assert written.text
    # A second click costs nothing and does not overwrite a letter somebody may
    # have edited by hand — the same idempotence the connectors follow.
    assert repeated.saved is False
    assert repeated.skipped == "letter_exists"


async def test_a_saved_letter_records_the_rules_that_judged_it(
    db_session: AsyncSession, vacancies: VacancyRepository, profiles: ProfileRepository
) -> None:
    """Written by the same call that writes the text, so the two cannot drift."""
    await profiles.create(make_profile())
    await db_session.flush()
    vacancy = await vacancies.upsert_by_external_id(
        make_vacancy("workshop-2"),
        source_slug="hh",
        external_id="hh-workshop-2",
        url="https://e.test/workshop-2",
        raw={"_derived": {"key_skills": ["Python"]}},
    )

    await workshop_service.write(db_session, vacancy.vacancy_id, router=NoProviderRouter())
    documents = await documents_service.build(db_session)

    assert [letter.rules_version for letter in documents.letters] == [RULES_VERSION]


async def test_the_queue_shows_the_vacancies_that_already_have_a_letter(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """A screen and a batch job want opposite defaults here.

    The batch skips what it has written so a repeated run costs nothing; the
    queue shows it, flagged, because "this one is done" is what somebody
    looking at a queue needs to see rather than a row that silently vanished.
    """
    profile = await a_profile(profiles)
    written = await a_vacancy(vacancies, "queue-written")
    waiting = await a_vacancy(vacancies, "queue-waiting")
    await matches.bulk_upsert(
        [make_match(profile.id, written, 90), make_match(profile.id, waiting, 88)]
    )
    await _application(db_session, written, cover_letter="x" * 300)

    queued = {item.vacancy_id: item for item in await workshop_service.queue(db_session)}

    assert queued[written].has_letter is True
    assert queued[waiting].has_letter is False


async def test_the_documents_screen_answers_over_http(
    async_client: AsyncClient,
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
) -> None:
    """The whole screen in one request: resumes with their audit, and letters."""
    await a_profile(profiles)
    vacancy_id = await a_vacancy(vacancies, "docs-http")
    await _application(
        db_session, vacancy_id, cover_letter="c" * 300, letter_rules_version=RULES_VERSION
    )

    response = await async_client.get("/api/v1/documents/overview")

    assert response.status_code == 200
    body = response.json()
    assert len(body["resumes"]) == 1
    assert [letter["rules_version"] for letter in body["letters"]] == [RULES_VERSION]
    assert body["current_rules_version"] == RULES_VERSION


async def test_the_letter_queue_answers_over_http(
    async_client: AsyncClient,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """What the workshop offers to write next, best score first."""
    profile = await a_profile(profiles)
    top = await a_vacancy(vacancies, "queue-http-top", title="Python Developer")
    low = await a_vacancy(vacancies, "queue-http-low", title="Сварщик")
    await matches.bulk_upsert([make_match(profile.id, top, 95), make_match(profile.id, low, 20)])

    response = await async_client.get("/api/v1/documents/queue")

    assert response.status_code == 200
    # The low one is below the floor the batch writer uses, so offering it here
    # would be offering a letter the tool this screen fronts would not write.
    assert [item["title"] for item in response.json()] == ["Python Developer"]


async def test_the_card_answers_over_http(
    async_client: AsyncClient,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """The row expanded, with the requirement split the screen is built around."""
    profile = await a_profile(profiles, skills=("python",))
    vacancy_id = await a_vacancy(vacancies, "card-http")
    await matches.bulk_upsert(
        [make_match(profile.id, vacancy_id, 70, matched=("python",), missing_required=("scala",))]
    )

    response = await async_client.get(f"/api/v1/vacancies/{vacancy_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["match"]["score"] == "70.00"
    assert [row["canonical_name"] for row in body["requirements"]["absent"]] == ["scala"]
    # The link the crawler actually read travels with the vacancy: for hh it is a
    # regional subdomain and cannot be rebuilt from an id.
    assert body["vacancy"]["sources"][0]["url"].startswith("https://")
