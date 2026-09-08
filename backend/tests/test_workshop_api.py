"""The workshop over HTTP: what can be stored, what is refused, what is unreachable.

The interesting assertions here are the negative ones.

A **built-in rule has no UUID**, so ``PATCH /rules/builtin:no_links`` is rejected
by the routing table before any handler runs. That is what "неудаляемое" means
in this design: not a check somebody remembered to write, but an address that
does not exist.

A **rule that would require a claim is refused with 422** and the names that
caused it, because "rejected" with no names is unactionable — the person has to
know which word was read as a claim about them.

An **upload is one document or the other**, never both and never neither, so
there is one place where the size floor and the extraction warnings live.

Every test runs against the real application and the real database. Only the
preview reaches a model, and its router is replaced with one that answers from a
script.
"""

from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from test_workshop import FILLER, FakeRouter, _draft

from app.db.models import Application, GenerationRule
from app.db.repositories import ProfileRepository, VacancyRepository
from factories import make_profile, make_vacancy

pytestmark = pytest.mark.db

RULES = "/api/v1/workshop/rules"
REFERENCES = "/api/v1/workshop/references"
PREVIEW = "/api/v1/workshop/preview"

#: Long enough to clear the floor below which a document is not an example of
#: anything. The content is irrelevant to every test that uses it.
A_LETTER = "Здравствуйте! Меня заинтересовала ваша вакансия. " * 6


def length_rule(**overrides: Any) -> dict[str, Any]:
    """The simplest rule there is: a ceiling in characters."""
    payload: dict[str, Any] = {
        "params": {"kind": "length", "unit": "characters", "maximum": 2000},
        "message": "Не длиннее 2000 знаков",
        "scope": "cover_letter",
        "severity": "hard",
    }
    payload.update(overrides)
    return payload


# ── rules ─────────────────────────────────────────────────────────────


async def test_the_built_in_rules_are_listed_and_marked_as_unremovable(
    async_client: AsyncClient,
) -> None:
    """A person is entitled to know which constraints they cannot lift, and why."""
    response = await async_client.get(RULES)

    assert response.status_code == 200
    builtin = [rule for rule in response.json() if rule["is_builtin"]]
    assert {rule["kind"] for rule in builtin} == {"no_links", "no_contact_handles"}
    assert all(rule["id"].startswith("builtin:") for rule in builtin)
    assert all("hh" in rule["message"] for rule in builtin)


async def test_a_built_in_rule_has_no_address_to_delete_it_at(
    async_client: AsyncClient,
) -> None:
    """ "Undeletable" as a property of the URL space rather than of a check.

    The path parameter is a UUID and ``builtin:no_links`` is not one, so the
    request never reaches a handler that could have got it wrong.
    """
    deleted = await async_client.delete(f"{RULES}/builtin:no_links")
    patched = await async_client.patch(f"{RULES}/builtin:no_links", json={"is_active": False})

    assert deleted.status_code == 422
    assert patched.status_code == 422


async def test_a_rule_is_stored_and_comes_back_with_what_the_model_will_be_asked(
    async_client: AsyncClient,
) -> None:
    """``asked_as`` beside the owner's own sentence, so the two can be compared.

    A rule that does not measure what its author meant is visible here rather
    than in a letter three weeks later.
    """
    created = await async_client.post(RULES, json=length_rule())

    assert created.status_code == 201
    body = created.json()
    assert body["kind"] == "length"
    assert body["is_builtin"] is False
    assert body["message"] == "Не длиннее 2000 знаков"
    assert body["asked_as"] == "the text must be at most 2000 characters long"

    listed = await async_client.get(RULES)
    assert [rule["id"] for rule in listed.json() if not rule["is_builtin"]] == [body["id"]]


async def test_the_owners_own_example_of_a_rule_goes_in_as_written(
    async_client: AsyncClient,
) -> None:
    """«В навыках не меньше 21 пункта» — the rule the whole feature exists for."""
    response = await async_client.post(
        RULES,
        json={
            "params": {"kind": "section_item_count", "section": "навыки", "minimum": 21},
            "message": "В навыках не меньше 21 пункта",
            "scope": "cv",
        },
    )

    assert response.status_code == 201
    assert response.json()["asked_as"] == 'the section "навыки" must list at least 21 items'


async def test_a_rule_requiring_experience_the_profile_lacks_is_refused_with_its_name(
    async_client: AsyncClient, db_session: AsyncSession, profiles: ProfileRepository
) -> None:
    """The boundary, over HTTP, with the names that caused it.

    A 422 saying only "rejected" would leave the person guessing which of their
    words was read as a claim.
    """
    await profiles.create(make_profile(skills=("python", "fastapi")))

    response = await async_client.post(
        RULES,
        json={
            "params": {"kind": "required_keyword", "keyword": "всегда пиши про Kubernetes"},
            "message": "упоминай Kubernetes",
        },
    )

    assert response.status_code == 422
    problem = response.json()
    assert problem["claims"] == ["kubernetes"]
    assert await db_session.scalar(select(func.count()).select_from(GenerationRule)) == 0


async def test_a_rule_forbidding_a_word_is_never_read_as_a_claim(
    async_client: AsyncClient, profiles: ProfileRepository
) -> None:
    """«Никогда не пиши Kubernetes» asserts nothing about anyone."""
    await profiles.create(make_profile(skills=("python",)))

    response = await async_client.post(
        RULES,
        json={
            "params": {"kind": "forbidden_phrase", "phrase": "Kubernetes"},
            "message": "не упоминай Kubernetes",
        },
    )

    assert response.status_code == 201


async def test_a_rule_can_be_switched_off_and_deleted(async_client: AsyncClient) -> None:
    """Off is not gone: a rule nobody is checking is what somebody is looking for."""
    rule_id = (await async_client.post(RULES, json=length_rule())).json()["id"]

    switched = await async_client.patch(f"{RULES}/{rule_id}", json={"is_active": False})
    assert switched.status_code == 200
    assert switched.json()["is_active"] is False

    removed = await async_client.delete(f"{RULES}/{rule_id}")
    assert removed.status_code == 204
    assert [r for r in (await async_client.get(RULES)).json() if not r["is_builtin"]] == []


async def test_editing_a_rule_into_a_dishonest_one_is_refused_too(
    async_client: AsyncClient, profiles: ProfileRepository
) -> None:
    """Because "create the harmless one, then edit it" is the obvious way round."""
    await profiles.create(make_profile(skills=("python",)))
    rule_id = (await async_client.post(RULES, json=length_rule())).json()["id"]

    response = await async_client.patch(
        f"{RULES}/{rule_id}",
        json={"params": {"kind": "required_keyword", "keyword": "Kubernetes"}},
    )

    assert response.status_code == 422
    assert response.json()["claims"] == ["kubernetes"]


async def test_a_rule_that_asks_nothing_is_rejected_by_the_contract(
    async_client: AsyncClient,
) -> None:
    """A count with neither bound would sit in the list looking enforced."""
    response = await async_client.post(
        RULES,
        json={
            "params": {"kind": "section_item_count", "section": "навыки"},
            "message": "ни о чём",
        },
    )

    assert response.status_code == 422


async def test_a_rule_for_a_kind_that_does_not_exist_is_rejected(
    async_client: AsyncClient,
) -> None:
    """The vocabulary is closed, and the API is where that is enforced."""
    response = await async_client.post(
        RULES,
        json={"params": {"kind": "write_whatever_you_like"}, "message": "нет"},
    )

    assert response.status_code == 422


# ── reference documents ───────────────────────────────────────────────


async def test_a_reference_can_be_pasted_as_text(async_client: AsyncClient) -> None:
    """The ordinary case for a document whose layout the extractor would mangle."""
    response = await async_client.post(
        REFERENCES,
        data={
            "kind": "cover_letter",
            "title": "Письмо, на которое ответили",
            "note": "коротко, без канцелярита",
            "text": A_LETTER,
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["reference"]["text"] == A_LETTER.strip()
    assert body["reference"]["source_format"] is None
    assert body["warnings"] == []


async def test_a_reference_can_be_uploaded_as_a_file(async_client: AsyncClient) -> None:
    """Extraction is the resume extractor's, not a second one written here."""
    response = await async_client.post(
        REFERENCES,
        data={"kind": "cv", "title": "Хорошее резюме"},
        files={"file": ("cv.txt", A_LETTER.encode(), "text/plain")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["reference"]["source_format"] == "txt"
    assert body["reference"]["source_filename"] == "cv.txt"
    assert body["reference"]["size_bytes"] == len(A_LETTER.encode())


async def test_a_reference_is_either_a_file_or_text_and_never_both(
    async_client: AsyncClient,
) -> None:
    """Two ways of giving one document; only one of them can be the stored one."""
    both = await async_client.post(
        REFERENCES,
        data={"kind": "cv", "title": "Оба", "text": A_LETTER},
        files={"file": ("cv.txt", A_LETTER.encode(), "text/plain")},
    )
    neither = await async_client.post(REFERENCES, data={"kind": "cv", "title": "Ничего"})

    assert both.status_code == 422
    assert neither.status_code == 422


async def test_a_document_too_short_to_be_an_example_is_refused(
    async_client: AsyncClient,
) -> None:
    """A fragment teaches a shape nobody meant."""
    response = await async_client.post(
        REFERENCES, data={"kind": "cv", "title": "Обрывок", "text": "Здравствуйте!"}
    )

    assert response.status_code == 422


async def test_a_pdf_reference_warns_that_two_columns_come_out_interleaved(
    async_client: AsyncClient,
) -> None:
    """The warning has to arrive while the person still has the file open.

    ``pdfplumber`` reads a page line by line, so a sidebar and a body interleave
    — the resume path works around that by sending the PDF to the model as a
    document, and a reference has no such path. Saying so is the honest answer.
    """
    pdf = (
        pytest.importorskip("pathlib").Path("backend/tests/fixtures/resumes/english.pdf")
    ).read_bytes()

    response = await async_client.post(
        REFERENCES,
        data={"kind": "cv", "title": "Резюме из PDF"},
        files={"file": ("cv.pdf", pdf, "application/pdf")},
    )

    assert response.status_code == 201
    assert any("двухколоночном" in warning for warning in response.json()["warnings"])


async def test_a_reference_is_listed_without_shipping_the_whole_document(
    async_client: AsyncClient,
) -> None:
    """Five CVs to render five rows is a design nobody notices until it is slow."""
    await async_client.post(
        REFERENCES,
        data={"kind": "cover_letter", "title": "Письмо", "text": A_LETTER},
    )

    listed = await async_client.get(REFERENCES)

    assert listed.status_code == 200
    row = listed.json()[0]
    assert "text" not in row
    assert row["characters"] == len(A_LETTER.strip())
    assert row["preview"] and len(row["preview"]) < row["characters"]


async def test_a_reference_can_be_renamed_switched_off_and_deleted(
    async_client: AsyncClient,
) -> None:
    """Everything around the document may be corrected. The document may not."""
    created = await async_client.post(
        REFERENCES, data={"kind": "cover_letter", "title": "Письмо", "text": A_LETTER}
    )
    reference_id = created.json()["reference"]["id"]

    patched = await async_client.patch(
        f"{REFERENCES}/{reference_id}", json={"title": "Лучшее письмо", "is_active": False}
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "Лучшее письмо"
    assert patched.json()["is_active"] is False
    assert patched.json()["text"] == A_LETTER.strip()

    assert (await async_client.delete(f"{REFERENCES}/{reference_id}")).status_code == 204
    assert (await async_client.get(f"{REFERENCES}/{reference_id}")).status_code == 404


async def test_a_reference_body_cannot_be_edited_through_the_patch(
    async_client: AsyncClient,
) -> None:
    """Editing it in place would make the stored text something nobody has read."""
    created = await async_client.post(
        REFERENCES, data={"kind": "cover_letter", "title": "Письмо", "text": A_LETTER}
    )
    reference_id = created.json()["reference"]["id"]

    response = await async_client.patch(
        f"{REFERENCES}/{reference_id}", json={"text": "совсем другой документ"}
    )

    assert response.status_code == 422


async def test_references_can_be_listed_by_kind(async_client: AsyncClient) -> None:
    """A CV and a letter are different documents and different lists."""
    await async_client.post(REFERENCES, data={"kind": "cv", "title": "Резюме", "text": A_LETTER})
    await async_client.post(
        REFERENCES, data={"kind": "cover_letter", "title": "Письмо", "text": A_LETTER}
    )

    only_cv = await async_client.get(REFERENCES, params={"kind": "cv"})

    assert [row["title"] for row in only_cv.json()] == ["Резюме"]


async def test_everything_addressed_by_an_id_that_is_not_there_is_a_404(
    async_client: AsyncClient,
) -> None:
    """One place, so a client never has to guess which absence it hit."""
    missing = uuid4()

    assert (await async_client.get(f"{REFERENCES}/{missing}")).status_code == 404
    assert (
        await async_client.patch(f"{REFERENCES}/{missing}", json={"title": "нет"})
    ).status_code == 404
    assert (await async_client.delete(f"{REFERENCES}/{missing}")).status_code == 404
    assert (
        await async_client.patch(f"{RULES}/{missing}", json={"is_active": False})
    ).status_code == 404
    assert (await async_client.delete(f"{RULES}/{missing}")).status_code == 404


# ── the vacancy picker behind the preview ─────────────────────────────


async def test_the_picker_offers_vacancies_and_can_be_searched(
    async_client: AsyncClient, vacancies: VacancyRepository
) -> None:
    """«На любой вакансии из базы» — which needs a way to name one."""
    await vacancies.upsert_by_external_id(
        make_vacancy("pick-1", title="Backend Engineer"),
        source_slug="hh",
        external_id="hh-k1",
        url="https://e.test/k1",
    )
    await vacancies.upsert_by_external_id(
        make_vacancy("pick-2", title="Data Engineer"),
        source_slug="hh",
        external_id="hh-k2",
        url="https://e.test/k2",
    )

    everything = await async_client.get("/api/v1/workshop/vacancies")
    searched = await async_client.get("/api/v1/workshop/vacancies", params={"q": "Data"})

    assert everything.status_code == 200
    assert len(everything.json()) == 2
    assert [row["title"] for row in searched.json()] == ["Data Engineer"]


# ── the trial letter ──────────────────────────────────────────────────


async def test_the_preview_writes_a_letter_and_leaves_the_tracker_alone(
    async_client: AsyncClient,
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The "try it" button. Nothing it does may reach the apply queue."""
    await profiles.create(make_profile())
    upserted = await vacancies.upsert_by_external_id(
        make_vacancy("preview-1"),
        source_slug="hh",
        external_id="hh-p1",
        url="https://e.test/p1",
    )
    router = FakeRouter(_draft(FILLER, addressed_skills=[]))
    monkeypatch.setattr("app.workshop.service.get_router", lambda: router)

    response = await async_client.post(PREVIEW, json={"vacancy_id": str(upserted.vacancy_id)})

    assert response.status_code == 200
    body = response.json()
    assert body["written"] is True
    assert body["source"] == "model"
    assert body["text"].startswith("Здравствуйте")
    assert await db_session.scalar(select(func.count()).select_from(Application)) == 0


async def test_the_preview_reports_a_rule_it_could_not_keep_as_an_answer(
    async_client: AsyncClient,
    vacancies: VacancyRepository,
    profiles: ProfileRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """200 with ``written: false``, not a 4xx that throws the list away.

    It is the answer to the question the owner asked — "what do my rules do?" —
    and the list of what was broken is the whole of its usefulness.
    """
    await profiles.create(make_profile())
    upserted = await vacancies.upsert_by_external_id(
        make_vacancy("preview-2"),
        source_slug="hh",
        external_id="hh-p2",
        url="https://e.test/p2",
    )
    await async_client.post(
        RULES,
        json={
            "params": {"kind": "required_keyword", "keyword": "непроизносимое слово"},
            "message": "должно быть непроизносимое слово",
            "scope": "cover_letter",
        },
    )
    router = FakeRouter(_draft(FILLER, addressed_skills=[]))
    monkeypatch.setattr("app.workshop.service.get_router", lambda: router)

    response = await async_client.post(PREVIEW, json={"vacancy_id": str(upserted.vacancy_id)})

    assert response.status_code == 200
    body = response.json()
    assert body["written"] is False
    assert body["text"] is None
    assert [rule["message"] for rule in body["broken_rules"]] == [
        "должно быть непроизносимое слово"
    ]


async def test_a_preview_without_a_resume_says_which_thing_is_missing(
    async_client: AsyncClient, vacancies: VacancyRepository
) -> None:
    """Only one of the two 404s is fixed by uploading something."""
    upserted = await vacancies.upsert_by_external_id(
        make_vacancy("preview-3"),
        source_slug="hh",
        external_id="hh-p3",
        url="https://e.test/p3",
    )

    response = await async_client.post(PREVIEW, json={"vacancy_id": str(upserted.vacancy_id)})

    assert response.status_code == 404
    assert "resume" in response.json()["detail"]


async def test_a_preview_for_an_unknown_vacancy_is_a_plain_404(
    async_client: AsyncClient, profiles: ProfileRepository
) -> None:
    """And says so in different words from the missing-resume one."""
    await profiles.create(make_profile())

    response = await async_client.post(PREVIEW, json={"vacancy_id": str(uuid4())})

    assert response.status_code == 404
    assert response.json()["detail"] == "Vacancy not found"
