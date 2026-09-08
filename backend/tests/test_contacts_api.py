"""The contact block: prefilled from a resume, corrected by hand, never lost.

Three promises are defended here, and each one is a bug that has bitten this
kind of feature before.

**A correction survives the next parse.** Extraction runs again on every upload
and would happily put the phone number it misread back over the one the owner
typed. The ``*_edited`` flags are what stop it, and the tests below assert on
the row rather than on the response, because it is the row the next parse
reads.

**A correction survives the next resume.** Uploading a CV creates a new profile
rather than editing the old one. Without carrying the hand-edited block
forward, "my contacts" would silently reset every time the owner updated their
CV — the exact moment they are least likely to check.

**Contacts do not leak into the profile.** They live in a table of their own
with nothing pointing at it from ``candidate_profile`` precisely so that a
phone number cannot ride along in a response that was asked for something else.

Every person, number and address below is invented.
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PROBLEM_JSON
from app.db.models import CandidateProfile, ProfileContact, ProfileContactLink
from app.db.repositories.contact import ContactRepository
from app.db.repositories.profile import ProfileRepository
from app.db.session import get_session
from app.schemas.contact import ProfileContactUpdate
from app.services import contacts as contact_service
from app.services import resume as resume_service
from factories import make_profile

RESUME_TEXT = (
    "Кирилл Макетов\n"
    "Python-разработчик\n"
    "Город: Астана, Казахстан\n"
    "Email: k.maketov@example.com\n"
    "Телефон: +7 700 000 00 15\n"
    "GitHub: github.com/maketov\n"
    "Telegram: @maketov_k\n"
    "Зарплатные ожидания: от 1 400 000 KZT\n"
)


def contacts_url(profile_id: UUID | str) -> str:
    """The endpoint under test for one profile id."""
    return f"/api/v1/profile/{profile_id}/contacts"


@pytest_asyncio.fixture
async def profile(profiles: ProfileRepository, db_session: AsyncSession) -> CandidateProfile:
    """A parsed profile carrying the resume text the contacts come out of."""
    instance = await profiles.create(make_profile(name="Кирилл Макетов", raw_text=RESUME_TEXT))
    # ``make_profile`` fixes a city of its own; this one has to match the resume
    # text above, because that is the pairing prefill is asserted on.
    instance.locations = ["Астана"]
    await db_session.flush()
    return instance


@pytest_asyncio.fixture
async def failing_client(app: FastAPI, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """A client that lets a server error through as a 500 response.

    The default fixture re-raises inside the test, which is right for most
    tests and wrong for the ones asserting what a caller actually receives.
    """

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def stored_contact(session: AsyncSession, profile_id: UUID) -> ProfileContact:
    """The contact row, read back from the database rather than from a response."""
    contact = await ContactRepository(session).get(profile_id)
    assert contact is not None, "no contact block was written"
    return contact


async def stored_links(session: AsyncSession, profile_id: UUID) -> list[tuple[str, str, bool]]:
    """Every stored link as ``(kind, url, is_manual)``, in display order."""
    contact = await stored_contact(session, profile_id)
    return [(link.kind, link.url, link.is_manual) for link in contact.links]


async def count_rows(session: AsyncSession, model: Any) -> int:
    """How many rows of one table exist, counted in SQL."""
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


# ── reading ───────────────────────────────────────────────────────────


async def test_a_profile_with_no_contacts_yet_reads_as_an_empty_block(
    async_client: AsyncClient, profile: CandidateProfile
) -> None:
    """Not a 404. The screen behind this endpoint is a form, and "nothing here
    yet" is the state it exists to fix — answering 404 would make an empty
    profile indistinguishable from a broken link and leave nothing to type
    into."""
    response = await async_client.get(contacts_url(profile.id))

    assert response.status_code == 200
    body = response.json()
    assert body["profile_id"] == str(profile.id)
    assert body["full_name"] is None
    assert body["links"] == []
    assert body["edited"] == {"full_name": False, "phone": False, "email": False, "city": False}
    assert body["updated_at"] is None


async def test_reading_the_contacts_of_a_profile_that_does_not_exist_is_404(
    async_client: AsyncClient,
) -> None:
    """An empty block for an id that was never a profile would tell a client
    its bookmark still works."""
    response = await async_client.get(contacts_url(uuid4()))

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)


async def test_the_profile_endpoint_does_not_carry_contacts(
    async_client: AsyncClient, profile: CandidateProfile, db_session: AsyncSession
) -> None:
    """The privacy boundary, asserted from the outside. A phone number reaches
    a response only when something asked for one; ``GET /profile/{id}`` asks
    for a profile, and every screen that renders it — the dashboard, the match
    list — would otherwise be carrying one."""
    await contact_service.update_contacts(
        db_session, profile.id, ProfileContactUpdate(phone="+7 700 000 00 15")
    )
    await db_session.flush()

    response = await async_client.get(f"/api/v1/profile/{profile.id}")

    assert response.status_code == 200
    assert "+7 700 000 00 15" not in response.text
    assert "phone" not in response.json()


async def test_the_active_profile_endpoint_finds_the_id_a_client_has_none_of(
    async_client: AsyncClient, profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """A browser that has just been opened knows no profile id, and every
    contacts request needs one. This is how the screen finds it."""
    instance = await profiles.create(make_profile(name="Кирилл Макетов"))
    await profiles.activate(instance.id)
    await profiles.deactivate_others(instance.id)
    await db_session.flush()

    response = await async_client.get("/api/v1/profile/active")

    assert response.status_code == 200
    assert response.json()["id"] == str(instance.id)


async def test_the_active_profile_route_is_not_read_as_an_id(
    async_client: AsyncClient, profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """``/profile/active`` and ``/profile/{profile_id}`` overlap, and routes are
    matched in the order they were added. Declared the other way round this
    would be a 422 about a malformed UUID, which is why the order is asserted
    rather than assumed."""
    response = await async_client.get("/api/v1/profile/active")

    assert response.status_code == 404
    body = response.json()
    assert "upload a resume" in body["detail"]


# ── correcting by hand ────────────────────────────────────────────────


async def test_a_patch_writes_the_values_and_records_who_wrote_them(
    async_client: AsyncClient, profile: CandidateProfile, db_session: AsyncSession
) -> None:
    """The flags are the point. Storing the value is the easy half; recording
    that a human chose it is what makes the next parse leave it alone."""
    response = await async_client.patch(
        contacts_url(profile.id),
        json={"full_name": "Кирилл Макетов", "phone": "+7 700 000 00 15"},
    )

    assert response.status_code == 200
    contact = await stored_contact(db_session, profile.id)
    assert contact.full_name == "Кирилл Макетов"
    assert contact.phone == "+7 700 000 00 15"
    assert contact.full_name_edited is True
    assert contact.phone_edited is True
    # Untouched fields stay unflagged, so prefill may still fill them in.
    assert contact.email_edited is False
    assert contact.city_edited is False


async def test_clearing_a_field_is_a_decision_and_is_remembered_as_one(
    async_client: AsyncClient, profile: CandidateProfile, db_session: AsyncSession
) -> None:
    """A cleared field has to stick: "I have no phone number on my CV" is an
    answer. Treated as "unset", the
    next upload would put the extracted number straight back and the owner
    would have to delete it again after every resume they upload."""
    await async_client.patch(contacts_url(profile.id), json={"phone": "+7 700 000 00 15"})

    response = await async_client.patch(contacts_url(profile.id), json={"phone": None})

    assert response.status_code == 200
    contact = await stored_contact(db_session, profile.id)
    assert contact.phone is None
    assert contact.phone_edited is True


async def test_an_emptied_input_is_read_as_cleared_rather_than_rejected(
    async_client: AsyncClient, profile: CandidateProfile, db_session: AsyncSession
) -> None:
    """A form submits ``""`` for a field the user emptied. Stored as-is it
    would be a phone number that is present, blank and impossible to tell from
    one nobody has filled in; rejected, the owner could not clear a field at
    all without opening a developer console."""
    response = await async_client.patch(contacts_url(profile.id), json={"email": "   "})

    assert response.status_code == 200
    contact = await stored_contact(db_session, profile.id)
    assert contact.email is None
    assert contact.email_edited is True


async def test_a_patch_leaves_the_fields_it_does_not_mention_alone(
    async_client: AsyncClient, profile: CandidateProfile, db_session: AsyncSession
) -> None:
    """PATCH, not PUT. The screen sends what changed, and a field missing from
    the body is one the owner did not touch — not one they cleared."""
    await async_client.patch(
        contacts_url(profile.id), json={"full_name": "Кирилл Макетов", "city": "Астана"}
    )

    await async_client.patch(contacts_url(profile.id), json={"city": "Алматы"})

    contact = await stored_contact(db_session, profile.id)
    assert contact.full_name == "Кирилл Макетов"
    assert contact.city == "Алматы"


async def test_links_are_replaced_wholesale_and_marked_as_the_owners(
    async_client: AsyncClient, profile: CandidateProfile, db_session: AsyncSession
) -> None:
    """A link list is edited as a list — reordered, pruned, relabelled — so the
    body carries the whole set rather than a diff nobody wants to compute in a
    form."""
    response = await async_client.patch(
        contacts_url(profile.id),
        json={
            "links": [
                {"kind": "github", "url": "https://github.com/maketov"},
                {"kind": "portfolio", "url": "https://maketov.example.com", "label": "Портфолио"},
            ]
        },
    )

    assert response.status_code == 200
    assert await stored_links(db_session, profile.id) == [
        ("github", "https://github.com/maketov", True),
        ("portfolio", "https://maketov.example.com", True),
    ]
    # Order is stored, not incidental: it is the order a generated document
    # prints the links in.
    assert [link["kind"] for link in response.json()["links"]] == ["github", "portfolio"]


async def test_a_kind_this_codebase_has_never_heard_of_is_still_stored(
    async_client: AsyncClient, profile: CandidateProfile, db_session: AsyncSession
) -> None:
    """``kind`` is a string, not an enum, exactly so that the next place people
    keep a profile does not need a migration to be storable."""
    response = await async_client.patch(
        contacts_url(profile.id),
        json={"links": [{"kind": "codeberg", "url": "https://codeberg.org/maketov"}]},
    )

    assert response.status_code == 200
    assert await stored_links(db_session, profile.id) == [
        ("codeberg", "https://codeberg.org/maketov", True)
    ]


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"email": "не почта"}, "an address nothing could deliver to"),
        ({"email": "user@localhost"}, "no dotted domain to route to"),
        ({"phone": "позвоните секретарю"}, "a phone number with no digits in it"),
        (
            {"links": [{"kind": "site", "url": "javascript:alert(1)"}]},
            "a scheme that is not http(s), rendered as a link the owner clicks",
        ),
        (
            {"links": [{"kind": "site", "url": "https://user:token@example.com"}]},
            "credentials smuggled into a stored, displayed URL",
        ),
        (
            {"links": [{"kind": "Не Слаг", "url": "https://example.com"}]},
            "a kind that is not a slug the UI can key on",
        ),
        ({"links": [{"url": "example.com"}]}, "a bare host is not a link"),
    ],
)
async def test_a_value_that_could_not_be_used_is_rejected_with_a_reason(
    async_client: AsyncClient, profile: CandidateProfile, body: dict[str, Any], reason: str
) -> None:
    """Validation runs at the schema boundary so the caller gets a 422 naming
    the field, rather than a row that looks fine until someone tries to use
    it."""
    response = await async_client.patch(contacts_url(profile.id), json=body)

    assert response.status_code == 422, reason
    assert response.headers["content-type"].startswith(PROBLEM_JSON)


async def test_the_same_link_twice_is_a_422_that_names_it(
    failing_client: AsyncClient, profile: CandidateProfile
) -> None:
    """The table is unique on ``(contact_id, url)``, so a repeated address would
    otherwise reach PostgreSQL and come back as a 500 that says nothing about
    which link was the problem."""
    response = await failing_client.patch(
        contacts_url(profile.id),
        json={
            "links": [
                {"kind": "github", "url": "https://github.com/maketov"},
                {"kind": "website", "url": "https://github.com/maketov"},
            ]
        },
    )

    assert response.status_code == 422
    assert "https://github.com/maketov" in response.text


async def test_more_links_than_a_contact_block_can_hold_is_rejected(
    async_client: AsyncClient, profile: CandidateProfile
) -> None:
    """A bound exists so that a scripted client cannot turn one profile into an
    unbounded link farm."""
    response = await async_client.patch(
        contacts_url(profile.id),
        json={"links": [{"url": f"https://example.com/{index}"} for index in range(25)]},
    )

    assert response.status_code == 422


async def test_patching_the_contacts_of_a_profile_that_does_not_exist_is_404(
    async_client: AsyncClient,
) -> None:
    """Creating a block for an id that is not a profile would be an orphan the
    foreign key happens to catch; 404 says what is actually wrong."""
    response = await async_client.patch(contacts_url(uuid4()), json={"city": "Астана"})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)


async def test_a_correction_is_committed_rather_than_only_returned(
    app: FastAPI, profile: CandidateProfile, db_session: AsyncSession
) -> None:
    """The endpoint commits, so the value is still there for the next request
    rather than only for the response being rendered."""

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.patch(contacts_url(profile.id), json={"city": "Астана"})
        second = await client.get(contacts_url(profile.id))
    app.dependency_overrides.clear()

    assert second.json()["city"] == "Астана"


# ── prefilling from the resume ────────────────────────────────────────


async def test_prefill_fills_an_empty_block_from_the_resume(
    db_session: AsyncSession, profile: CandidateProfile
) -> None:
    """The point of the feature: the contact block exists without anybody
    typing it, because everything in it was already on the page."""
    await resume_service.fill_contacts(db_session, profile.id)

    contact = await stored_contact(db_session, profile.id)
    assert contact.full_name == "Кирилл Макетов"
    assert contact.city == "Астана"
    assert contact.email == "k.maketov@example.com"
    assert contact.phone == "+7 700 000 00 15"
    assert await stored_links(db_session, profile.id) == [
        ("github", "https://github.com/maketov", False),
        ("telegram", "https://t.me/maketov_k", False),
    ]
    # Filled by a machine, so nothing is flagged as settled and the next parse
    # is free to improve on it.
    assert contact.phone_edited is False


async def test_prefill_never_overwrites_what_a_person_corrected(
    db_session: AsyncSession, profile: CandidateProfile
) -> None:
    """The promise the ``*_edited`` flags exist for. Re-parsing the same resume
    is the normal path — it happens on every upload — and it must not undo a
    correction made because the parser got it wrong the first time."""
    await contact_service.update_contacts(
        db_session, profile.id, ProfileContactUpdate(phone="+7 707 111 22 33", email=None)
    )

    await resume_service.fill_contacts(db_session, profile.id)

    contact = await stored_contact(db_session, profile.id)
    assert contact.phone == "+7 707 111 22 33"
    # Cleared by hand, and it stays cleared: "not set" would let the resume's
    # address back in.
    assert contact.email is None
    # Fields nobody touched are filled in as usual.
    assert contact.full_name == "Кирилл Макетов"


async def test_prefill_replaces_extracted_links_and_keeps_the_owners(
    db_session: AsyncSession, profile: CandidateProfile
) -> None:
    """Two halves of one list. Re-parsing refreshes what the resume says and
    leaves the links the owner added by hand — including one the resume has
    never mentioned."""
    await resume_service.fill_contacts(db_session, profile.id)
    await contact_service.update_contacts(
        db_session,
        profile.id,
        ProfileContactUpdate(links=[{"kind": "portfolio", "url": "https://maketov.example.com"}]),  # type: ignore[list-item]  # validated on the way in
    )

    await resume_service.fill_contacts(db_session, profile.id)

    stored = await stored_links(db_session, profile.id)
    assert ("portfolio", "https://maketov.example.com", True) in stored
    assert ("github", "https://github.com/maketov", False) in stored
    assert len(stored) == 3


async def test_a_link_the_owner_keeps_is_not_duplicated_by_the_next_parse(
    db_session: AsyncSession, profile: CandidateProfile
) -> None:
    """The resume and the owner can name the same address. Adding it twice
    would trip the unique constraint, and demoting it to an extracted link
    would let the next upload delete something the owner chose to keep."""
    await contact_service.update_contacts(
        db_session,
        profile.id,
        ProfileContactUpdate(links=[{"kind": "github", "url": "https://github.com/maketov"}]),  # type: ignore[list-item]  # validated on the way in
    )

    await resume_service.fill_contacts(db_session, profile.id)

    stored = await stored_links(db_session, profile.id)
    assert stored.count(("github", "https://github.com/maketov", True)) == 1
    assert ("github", "https://github.com/maketov", False) not in stored


async def test_a_resume_that_says_nothing_does_not_erase_what_is_there(
    db_session: AsyncSession, profiles: ProfileRepository
) -> None:
    """A scanned resume has no text layer at all. "Found nothing" is not the
    same as "there is nothing", and treating them alike would wipe a contact
    block the moment someone uploaded a photographed CV."""
    empty = await profiles.create(make_profile(name=None, raw_text=None))
    empty.locations = []
    await db_session.flush()
    await contact_service.update_contacts(
        db_session, empty.id, ProfileContactUpdate(phone="+7 700 000 00 15")
    )

    await resume_service.fill_contacts(db_session, empty.id)

    contact = await stored_contact(db_session, empty.id)
    assert contact.phone == "+7 700 000 00 15"


async def test_prefill_of_a_profile_that_is_gone_does_nothing(db_session: AsyncSession) -> None:
    """The parse runs in a background task and the profile can be deleted while
    it does. Writing a contact row then would leave one with nothing to belong
    to."""
    assert (
        await contact_service.prefill_from_resume(
            db_session, profile_id=uuid4(), raw_text=RESUME_TEXT
        )
        is None
    )


# ── carrying corrections across resumes ───────────────────────────────


async def test_a_new_resume_inherits_the_contacts_the_owner_corrected(
    db_session: AsyncSession, profile: CandidateProfile, profiles: ProfileRepository
) -> None:
    """Uploading a new CV creates a new profile rather than editing the old
    one. Without this the contact block would silently reset on every upload —
    at the exact moment the owner is least likely to check it."""
    await contact_service.update_contacts(
        db_session,
        profile.id,
        ProfileContactUpdate(
            phone="+7 707 111 22 33",
            links=[{"kind": "portfolio", "url": "https://maketov.example.com"}],  # type: ignore[list-item]  # validated on the way in
        ),
    )
    newer = await profiles.create(make_profile(name="Кирилл Макетов", raw_text=RESUME_TEXT))
    await db_session.flush()

    await resume_service.fill_contacts(db_session, newer.id)

    contact = await stored_contact(db_session, newer.id)
    assert contact.phone == "+7 707 111 22 33"
    assert contact.phone_edited is True
    assert ("portfolio", "https://maketov.example.com", True) in await stored_links(
        db_session, newer.id
    )


async def test_what_the_old_resume_merely_said_is_not_carried_forward(
    db_session: AsyncSession, profile: CandidateProfile, profiles: ProfileRepository
) -> None:
    """Only corrections travel. A value the previous profile got from its own
    resume is exactly what the new resume should be allowed to update — that is
    how a changed phone number ever gets picked up."""
    await resume_service.fill_contacts(db_session, profile.id)
    newer = await profiles.create(
        make_profile(
            name="Кирилл Макетов",
            raw_text=RESUME_TEXT.replace("+7 700 000 00 15", "+7 777 222 33 44"),
        )
    )
    await db_session.flush()

    await resume_service.fill_contacts(db_session, newer.id)

    contact = await stored_contact(db_session, newer.id)
    assert contact.phone == "+7 777 222 33 44"
    assert contact.phone_edited is False


async def test_a_block_the_owner_has_already_touched_is_not_overwritten_by_an_older_one(
    db_session: AsyncSession, profile: CandidateProfile, profiles: ProfileRepository
) -> None:
    """Carrying forward is for a block nobody has filled in. Doing it to one
    that is already being edited would take a correction made a minute ago and
    replace it with one made last month."""
    await contact_service.update_contacts(
        db_session, profile.id, ProfileContactUpdate(phone="+7 707 111 22 33")
    )
    newer = await profiles.create(make_profile(name="Кирилл Макетов", raw_text=RESUME_TEXT))
    await db_session.flush()
    await contact_service.update_contacts(
        db_session, newer.id, ProfileContactUpdate(phone="+7 708 999 88 77")
    )

    await resume_service.fill_contacts(db_session, newer.id)

    contact = await stored_contact(db_session, newer.id)
    assert contact.phone == "+7 708 999 88 77"


# ── the storage promises ──────────────────────────────────────────────


async def test_one_profile_can_only_have_one_contact_block(
    db_session: AsyncSession, profile: CandidateProfile
) -> None:
    """A second row would split one person's contacts in two, and whichever the
    query happened to read would look complete."""
    first = await ContactRepository(db_session).ensure(profile.id)
    second = await ContactRepository(db_session).ensure(profile.id)

    assert first.id == second.id


async def test_deleting_a_profile_takes_its_contacts_and_links_with_it(
    db_session: AsyncSession, profile: CandidateProfile, profiles: ProfileRepository
) -> None:
    """Contacts are the most identifying data here, so "delete my resume" has
    to mean the phone number goes too. The cascade is the database's, asserted
    with a count rather than through the ORM's in-memory state."""
    await resume_service.fill_contacts(db_session, profile.id)
    assert await count_rows(db_session, ProfileContact) == 1
    assert await count_rows(db_session, ProfileContactLink) > 0

    assert await profiles.delete(profile.id) is True

    assert await count_rows(db_session, ProfileContact) == 0
    assert await count_rows(db_session, ProfileContactLink) == 0
