"""The profile endpoints: reading an extracted profile and correcting it by hand.

Two things are being defended here.

The first is privacy. ``candidate_profile`` stores the entire resume text and a
1024-dimension embedding of it. Neither belongs in an HTTP response: the text is
personal data the client never asked for, and the vector is a large blob that is
meaningless outside the matcher. They are absent from ``CandidateProfileRead``
on purpose, and a field added back "for debugging" would leak a whole CV.

The second is that PATCH has to actually take effect. Extraction is a language
model reading a PDF; it invents skills, inflates levels and misreads cities, and
every one of those errors turns into a wrong match score. This endpoint is the
only way a human corrects it, so "the request returned 200" is not the assertion
that matters — what is in the database afterwards is.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PROBLEM_JSON
from app.db.enums import ParseStatus, Seniority, SkillLevel
from app.db.models import CandidateProfile, ProfileSkill
from app.db.repositories.profile import ProfileRepository
from app.db.session import get_session
from app.schemas.ats import ATSReport, Finding, FindingCode, Severity
from app.schemas.profile import CandidateProfileUpdate
from factories import make_profile

PROFILES_URL = "/api/v1/profile"

#: The resume text a client must never be handed back.
RAW_TEXT = "Нуржан, backend-инженер. Личные данные, которым не место в ответе API."


def url_for(profile_id: UUID | str) -> str:
    """The endpoint under test for one profile id."""
    return f"{PROFILES_URL}/{profile_id}"


async def stored_skill_names(session: AsyncSession, profile_id: UUID) -> set[str]:
    """Skill names read straight from the table.

    A column select, not ``ProfileRepository.get``: the endpoint shares this
    session, so a repository read would hand back the very ORM instance the
    request mutated and pass even if nothing was written. This goes to the row.
    """
    stmt = select(ProfileSkill.canonical_name).where(ProfileSkill.profile_id == profile_id)
    return set((await session.execute(stmt)).scalars().all())


async def stored_column(session: AsyncSession, profile_id: UUID, column: str) -> Any:
    """One profile column, read from the row rather than from the identity map."""
    stmt = select(getattr(CandidateProfile, column)).where(CandidateProfile.id == profile_id)
    return (await session.execute(stmt)).scalar_one()


@pytest_asyncio.fixture
async def profile(profiles: ProfileRepository, db_session: AsyncSession) -> CandidateProfile:
    """A finished profile with skills, upload metadata, raw text and an embedding.

    Deliberately fully populated: the read model is asserted to *omit* two
    columns, and that assertion is worthless against a row where those columns
    happen to be NULL.
    """
    instance = await profiles.create(
        make_profile(skills=("python", "fastapi", "postgresql"), raw_text=RAW_TEXT)
    )
    instance.parse_status = ParseStatus.READY
    instance.resume_filename = "resume.pdf"
    instance.resume_size_bytes = 12_345
    instance.resume_format = "pdf"
    await db_session.flush()
    await profiles.set_embedding(instance.id, [0.01] * 1024)
    await db_session.flush()
    # The UPDATE above went round the identity map, so the in-memory instance
    # still carries the old NULL. Reload just that column: the object the
    # endpoint serialises has to hold the vector for its absence to mean
    # anything.
    await db_session.refresh(instance, attribute_names=["embedding"])
    return instance


@pytest_asyncio.fixture
async def tolerant_client(app: FastAPI, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """Client that returns a 500 response instead of re-raising the exception.

    Needed only where the endpoint is expected to fail: the shared
    ``async_client`` lets the exception escape, which hides the status code the
    caller would really see.
    """

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


# ── GET ───────────────────────────────────────────────────────────────


async def test_get_returns_the_profile_with_skills_and_parse_state(
    async_client: AsyncClient, profile: CandidateProfile
) -> None:
    """This is the endpoint the client polls after an upload, so one response has
    to answer both "who is this candidate" and "has parsing finished"; a skills
    list served from a second round trip would let the two disagree."""
    response = await async_client.get(url_for(profile.id))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(profile.id)
    assert body["name"] == "Nurzhan"
    assert body["seniority"] == Seniority.MIDDLE.value
    assert body["parse_status"] == ParseStatus.READY.value
    assert body["resume_filename"] == "resume.pdf"
    assert body["resume_size_bytes"] == 12_345
    assert {skill["canonical_name"] for skill in body["skills"]} == {
        "python",
        "fastapi",
        "postgresql",
    }


async def test_get_never_returns_raw_text_or_the_embedding(
    async_client: AsyncClient, profile: CandidateProfile, db_session: AsyncSession
) -> None:
    """``raw_text`` is the candidate's whole CV — personal data that no screen in
    the product renders — and ``embedding`` is a 1024-float blob a client cannot
    interpret. Both are stored on the row this response is built from, so only a
    deliberate omission keeps them out; this test fails the moment someone adds
    them back to the read model."""
    # Prove the row really holds both before claiming the response drops them:
    # an absence assertion against NULL columns proves nothing.
    assert await stored_column(db_session, profile.id, "raw_text") == RAW_TEXT
    assert len(await stored_column(db_session, profile.id, "embedding")) == 1024

    response = await async_client.get(url_for(profile.id))

    # Anchor the negatives: an endpoint that 404s or returns {} would satisfy
    # "raw_text is absent" too. This has to be the real profile document.
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(profile.id)
    assert len(body["skills"]) == 3

    assert "raw_text" not in body
    assert "embedding" not in body
    # Belt and braces: the text must not resurface under another key either.
    assert RAW_TEXT not in response.text


async def test_get_unknown_id_is_a_problem_document(async_client: AsyncClient) -> None:
    """A 404 has to arrive in the same RFC 7807 envelope as every other error, or
    the client needs a special case for exactly this endpoint."""
    response = await async_client.get(url_for(uuid4()))

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert body["status"] == 404
    assert body["type"].endswith("#http-error")
    assert body["detail"] == "Profile not found"


async def test_get_rejects_an_id_that_is_not_a_uuid(async_client: AsyncClient) -> None:
    """A malformed id is the caller's mistake, not a missing profile: 422 tells
    them to fix the request, while 404 would send them looking for the row."""
    response = await async_client.get(url_for("not-a-uuid"))

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_JSON)


# ── PATCH: scalar fields ──────────────────────────────────────────────


async def test_patch_touches_only_the_fields_that_were_sent(
    async_client: AsyncClient, profile: CandidateProfile, db_session: AsyncSession
) -> None:
    """The UI sends one edited field, not the whole form. Without ``exclude_unset``
    every omitted field would be read as an explicit null and a one-word headline
    fix would wipe the salary expectation and the seniority with it."""
    response = await async_client.patch(
        url_for(profile.id), json={"headline": "Senior Backend Engineer"}
    )

    assert response.status_code == 200
    assert response.json()["headline"] == "Senior Backend Engineer"
    assert await stored_column(db_session, profile.id, "headline") == "Senior Backend Engineer"
    assert await stored_column(db_session, profile.id, "name") == "Nurzhan"
    assert await stored_column(db_session, profile.id, "salary_min") == Decimal("4000.00")
    assert await stored_column(db_session, profile.id, "seniority") is Seniority.MIDDLE
    assert await stored_skill_names(db_session, profile.id) == {
        "python",
        "fastapi",
        "postgresql",
    }


async def test_patch_can_write_an_explicit_null(
    async_client: AsyncClient, profile: CandidateProfile, db_session: AsyncSession
) -> None:
    """Unset means unchanged, but an explicit null must not collapse into the same
    thing: clearing a seniority the extractor guessed wrong is a correction the
    user has to be able to make."""
    response = await async_client.patch(url_for(profile.id), json={"seniority": None})

    assert response.status_code == 200
    assert response.json()["seniority"] is None
    assert await stored_column(db_session, profile.id, "seniority") is None


# ── PATCH: skills ─────────────────────────────────────────────────────


async def test_patch_skills_replaces_the_whole_set(
    async_client: AsyncClient, profile: CandidateProfile, db_session: AsyncSession
) -> None:
    """Sending skills replaces them rather than merging. Merging would make a
    hallucinated skill undeletable — the user removes it from the list, the merge
    puts it back, and it keeps inflating the score of every vacancy asking for
    it."""
    response = await async_client.patch(
        url_for(profile.id),
        json={
            "skills": [
                {"canonical_name": "python", "raw_names": ["Python"], "level": "expert"},
                {"canonical_name": "go", "raw_names": ["Go", "Golang"], "level": "basic"},
            ]
        },
    )

    assert response.status_code == 200
    assert {skill["canonical_name"] for skill in response.json()["skills"]} == {"python", "go"}
    assert await stored_skill_names(db_session, profile.id) == {"python", "go"}


async def test_patch_skills_keeps_the_edited_attributes(
    async_client: AsyncClient, profile: CandidateProfile, db_session: AsyncSession
) -> None:
    """Correcting a level is the common case — the extractor calls a line on a CV
    "expert" — so the new level has to reach the row the matcher weights by, not
    just the response body."""
    response = await async_client.patch(
        url_for(profile.id),
        json={"skills": [{"canonical_name": "python", "level": "basic", "years": "1.5"}]},
    )

    assert response.status_code == 200
    stmt = select(ProfileSkill).where(ProfileSkill.profile_id == profile.id)
    stored = (await db_session.execute(stmt)).scalars().all()
    assert len(stored) == 1
    assert stored[0].level is SkillLevel.BASIC
    assert stored[0].years == Decimal("1.5")


async def test_patch_with_an_empty_skill_list_clears_every_skill(
    async_client: AsyncClient, profile: CandidateProfile, db_session: AsyncSession
) -> None:
    """An empty list is a value, not an absence: a profile whose skills were all
    wrong must be emptiable, and treating ``[]`` as "unchanged" would silently
    ignore the request."""
    response = await async_client.patch(url_for(profile.id), json={"skills": []})

    assert response.status_code == 200
    assert response.json()["skills"] == []
    assert await stored_skill_names(db_session, profile.id) == set()


async def test_patch_with_a_duplicated_canonical_name_is_rejected_by_name(
    tolerant_client: AsyncClient, profile: CandidateProfile
) -> None:
    """``(profile_id, canonical_name)`` is unique, and a hand-typed correction
    list is exactly where a repeat comes from.

    This used to reach PostgreSQL, raise an IntegrityError at flush and surface
    as an opaque 500 that did not say which skill was duplicated — and left the
    request's transaction in a failed state on the way out. It is now caught at
    the schema boundary, so the caller gets a 422 that names the offender."""
    response = await tolerant_client.patch(
        url_for(profile.id),
        json={
            "skills": [
                {"canonical_name": "python", "level": "expert"},
                {"canonical_name": "python", "level": "basic"},
            ]
        },
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert body["type"].endswith("#validation-error")
    assert "python" in str(body["errors"])


# ── PATCH: validation and missing rows ────────────────────────────────


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        pytest.param(
            {"skills": [{"canonical_name": "python", "level": "guru"}]},
            "level outside the enum",
            id="unknown-level",
        ),
        pytest.param(
            {"skills": [{"canonical_name": "python", "years": "99.0"}]},
            "more years than a career",
            id="years-out-of-range",
        ),
        pytest.param(
            {"salary_currency": "XYZ"},
            "not an active ISO 4217 code",
            id="unknown-currency",
        ),
        pytest.param({"salary_min": "-1"}, "negative pay", id="negative-salary"),
        pytest.param({"total_years": "-2"}, "negative experience", id="negative-total-years"),
    ],
)
async def test_patch_rejects_nonsense(
    async_client: AsyncClient,
    profile: CandidateProfile,
    db_session: AsyncSession,
    payload: dict[str, Any],
    reason: str,
) -> None:
    """Corrections are hand-typed, so the endpoint is the last gate before junk
    reaches the scorer: a level outside the enum has no weight, an unknown
    currency cannot be converted and would sort wrong, and a negative salary
    matches everything. Rejected requests must also leave the row alone."""
    response = await async_client.patch(url_for(profile.id), json=payload)

    assert response.status_code == 422, reason
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.json()["errors"]
    assert await stored_skill_names(db_session, profile.id) == {
        "python",
        "fastapi",
        "postgresql",
    }
    assert await stored_column(db_session, profile.id, "salary_min") == Decimal("4000.00")


async def test_patch_on_an_unknown_id_is_404(async_client: AsyncClient) -> None:
    """A correction aimed at a profile that no longer exists must say so, not
    report success for a write that went nowhere."""
    response = await async_client.patch(url_for(uuid4()), json={"headline": "Anything"})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.json()["detail"] == "Profile not found"


# ── deactivation ──────────────────────────────────────────────────────


async def test_deactivating_a_profile_hands_the_dashboard_the_other_one(
    profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """``is_active`` is what the dashboard scores against, and it is editable
    through the same PATCH contract. Retiring a profile has to move that pointer:
    otherwise the old resume keeps driving every match score while the UI shows
    the new one."""
    old = await profiles.create(make_profile(name="Old", skills=("python",)))
    new = await profiles.create(make_profile(name="New", skills=("go",)))
    await db_session.flush()

    await profiles.update(old.id, CandidateProfileUpdate(is_active=False))
    await db_session.flush()

    active = await profiles.get_active()
    assert active is not None
    assert active.id == new.id


async def test_no_active_profile_is_none_rather_than_an_error(
    profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """Before the first upload — and after every profile is retired — there is no
    active profile. That is an ordinary state the dashboard renders as "upload a
    resume", so the repository returns None instead of raising."""
    only = await profiles.create(make_profile(name="Only", skills=("python",)))
    await db_session.flush()

    # Without this the test would pass against a get_active that always
    # answers None, which is the very failure it is meant to rule out.
    before = await profiles.get_active()
    assert before is not None
    assert before.id == only.id

    await profiles.update(only.id, CandidateProfileUpdate(is_active=False))
    await db_session.flush()

    assert await profiles.get_active() is None


# ── GET /{id}/ats-report ──────────────────────────────────────────────


def ats_url(profile_id: UUID | str) -> str:
    """The readability report for one profile."""
    return f"{PROFILES_URL}/{profile_id}/ats-report"


CLEAN_REPORT = ATSReport(
    score=100,
    findings=[],
    checks_run=list(FindingCode),
    source_format="pdf",
    page_count=1,
    word_count=204,
)

SCAN_REPORT = ATSReport(
    score=0,
    findings=[
        Finding(
            code=FindingCode.NO_TEXT_LAYER,
            severity=Severity.CRITICAL,
            title="Нет текстового слоя",
            explanation="Из файла извлекается 0 символов.",
            fix="Экспортируй резюме в PDF из текстового редактора.",
            penalty=100,
        )
    ],
    checks_run=[FindingCode.NO_TEXT_LAYER],
    source_format="pdf",
    page_count=1,
    word_count=0,
)


async def test_ats_report_is_served_from_what_was_stored(
    async_client: AsyncClient,
    profiles: ProfileRepository,
    db_session: AsyncSession,
) -> None:
    """Read back, not recomputed: the uploaded file is long gone by then."""
    reserved = await profiles.create_pending(
        filename="scan.pdf",
        size_bytes=23_338,
        source_format="pdf",
        started_at=datetime.now(UTC),
        ats_report=SCAN_REPORT,
    )
    await db_session.flush()

    response = await async_client.get(ats_url(reserved.id))

    assert response.status_code == 200
    body = response.json()
    assert body["score"] == 0
    assert body["findings"][0]["code"] == FindingCode.NO_TEXT_LAYER.value
    # The explanation and the fix are the point of the endpoint: a bare score
    # tells the candidate nothing they can act on.
    assert body["findings"][0]["fix"]
    assert body["findings"][0]["explanation"]


async def test_a_profile_without_a_report_is_not_reported_as_clean(
    async_client: AsyncClient,
    profiles: ProfileRepository,
    db_session: AsyncSession,
) -> None:
    """The dangerous failure mode this endpoint has to avoid.

    Profiles uploaded before the audit existed have no report, and an audit that
    crashed leaves none either. Answering 200 with an empty finding list would
    tell those candidates their resume is machine-readable — a claim nothing
    checked. The absence is reported as an absence."""
    reserved = await profiles.create_pending(
        filename="old.pdf", size_bytes=1, source_format="pdf", started_at=datetime.now(UTC)
    )
    await db_session.flush()

    response = await async_client.get(ats_url(reserved.id))

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_JSON
    # Distinct from the profile's own 404: the profile exists, the report does not.
    assert "report" in response.json()["detail"].lower()


async def test_an_unknown_profile_has_no_report(async_client: AsyncClient) -> None:
    """Same 404, so a wrong id cannot be told apart from a missing report by
    status alone — and neither leaks whether that profile exists."""
    assert (await async_client.get(ats_url(uuid4()))).status_code == 404


async def test_the_report_survives_the_jsonb_round_trip(
    profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """The column is JSONB, so the enums and the nested findings go through a
    dict and come back. A field that serialised but did not validate would only
    surface on somebody's upload."""
    reserved = await profiles.create_pending(
        filename="r.pdf",
        size_bytes=1,
        source_format="pdf",
        started_at=datetime.now(UTC),
        ats_report=SCAN_REPORT,
    )
    await db_session.flush()

    assert await profiles.get_ats_report(reserved.id) == SCAN_REPORT


async def test_the_profile_response_does_not_carry_the_report(
    async_client: AsyncClient,
    profiles: ProfileRepository,
    db_session: AsyncSession,
) -> None:
    """They are ready at different times, so they are separate resources.

    The report exists the moment the file lands; the profile it belongs to is
    still being extracted for another half-minute. Folding the report into the
    polled response would put a few kilobytes on every poll to deliver something
    that stopped changing before the polling began."""
    reserved = await profiles.create_pending(
        filename="r.pdf",
        size_bytes=1,
        source_format="pdf",
        started_at=datetime.now(UTC),
        ats_report=CLEAN_REPORT,
    )
    await db_session.flush()

    body = (await async_client.get(url_for(reserved.id))).json()

    assert "ats_report" not in body
