"""The two buttons, as HTTP: what they return, and what they refuse to return.

The endpoints exist so a vacancy card can offer «CV под эту вакансию» and
«Сопроводительное», so the tests are about the three things a screen built on
them can get wrong:

* **a withheld document must not look like a delivered one.** The response
  carries ``delivered`` and either a document or a reason, and a client that
  ignores the flag has to find no file and no download id rather than a file it
  should not have had.
* **the report travels with the document.** The brief requires it, and a client
  cannot render one without having been handed the other, so the response type
  carries both or neither.
* **the versions accumulate.** Pressing the button twice has to produce two
  readable versions, because the reason a person presses it again is to see what
  changed.

There is no send endpoint here and there is not going to be one: an application
is sent by ``agent/``, from a browser, under the user's own account, after a
human has confirmed it. A test asserts that too, because "we simply never added
it" is not a guarantee.

The router is faked throughout — these tests are about the HTTP layer, and the
generator has its own file.
"""

from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PROBLEM_JSON
from app.db.base import uuid7
from app.db.models import VacancySkill
from app.db.repositories import MatchRepository, ProfileRepository, VacancyRepository
from app.db.session import get_session
from app.documents import service as document_service
from app.documents.render import MEDIA_TYPE
from app.schemas.profile import CandidateProfileCreate, ExperienceCreate, SkillCreate
from factories import make_match, make_vacancy

DOCUMENTS_URL = "/api/v1/documents"


@pytest_asyncio.fixture
async def client(app: FastAPI, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """A client whose requests share the test's transaction.

    The endpoints commit, and a committed row inside this session's savepoint is
    still rolled back at the end of the test — which is what lets a test assert
    on what was stored without leaving it behind.
    """

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def seed(
    profiles: ProfileRepository,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    session: AsyncSession,
    *,
    with_experience: bool = True,
) -> tuple[UUID, UUID]:
    """One profile and one scored vacancy, as a parse and a run would leave them."""
    created = await profiles.create(
        CandidateProfileCreate(
            name="Нуржан Сатыбалдиев",
            headline="Backend Developer",
            locations=["Алматы"],
            raw_text="Нуржан Сатыбалдиев\n+7 701 234 56 78 · nurzhan@example.com\n",
            education=[{"institution": "КазНУ", "degree": "Бакалавр", "end_year": 2021}],
            skills=[
                SkillCreate(canonical_name=name, raw_names=[name.title()])
                for name in ("python", "postgresql", "fastapi", "docker", "redis", "git")
            ],
            experience=(
                [
                    ExperienceCreate(
                        position=0,
                        company="Chocofamily",
                        title="Backend Developer",
                        start="2023-04",
                        is_current=True,
                        stack=["Python", "FastAPI", "PostgreSQL"],
                    )
                ]
                if with_experience
                else []
            ),
        )
    )
    upserted = await vacancies.upsert_by_external_id(
        make_vacancy("docs-api-1", title="Backend-разработчик", company="Kaspi"),
        source_slug="hh",
        external_id="hh-docs-1",
        url="https://hh.kz/vacancy/1",
        raw={"_derived": {"accredited_it_employer": True, "responses_count": 5}},
    )
    session.add_all(
        [
            VacancySkill(id=uuid7(), vacancy_id=upserted.vacancy_id, canonical_name=name)
            for name in ("Python", "PostgreSQL", "Kubernetes")
        ]
    )
    await matches.bulk_upsert([make_match(created.id, upserted.vacancy_id, Decimal("88"))])
    await session.flush()
    return created.id, upserted.vacancy_id


@pytest.fixture
def offline_router(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No provider is available, so every generation takes the rule-based branch.

    Deliberate: these tests are about the HTTP contract, and the rule-based
    branch produces a real document without a model, a network call, or a
    scripted answer that would have to be kept in step with the prompt.

    **The teardown is the important half.** ``app.llm.router`` holds a
    process-wide singleton, so building it while ``build_providers`` is faked
    leaves every later test in the session talking to a router with one crippled
    provider in it — and the symptom is a ``KeyError`` in an unrelated file about
    a provider this fixture never mentioned. ``monkeypatch`` restores the
    function; only ``reset_router`` restores the object it already built, so it
    is called on both sides.
    """

    class Offline:
        def is_available(self) -> bool:
            return False

    from app.llm import router as router_module

    monkeypatch.setattr(router_module, "build_providers", lambda: {"cli": Offline()})
    monkeypatch.setattr(
        router_module.settings, "llm_fallback_chain", {"cli": [], "api": [], "ollama": []}
    )
    monkeypatch.setattr(
        router_module.settings,
        "llm_routing",
        dict.fromkeys(router_module.settings.llm_routing, "cli"),
    )
    router_module.reset_router()
    try:
        yield
    finally:
        router_module.reset_router()


# ── generating ────────────────────────────────────────────────────────


async def test_generating_a_cv_answers_with_the_document_and_its_audit(
    client: AsyncClient,
    db_session: AsyncSession,
    profiles: ProfileRepository,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    offline_router: None,
) -> None:
    """The report travels with the document, because neither is useful alone."""
    _, vacancy_id = await seed(profiles, vacancies, matches, db_session)

    response = await client.post(f"{DOCUMENTS_URL}/cv/{vacancy_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["delivered"] is True
    assert body["version"] == 1
    assert body["document_id"] is not None
    assert body["file_format"] == "docx"
    assert body["review"]["ats"]["overall"] in {"ok", "degraded"}
    assert body["review"]["coverage"]["not_held"] == ["Kubernetes"]
    assert "Kubernetes" not in body["text"]


async def test_pressing_the_button_twice_produces_two_versions(
    client: AsyncClient,
    db_session: AsyncSession,
    profiles: ProfileRepository,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    offline_router: None,
) -> None:
    """The reason a person regenerates is to see what changed, so nothing is lost."""
    _, vacancy_id = await seed(profiles, vacancies, matches, db_session)

    first = await client.post(f"{DOCUMENTS_URL}/cv/{vacancy_id}")
    second = await client.post(f"{DOCUMENTS_URL}/cv/{vacancy_id}")
    history = await client.get(f"{DOCUMENTS_URL}/versions/{vacancy_id}/cv")

    assert [first.json()["version"], second.json()["version"]] == [1, 2]
    assert [row["version"] for row in history.json()] == [1, 2]
    assert first.json()["document_id"] != second.json()["document_id"]


async def test_every_stored_version_records_the_rules_it_was_written_under(
    client: AsyncClient,
    db_session: AsyncSession,
    profiles: ProfileRepository,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    offline_router: None,
) -> None:
    """ "Why is version 3 different" is usually "because the rules changed", and
    that has to be answerable from the rows rather than from memory.

    ``workshop:`` rather than ``builtin:`` since the merge: the set a CV is held
    to is the owner's own rules plus the structural guarantees, so the value
    moves when they edit a rule and not only when a constant in this repository
    changes. See ``app/documents/rules.py``.
    """
    _, vacancy_id = await seed(profiles, vacancies, matches, db_session)
    await client.post(f"{DOCUMENTS_URL}/cv/{vacancy_id}")

    history = await client.get(f"{DOCUMENTS_URL}/versions/{vacancy_id}/cv")

    assert history.json()[0]["rules_version"].startswith("workshop:")


async def test_a_withheld_document_carries_no_file_and_says_why(
    client: AsyncClient,
    db_session: AsyncSession,
    profiles: ProfileRepository,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    offline_router: None,
) -> None:
    """A refusal must not be renderable as a success.

    A profile with no stored jobs is the real case here: every profile parsed
    before migration 0010 is in exactly this state, and the reason names the one
    action that fixes it.
    """
    _, vacancy_id = await seed(profiles, vacancies, matches, db_session, with_experience=False)

    response = await client.post(f"{DOCUMENTS_URL}/cv/{vacancy_id}")

    body = response.json()
    assert response.status_code == 200
    assert body["delivered"] is False
    assert body["document_id"] is None
    assert body["text"] is None
    assert body["reason"] == "no_experience"
    assert body["reason_ru"]
    assert body["hard_rules"]


async def test_generating_without_a_profile_is_a_404_that_says_what_to_do(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The buttons live on a vacancy, and a vacancy screen is reachable before
    any resume has been uploaded."""
    response = await client.post(f"{DOCUMENTS_URL}/cv/{uuid4()}")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert "resume" in response.json()["detail"]


async def test_generating_for_a_vacancy_that_is_gone_says_so_rather_than_failing(
    client: AsyncClient,
    db_session: AsyncSession,
    profiles: ProfileRepository,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    offline_router: None,
) -> None:
    """A posting can be deleted between the screen being drawn and the click."""
    await seed(profiles, vacancies, matches, db_session)

    response = await client.post(f"{DOCUMENTS_URL}/cv/{uuid4()}")

    assert response.status_code == 200
    assert response.json()["reason"] == "vacancy_not_found"


# ── downloading ───────────────────────────────────────────────────────


async def test_a_stored_version_downloads_as_a_docx(
    client: AsyncClient,
    db_session: AsyncSession,
    profiles: ProfileRepository,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    offline_router: None,
) -> None:
    """The file is rebuilt from the arrangement; the response is a real .docx."""
    _, vacancy_id = await seed(profiles, vacancies, matches, db_session)
    document_id = (await client.post(f"{DOCUMENTS_URL}/cv/{vacancy_id}")).json()["document_id"]

    response = await client.get(f"{DOCUMENTS_URL}/{document_id}/file")

    assert response.status_code == 200
    assert response.headers["content-type"] == MEDIA_TYPE
    assert response.content[:2] == b"PK"


async def test_a_cyrillic_filename_survives_the_content_disposition_header(
    client: AsyncClient,
    db_session: AsyncSession,
    profiles: ProfileRepository,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    offline_router: None,
) -> None:
    """Without the extended form a CV called «Нуржан-Kaspi.docx» arrives as
    a row of question marks, which is what every file on this market would."""
    _, vacancy_id = await seed(profiles, vacancies, matches, db_session)
    document_id = (await client.post(f"{DOCUMENTS_URL}/cv/{vacancy_id}")).json()["document_id"]

    response = await client.get(f"{DOCUMENTS_URL}/{document_id}/file")

    disposition = response.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition
    assert "%D0" in disposition  # percent-encoded Cyrillic


async def test_downloading_something_that_was_never_generated_is_a_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A document id that names nothing is not an empty file."""
    response = await client.get(f"{DOCUMENTS_URL}/{uuid4()}/file")

    assert response.status_code == 404


# ── listing ───────────────────────────────────────────────────────────


async def test_the_candidates_list_carries_the_counts_the_buttons_need(
    client: AsyncClient,
    db_session: AsyncSession,
    profiles: ProfileRepository,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    offline_router: None,
) -> None:
    """Zero means the button has never been pressed; one means pressing it again
    adds a version rather than replacing anything."""
    _, vacancy_id = await seed(profiles, vacancies, matches, db_session)

    before = (await client.get(f"{DOCUMENTS_URL}/candidates")).json()
    await client.post(f"{DOCUMENTS_URL}/cv/{vacancy_id}")
    after = (await client.get(f"{DOCUMENTS_URL}/candidates")).json()

    assert before[0]["cv_versions"] == 0
    assert after[0]["cv_versions"] == 1
    assert after[0]["letter_versions"] == 0


async def test_the_candidates_list_shows_what_the_employer_published(
    client: AsyncClient,
    db_session: AsyncSession,
    profiles: ProfileRepository,
    vacancies: VacancyRepository,
    matches: MatchRepository,
) -> None:
    """Four facts from the posting itself. Nothing is looked up anywhere."""
    await seed(profiles, vacancies, matches, db_session)

    rows = (await client.get(f"{DOCUMENTS_URL}/candidates")).json()

    assert rows[0]["employer"]["accredited_it_employer"] is True
    assert rows[0]["employer"]["responses_count"] == 5


async def test_the_documents_list_leaves_the_bodies_out(
    client: AsyncClient,
    db_session: AsyncSession,
    profiles: ProfileRepository,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    offline_router: None,
) -> None:
    """Twenty rows on a screen need a score and a date, not twenty CVs."""
    _, vacancy_id = await seed(profiles, vacancies, matches, db_session)
    await client.post(f"{DOCUMENTS_URL}/cv/{vacancy_id}")

    rows = (await client.get(DOCUMENTS_URL)).json()

    assert len(rows) == 1
    assert rows[0]["vacancy_title"] == "Backend-разработчик"
    assert "text" not in rows[0]
    assert rows[0]["ats_score"] > 0


async def test_the_hard_rules_are_readable_without_generating_anything(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The screen that explains a refusal needs the rules, not a second refusal."""
    response = await client.get(f"{DOCUMENTS_URL}/rules")

    assert response.status_code == 200
    assert len(response.json()) >= 4


# ── what these endpoints deliberately cannot do ───────────────────────


async def test_no_endpoint_here_can_send_an_application(app: FastAPI) -> None:
    """Sending needs a human at a keyboard, and a browser cannot give that
    guarantee — so the dashboard has no send button and this router has no
    endpoint behind one. Asserted rather than assumed: "we never added it" stops
    being true the first time somebody adds it."""
    paths = [path for path in app.openapi()["paths"] if path.startswith(DOCUMENTS_URL)]

    # Read off the OpenAPI document rather than the router, because that is the
    # contract a client is written against: an endpoint absent from here is one
    # nothing can be built on, whatever the router happens to hold.
    assert paths
    for path in paths:
        assert "send" not in path
        assert "apply" not in path


async def test_the_service_reports_a_reason_for_everything_it_refuses() -> None:
    """Every machine-readable reason has a sentence a person can read.

    A reason with no Russian beside it reaches the dashboard as a bare slug,
    which is how a refusal stops being actionable.
    """
    from app.documents.service import REASONS_RU

    assert all(text.strip() for text in REASONS_RU.values())
    assert set(REASONS_RU) >= {"no_experience", "vacancy_not_found", "not_machine_readable"}


def test_the_outcome_type_cannot_be_both_delivered_and_refused() -> None:
    """A refusal carries no document, and a delivery carries no reason.

    Not a runtime check — a shape one. Whatever a caller does with an outcome,
    it cannot render a file it was not given.
    """
    refused = document_service.DocumentOutcome(
        vacancy_id=uuid7(), kind=document_service.DocumentKind.CV, reason="no_experience"
    )

    assert not refused.delivered
    assert refused.document is None
    assert refused.stored_id is None
    assert refused.reason_ru is not None


def test_an_unknown_reason_falls_back_to_itself_rather_than_to_nothing() -> None:
    """A slug on the screen is bad; a blank space where the reason was is worse."""
    outcome = document_service.DocumentOutcome(
        vacancy_id=uuid7(), kind=document_service.DocumentKind.CV, reason="something_new"
    )

    assert outcome.reason_ru == "something_new"
