"""The queue endpoint: the wire the agent parses, and the rows it writes back.

Four things are defended here, because each of them breaks silently.

**The wire shape.** ``agent/queue.py`` is the contract of record and it is
strict: it rejects an item whose ``vacancy_id`` is not a digit string, and it
rejects an item whose ``url`` names a different vacancy — for the whole batch,
not just that item. So the tests below read the agent's own source with ``ast``
and check that every key it reaches for is a field this backend serves. Reading
a file is not importing it: ``backend/`` must not import ``agent`` and
``agent/tests/test_isolation.py`` enforces that by parsing the import graph, so
the drift guard has to be a parser too.

**The URL is served, not rebuilt.** For hh the stored address is a regional
subdomain, because the crawler walks ``almaty.hh.kz``'s own sitemap. That URL is
what the agent opens and what the human reads on the confirmation card, so a
test pins it against the row rather than against a pattern.

**Idempotency.** ``application`` has no unique constraint on ``vacancy_id`` and
must not grow a second row when a result is posted twice — which happens for
real whenever a run dies between sending and reporting.

**The token is the whole of the authentication, and an unset one is a refusal.**
Not a warning, not an open queue.
"""

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import uuid7
from app.db.enums import ApplicationStatus, MatchBucket
from app.db.models import Application
from app.db.repositories import MatchRepository, ProfileRepository, VacancyRepository
from app.schemas.agent import (
    CONTRACT_VERSION,
    AgentStatus,
    ApplicationResult,
    MatchExplanation,
    QueueItem,
)
from app.schemas.match import MatchedSkill, MissingSkill
from app.services import agent_queue
from factories import make_match, make_profile, make_vacancy

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_QUEUE = REPO_ROOT / "agent" / "queue.py"
AGENT_STATE = REPO_ROOT / "agent" / "state.py"

QUEUE_URL = f"{settings.api_v1_prefix}/applications/queue"
RESULTS_URL = f"{settings.api_v1_prefix}/applications/results"

#: A token that exists only inside this module. Never a real one, never a
#: default in the code: CLAUDE.md rule 4 puts secrets in the environment only.
TEST_TOKEN = "local-agent-token-for-tests-only"
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}

#: The shape hh actually serves: a city subdomain, and the id in the path.
HH_ID = "136773120"
HH_URL = f"https://almaty.hh.kz/vacancy/{HH_ID}"


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the local token for the duration of one test."""
    monkeypatch.setattr(settings, "agent_api_token", SecretStr(TEST_TOKEN))


# ── the contract, pinned against the agent's own source ───────────────


def _keys_read_from_payload(source: str, class_name: str, method: str) -> set[str]:
    """Every ``payload.get("x")`` key one method of one class reaches for."""
    tree = ast.parse(source)
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for member in node.body:
            if not isinstance(member, ast.FunctionDef) or member.name != method:
                continue
            for call in ast.walk(member):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "get"
                    and call.args
                    and isinstance(call.args[0], ast.Constant)
                    and isinstance(call.args[0].value, str)
                ):
                    keys.add(call.args[0].value)
    return keys


def _dict_keys_returned(source: str, class_name: str, method: str) -> set[str]:
    """The string keys of every dict literal one method builds."""
    tree = ast.parse(source)
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for member in node.body:
            if not isinstance(member, ast.FunctionDef) or member.name != method:
                continue
            for mapping in ast.walk(member):
                if not isinstance(mapping, ast.Dict):
                    continue
                keys.update(
                    key.value
                    for key in mapping.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                )
    return keys


def _enum_values(source: str, class_name: str) -> set[str]:
    """The string values assigned inside one enum class."""
    tree = ast.parse(source)
    values: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for member in node.body:
            if (
                isinstance(member, ast.Assign)
                and isinstance(member.value, ast.Constant)
                and isinstance(member.value.value, str)
            ):
                values.add(member.value.value)
    return values


@pytest.mark.unit
def test_every_field_the_agent_reads_off_a_queue_item_is_one_this_backend_serves() -> None:
    """A key the agent looks for and we never send is a silent default.

    ``QueueItem.from_json`` reads by key, so a rename here does not raise
    anywhere — it quietly turns ``closed_for_applicants`` into ``False`` and the
    prefilter into a no-op.
    """
    expected = _keys_read_from_payload(
        AGENT_QUEUE.read_text(encoding="utf-8"), "QueueItem", "from_json"
    )

    assert expected, "the agent's QueueItem.from_json no longer reads keys by name"
    assert expected <= set(QueueItem.model_fields)


@pytest.mark.unit
def test_every_field_the_agent_reports_is_one_this_backend_accepts() -> None:
    """A result field we reject is a 422 on something that already happened."""
    sent = _dict_keys_returned(AGENT_QUEUE.read_text(encoding="utf-8"), "Result", "to_json")

    assert sent, "the agent's Result.to_json no longer builds a dict literal"
    assert sent <= set(ApplicationResult.model_fields)


@pytest.mark.unit
def test_the_agent_status_set_has_not_drifted() -> None:
    """Two copies of one state machine, because the packages must not import
    each other. This is the thing that stops them disagreeing."""
    assert _enum_values(AGENT_STATE.read_text(encoding="utf-8"), "Status") == {
        status.value for status in AgentStatus
    }


@pytest.mark.unit
def test_the_contract_version_matches_the_agents() -> None:
    """The agent refuses a queue whose version it does not recognise."""
    source = AGENT_QUEUE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    declared = [
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "CONTRACT_VERSION"
        and isinstance(node.value, ast.Constant)
    ]

    assert declared == [CONTRACT_VERSION]


@pytest.mark.unit
def test_the_explanation_says_nothing_rather_than_inventing_a_reason() -> None:
    """A sentence built from an empty match would read as an explanation and be
    one, on the card that decides whether a letter is sent."""
    empty = MatchExplanation(score=Decimal("80"), bucket=MatchBucket.STRONG)

    assert agent_queue.render_explanation(empty) is None


@pytest.mark.unit
def test_the_explanation_names_what_is_missing_and_stays_on_one_line() -> None:
    """The half of the score a person can act on."""
    rendered = agent_queue.render_explanation(
        MatchExplanation(
            score=Decimal("72"),
            bucket=MatchBucket.STRONG,
            verdict="подходит по стеку",
            matched_skills=[MatchedSkill(canonical_name="python", coverage=Decimal("1.0"))],
            missing_required=[MissingSkill(canonical_name="kubernetes", weight=Decimal("1.0"))],
            missing_nice=[MissingSkill(canonical_name="docker", weight=Decimal("0.5"))],
            red_flags=["зарплата не указана"],
            experience_gap_years=Decimal("2.0"),
        )
    )

    assert rendered is not None
    assert "python" in rendered
    assert "kubernetes" in rendered
    assert "зарплата не указана" in rendered
    assert rendered.startswith("подходит по стеку")
    assert "docker" in rendered
    assert "2" in rendered
    assert "\n" not in rendered
    assert len(rendered) <= agent_queue.MAX_EXPLANATION


# ── the URL, which is the field with the sharpest edge ────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url", "external_id", "accepted"),
    [
        (HH_URL, HH_ID, True),
        (f"{HH_URL}/", HH_ID, True),
        # The id in a query string is not the page the browser will open.
        (f"https://almaty.hh.kz/vacancy/999?backurl=/vacancy/{HH_ID}", HH_ID, False),
        (f"https://almaty.hh.kz/vacancy/{HH_ID}9", HH_ID, False),
        (f"http://almaty.hh.kz/vacancy/{HH_ID}", HH_ID, False),
        (f"/vacancy/{HH_ID}", HH_ID, False),
        (f"https:///vacancy/{HH_ID}", HH_ID, False),
    ],
)
def test_a_served_url_must_name_its_own_vacancy(url: str, external_id: str, accepted: bool) -> None:
    """The agent rejects the whole batch on this mismatch, so we check first."""
    assert agent_queue._url_names(url, external_id) is accepted


@pytest.mark.unit
def test_one_unreadable_skill_entry_does_not_empty_the_explanation() -> None:
    """The annotation on a JSONB column is a promise the database does not keep,
    and those lists were last written by the scorer."""
    parsed = agent_queue._models(
        MissingSkill,
        [{"canonical_name": "kubernetes", "weight": 1.0}, {"weight": "not a number"}, "prose"],
    )

    assert [skill.canonical_name for skill in parsed] == ["kubernetes"]


# ── what comes back ───────────────────────────────────────────────────


@pytest.mark.unit
def test_hh_last_state_is_a_string_because_the_set_is_hhs_and_open() -> None:
    """``DISCARD`` was observed live. Enumerating the rest would turn hh adding
    a state into a 422 on a result that has already happened in the world."""
    for state in ("DISCARD", "INVITATION", "SOMETHING_HH_ADDS_IN_2027"):
        result = ApplicationResult(vacancy_id=HH_ID, status=AgentStatus.SENT, last_state=state)
        assert result.last_state == state


@pytest.mark.unit
def test_an_unknown_agent_status_is_refused() -> None:
    """Unlike hh's states, this set is ours: anything outside it is our bug."""
    with pytest.raises(ValidationError):
        ApplicationResult.model_validate({"vacancy_id": HH_ID, "status": "half-sent"})


@pytest.mark.unit
def test_both_of_hhs_warnings_are_kept_apart_in_the_note() -> None:
    """They mean opposite things: one stopped the send, the other did not."""
    rendered = agent_queue.render_outcome(
        ApplicationResult(
            vacancy_id=HH_ID,
            status=AgentStatus.NEEDS_MANUAL,
            hh_blocking_warning="Чтобы откликнуться, откройте резюме работодателям.",
            hh_warning="Такой отклик может получить отказ: требуется английский C1.",
            negotiations_total=1,
            last_state="DISCARD",
        )
    )

    assert "Чтобы откликнуться" in rendered
    assert "может получить отказ" in rendered
    assert "negotiations.total): 1" in rendered
    assert "lastState): DISCARD" in rendered
    # Both sentences on their own labelled lines, so a column can be
    # backfilled from this later by parsing rather than by guessing.
    assert len(rendered.splitlines()) == 6


@pytest.mark.unit
def test_the_agents_block_replaces_itself_and_leaves_a_persons_notes_alone() -> None:
    """Re-posting a result must not append a second copy of it."""
    human = "Позвонить рекрутеру во вторник."
    once = agent_queue.merge_notes(human, agent_queue.render_outcome(_sent()))
    twice = agent_queue.merge_notes(once, agent_queue.render_outcome(_sent()))

    assert once == twice
    assert twice.startswith(human)
    assert twice.count(agent_queue.NOTES_MARKER) == 1


def _sent(vacancy_id: str = HH_ID) -> ApplicationResult:
    """A plain successful send."""
    return ApplicationResult(vacancy_id=vacancy_id, status=AgentStatus.SENT, negotiations_total=1)


# ── the token ─────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_an_unset_token_refuses_to_serve_rather_than_serving_openly(
    client_without_db: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing secret must fail loudly. An open letter queue is worse than
    an agent that cannot reach the backend."""
    monkeypatch.setattr(settings, "agent_api_token", None)

    response = await client_without_db.get(QUEUE_URL, headers=AUTH)

    assert response.status_code == 503
    assert "AGENT_API_TOKEN" in response.json()["detail"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": TEST_TOKEN},
        {"Authorization": "Basic bG9jYWw6bG9jYWw="},
    ],
)
async def test_the_queue_is_unreachable_without_the_configured_token(
    client_without_db: AsyncClient, token: None, headers: dict[str, str]
) -> None:
    """Anything else on this host must not read the owner's letters."""
    response = await client_without_db.get(QUEUE_URL, headers=headers)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.unit
async def test_results_are_refused_without_the_token_too(
    client_without_db: AsyncClient, token: None
) -> None:
    """The write side is the half that changes the tracker."""
    response = await client_without_db.post(
        RESULTS_URL, json={"version": CONTRACT_VERSION, "results": []}
    )

    assert response.status_code == 401


# ── the database half ─────────────────────────────────────────────────


async def _posting(
    vacancies: VacancyRepository,
    *,
    seed: str,
    external_id: str,
    url: str,
    derived: dict[str, Any] | None = None,
    slug: str = "hh",
) -> UUID:
    """One vacancy with one source row, shaped the way the hh crawler leaves it."""
    result = await vacancies.upsert_by_external_id(
        make_vacancy(seed),
        source_slug=slug,
        external_id=external_id,
        url=url,
        raw={"_derived": {"external_id": external_id, "url": url, **(derived or {})}},
    )
    return result.vacancy_id


def _letter(session: AsyncSession, vacancy_id: UUID, text: str = "Здравствуйте!") -> None:
    """The row the letter generation would have left behind."""
    session.add(Application(id=uuid7(), vacancy_id=vacancy_id, cover_letter=text))


@pytest.mark.db
async def test_the_queue_serves_the_stored_regional_url_and_the_sources_own_id(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """The agent opens exactly this URL and the human reads it on the card.

    Rebuilding it from the id against a bare host would move both to a page hh
    redirects, and the redirect is the thing ``agent/hosts.py`` had to be
    written to survive.
    """
    profile = await profiles.create(make_profile())
    vacancy_id = await _posting(
        vacancies,
        seed="queue-served",
        external_id=HH_ID,
        url=HH_URL,
        derived={"closed_for_applicants": True, "anonymous": True},
    )
    await matches.bulk_upsert(
        [make_match(profile.id, vacancy_id, Decimal("91"), missing_required=["kubernetes"])]
    )
    _letter(db_session, vacancy_id)
    await db_session.flush()

    queue = await agent_queue.build_queue(db_session, limit=10, profile_id=profile.id)

    assert len(queue.items) == 1
    item = queue.items[0]
    assert item.url == HH_URL
    assert item.vacancy_id == HH_ID
    assert item.letter == "Здравствуйте!"
    assert item.closed_for_applicants is True
    assert item.anonymous is True
    assert item.source == "hh"
    assert item.match is not None
    assert item.match.score == Decimal("91.00")
    # The number alone is not what the person on the card needs, so the flat
    # pair the agent prints carries the reason as well as the score.
    assert item.score == Decimal("91.00")
    assert item.score_explanation is not None
    assert "kubernetes" in item.score_explanation
    assert [skill.canonical_name for skill in item.match.missing_required] == ["kubernetes"]


@pytest.mark.db
async def test_a_posting_whose_url_disagrees_with_its_id_is_dropped_not_served(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """The agent rejects the entire batch on that mismatch, so one bad row must
    not cost every good row behind it."""
    profile = await profiles.create(make_profile())
    broken = await _posting(
        vacancies,
        seed="queue-broken",
        external_id="111111111",
        url="https://almaty.hh.kz/vacancy/222222222",
    )
    good = await _posting(vacancies, seed="queue-good", external_id=HH_ID, url=HH_URL)
    await matches.bulk_upsert(
        [
            make_match(profile.id, broken, Decimal("99")),
            make_match(profile.id, good, Decimal("80")),
        ]
    )
    _letter(db_session, broken)
    _letter(db_session, good)
    await db_session.flush()

    queue = await agent_queue.build_queue(db_session, limit=10, profile_id=profile.id)

    assert [item.vacancy_id for item in queue.items] == [HH_ID]


@pytest.mark.db
async def test_one_vacancy_cross_posted_twice_is_offered_once(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """Two hh ads deduplicated onto one fingerprint are one job, and applying
    to it twice is the mistake the whole design is arranged against."""
    profile = await profiles.create(make_profile())
    vacancy_id = await _posting(vacancies, seed="queue-twin", external_id=HH_ID, url=HH_URL)
    twin = await _posting(
        vacancies,
        seed="queue-twin",
        external_id="900000001",
        url="https://astana.hh.kz/vacancy/900000001",
    )
    await matches.bulk_upsert([make_match(profile.id, vacancy_id, Decimal("88"))])
    _letter(db_session, vacancy_id)
    await db_session.flush()

    queue = await agent_queue.build_queue(db_session, limit=10, profile_id=profile.id)

    assert twin == vacancy_id, "the fixture is meant to produce one vacancy, not two"
    assert len(queue.items) == 1


@pytest.mark.db
async def test_the_queue_offers_nothing_a_person_or_a_run_already_acted_on(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """Offering it again is how a second application gets sent, and a second
    application is the one mistake the owner cannot undo."""
    profile = await profiles.create(make_profile())
    vacancy_id = await _posting(vacancies, seed="queue-acted", external_id=HH_ID, url=HH_URL)
    await matches.bulk_upsert([make_match(profile.id, vacancy_id, Decimal("88"))])
    _letter(db_session, vacancy_id)
    await db_session.flush()

    before = await agent_queue.build_queue(db_session, limit=10, profile_id=profile.id)
    await agent_queue.record_results(db_session, [_sent()])
    after = await agent_queue.build_queue(db_session, limit=10, profile_id=profile.id)

    assert len(before.items) == 1
    assert after.items == []


@pytest.mark.db
async def test_an_item_without_a_letter_is_held_back_unless_asked_for(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """The agent refuses to send without one, so serving it wastes a slot."""
    profile = await profiles.create(make_profile())
    vacancy_id = await _posting(vacancies, seed="queue-letterless", external_id=HH_ID, url=HH_URL)
    await matches.bulk_upsert([make_match(profile.id, vacancy_id, Decimal("88"))])
    await db_session.flush()

    default = await agent_queue.build_queue(db_session, limit=10, profile_id=profile.id)
    forced = await agent_queue.build_queue(
        db_session, limit=10, profile_id=profile.id, require_letter=False
    )

    assert default.items == []
    assert [item.vacancy_id for item in forced.items] == [HH_ID]
    assert forced.items[0].letter is None


@pytest.mark.db
async def test_a_score_below_the_floor_is_not_worth_a_slot(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
) -> None:
    """The daily budget is small on purpose."""
    profile = await profiles.create(make_profile())
    vacancy_id = await _posting(vacancies, seed="queue-weak", external_id=HH_ID, url=HH_URL)
    await matches.bulk_upsert([make_match(profile.id, vacancy_id, Decimal("40"))])
    _letter(db_session, vacancy_id)
    await db_session.flush()

    filtered = await agent_queue.build_queue(db_session, limit=10, profile_id=profile.id)
    asked = await agent_queue.build_queue(
        db_session, limit=10, profile_id=profile.id, min_score=Decimal("10")
    )

    assert filtered.items == []
    assert len(asked.items) == 1


@pytest.mark.db
async def test_the_same_result_posted_twice_leaves_one_row(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """A run that dies between sending and reporting gets re-reported by hand."""
    vacancy_id = await _posting(vacancies, seed="results-twice", external_id=HH_ID, url=HH_URL)

    first = await agent_queue.record_results(db_session, [_sent()])
    second = await agent_queue.record_results(db_session, [_sent()])
    rows = await db_session.scalar(
        select(func.count()).select_from(Application).where(Application.vacancy_id == vacancy_id)
    )

    assert rows == 1
    assert first.results[0].created is True
    assert second.results[0].created is False
    assert first.results[0].application_id == second.results[0].application_id


@pytest.mark.db
async def test_a_result_lands_on_the_row_the_letter_created(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """No row is created when the letter already made one — that is the whole
    argument for the queue writing nothing."""
    vacancy_id = await _posting(vacancies, seed="results-letter", external_id=HH_ID, url=HH_URL)
    _letter(db_session, vacancy_id)
    await db_session.flush()

    response = await agent_queue.record_results(db_session, [_sent()])
    application = await db_session.scalar(
        select(Application).where(Application.vacancy_id == vacancy_id)
    )

    assert response.results[0].created is False
    assert application is not None
    assert application.cover_letter == "Здравствуйте!"
    assert application.status is ApplicationStatus.APPLIED
    assert application.applied_at is not None


@pytest.mark.db
async def test_a_later_result_never_demotes_a_row_a_person_moved_on(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """The tracker is the person's. This endpoint only ever moves saved to
    applied; a row already at interview stays there."""
    vacancy_id = await _posting(vacancies, seed="results-demote", external_id=HH_ID, url=HH_URL)
    application = Application(id=uuid7(), vacancy_id=vacancy_id, status=ApplicationStatus.INTERVIEW)
    db_session.add(application)
    await db_session.flush()

    await agent_queue.record_results(
        db_session,
        [ApplicationResult(vacancy_id=HH_ID, status=AgentStatus.FAILED, reason="сеть отвалилась")],
    )
    await db_session.refresh(application)

    assert application.status is ApplicationStatus.INTERVIEW
    assert application.notes is not None
    assert "сеть отвалилась" in application.notes


@pytest.mark.db
async def test_hh_own_analysis_of_the_application_is_persisted(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """hh naming one unmet requirement is more specific than any similarity
    score this project computes, so it is kept rather than logged and dropped."""
    vacancy_id = await _posting(vacancies, seed="results-warning", external_id=HH_ID, url=HH_URL)

    await agent_queue.record_results(
        db_session,
        [
            ApplicationResult(
                vacancy_id=HH_ID,
                status=AgentStatus.SENT,
                hh_warning="Такой отклик может получить отказ: нужен английский C1.",
                negotiations_total=1,
                last_state="DISCARD",
            )
        ],
    )
    notes = await db_session.scalar(
        select(Application.notes).where(Application.vacancy_id == vacancy_id)
    )

    assert notes is not None
    assert "английский C1" in notes
    assert "DISCARD" in notes


@pytest.mark.db
async def test_a_result_for_a_posting_we_do_not_know_writes_nothing(
    db_session: AsyncSession,
) -> None:
    """An id we never served has no vacancy to hang a tracker row on, and
    inventing one would put a row in the tracker for a job nobody has."""
    response = await agent_queue.record_results(db_session, [_sent("999999999")])
    rows = await db_session.scalar(select(func.count()).select_from(Application))

    assert rows == 0
    assert response.accepted == 0
    assert response.unknown == ["999999999"]
    assert response.results[0].accepted is False


@pytest.mark.db
async def test_an_empty_queue_is_an_answer_when_no_profile_is_active(
    db_session: AsyncSession,
) -> None:
    """Nothing to match against is not an error; it is an empty batch."""
    queue = await agent_queue.build_queue(db_session, limit=10)

    assert queue.items == []
    assert queue.version == CONTRACT_VERSION


@pytest.mark.db
async def test_a_queue_for_an_unknown_profile_is_empty_rather_than_everybodys(
    db_session: AsyncSession,
) -> None:
    """A wrong id must not fall back to the active profile's letters."""
    queue = await agent_queue.build_queue(db_session, limit=10, profile_id=uuid4())

    assert queue.items == []


# ── over HTTP, which is the only way the agent ever gets here ─────────


@pytest.mark.db
async def test_the_endpoints_answer_the_shape_the_agent_parses(
    async_client: AsyncClient,
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    matches: MatchRepository,
    token: None,
) -> None:
    """One round trip: take a queue, report a result, see the row move."""
    profile = await profiles.create(make_profile())
    vacancy_id = await _posting(vacancies, seed="http-round", external_id=HH_ID, url=HH_URL)
    await matches.bulk_upsert([make_match(profile.id, vacancy_id, Decimal("93"))])
    _letter(db_session, vacancy_id)
    await db_session.flush()

    taken = await async_client.get(QUEUE_URL, params={"limit": 5}, headers=AUTH)
    reported = await async_client.post(
        RESULTS_URL,
        headers=AUTH,
        json={
            "version": CONTRACT_VERSION,
            "results": [{"vacancy_id": HH_ID, "status": "sent", "negotiations_total": 1}],
        },
    )
    application = await db_session.scalar(
        select(Application).where(Application.vacancy_id == vacancy_id)
    )

    assert taken.status_code == 200
    body = taken.json()
    assert body["version"] == CONTRACT_VERSION
    assert body["items"][0]["url"] == HH_URL
    assert body["items"][0]["vacancy_id"] == HH_ID
    assert reported.status_code == 200
    assert reported.json()["accepted"] == 1
    assert application is not None
    assert application.status is ApplicationStatus.APPLIED


@pytest.mark.db
async def test_a_contract_version_the_backend_does_not_speak_is_refused(
    async_client: AsyncClient, token: None
) -> None:
    """Guessing at a payload from a different contract is worse than saying no."""
    response = await async_client.post(
        RESULTS_URL, headers=AUTH, json={"version": CONTRACT_VERSION + 1, "results": []}
    )

    assert response.status_code == 409
