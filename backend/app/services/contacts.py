"""The contact block: reading it, correcting it, and filling it from a resume.

Three rules hold everywhere in this module, and every function below is shaped
by them.

**A human's correction is final.** Extraction runs again on every upload, and
it is a heuristic — it will one day read a phone number off the wrong line. A
field the owner has touched carries an edited flag and is never written by
anything but the owner again. That is what makes re-uploading a resume safe.

**Corrections survive the resume they were made on.** Uploading a new CV
creates a new profile rather than editing the old one, so the hand-edited parts
of the previous block are carried forward: the phone number belongs to the
person, not to the document that happened to mention it.

**Nothing here logs a value.** Contacts are the most identifying data this
service holds. Log lines carry the profile id, the *names* of the fields that
were filled and a count of links — never a name, an address or a number. This
is checked by ``backend/tests/test_pii_logging.py``, which fails the build if
one leaks.

Contacts are also never embedded, never scored and never sent anywhere. The
only outbound use is substitution into a document generated for the owner
themselves.
"""

from collections.abc import Sequence
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import ProfileContact
from app.db.repositories.contact import ContactRepository
from app.resume import contacts as contact_extractor
from app.resume.contacts import ExtractedLink
from app.schemas.contact import (
    ContactEdits,
    ContactLinkRead,
    ContactLinkWrite,
    OptionalCity,
    OptionalEmail,
    OptionalFullName,
    OptionalPhone,
    ProfileContactRead,
    ProfileContactUpdate,
)

logger = get_logger(__name__)

#: The scalar fields, each paired with the column recording that a human has
#: settled it. Written out rather than derived from the model so that adding a
#: field is a deliberate act: a new column with no flag beside it would be
#: silently overwritten by the next upload.
EDITABLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("full_name", "full_name_edited"),
    ("phone", "phone_edited"),
    ("email", "email_edited"),
    ("city", "city_edited"),
)

#: The same rules the API applies, applied to what extraction produces.
#:
#: Prefill takes its values from a regex and from a language model, and neither
#: is bound by the column widths: a model that returns a whole address line as
#: the city, or a resume carrying a 4 kB tracking URL, would otherwise reach
#: PostgreSQL as a ``value too long`` and fail a parse that had already
#: succeeded. Validating each candidate on its own means a bad one is dropped
#: and the rest of the block is still filled in.
FIELD_VALIDATORS: dict[str, TypeAdapter[str | None]] = {
    "full_name": TypeAdapter(OptionalFullName),
    "phone": TypeAdapter(OptionalPhone),
    "email": TypeAdapter(OptionalEmail),
    "city": TypeAdapter(OptionalCity),
}
LINK_VALIDATOR: TypeAdapter[ContactLinkWrite] = TypeAdapter(ContactLinkWrite)


def acceptable(field: str, value: str | None) -> str | None:
    """A prefill candidate if it passes the field's own rules, else None.

    Rejections are logged by field name and error count only. The value that
    failed is exactly the kind of string this module may not log — it is
    someone's phone number, however malformed.
    """
    if value is None:
        return None
    try:
        return FIELD_VALIDATORS[field].validate_python(value)
    except ValidationError as invalid:
        logger.warning("contacts.prefill_rejected", field=field, errors=invalid.error_count())
        return None


def acceptable_links(links: Sequence[ExtractedLink]) -> list[ContactLinkWrite]:
    """The extracted links that are storable, in the order they were found."""
    accepted: list[ContactLinkWrite] = []
    for link in links:
        try:
            accepted.append(LINK_VALIDATOR.validate_python({"kind": link.kind, "url": link.url}))
        except ValidationError as invalid:
            logger.warning("contacts.prefill_link_rejected", errors=invalid.error_count())
    return accepted


def to_read_model(contact: ProfileContact) -> ProfileContactRead:
    """Build the API view of a stored contact block."""
    return ProfileContactRead(
        profile_id=contact.profile_id,
        full_name=contact.full_name,
        phone=contact.phone,
        email=contact.email,
        city=contact.city,
        edited=ContactEdits(
            full_name=contact.full_name_edited,
            phone=contact.phone_edited,
            email=contact.email_edited,
            city=contact.city_edited,
        ),
        links=[ContactLinkRead.model_validate(link) for link in contact.links],
        updated_at=contact.updated_at,
    )


async def reread(repository: ContactRepository, profile_id: UUID) -> ProfileContact:
    """Load the block back after writing it, ready to be read attribute by attribute.

    Not paranoia about the write. ``created_at`` and ``updated_at`` are filled
    in by PostgreSQL, so they are expired on the instance the moment a flush
    sends the statement — and reading one then emits a lazy SELECT, which under
    asyncio is a ``MissingGreenlet`` rather than a query. One explicit read,
    with the links eagerly loaded, leaves an object every field of which can be
    touched without surprises.
    """
    contact = await repository.get(profile_id)
    if contact is None:  # pragma: no cover - written and flushed one line above
        raise RuntimeError(f"contact block for profile {profile_id} vanished after writing it")
    return contact


async def get_contacts(session: AsyncSession, profile_id: UUID) -> ProfileContactRead | None:
    """The profile's contact block, or None when there is no such profile.

    A profile that exists but has never had a block written reads as an empty
    one, not as a 404. The distinction matters to the screen this feeds: "we
    have not filled this in yet" is a form to type into, while "no such
    profile" is a dead link.
    """
    repository = ContactRepository(session)
    contact = await repository.get(profile_id)
    if contact is not None:
        return to_read_model(contact)
    if not await repository.profile_exists(profile_id):
        return None
    return ProfileContactRead(profile_id=profile_id)


async def update_contacts(
    session: AsyncSession, profile_id: UUID, changes: ProfileContactUpdate
) -> ProfileContactRead | None:
    """Apply the owner's corrections, or None when there is no such profile.

    Every field named in the request is marked as edited, including one set to
    null: clearing a phone number is a decision, and prefill must not undo it
    on the next upload by helpfully putting the resume's number back.
    """
    repository = ContactRepository(session)
    if not await repository.profile_exists(profile_id):
        return None

    contact = await repository.ensure(profile_id)
    supplied = changes.model_dump(exclude_unset=True)
    touched: list[str] = []
    for field, flag in EDITABLE_FIELDS:
        if field not in supplied:
            continue
        setattr(contact, field, supplied[field])
        setattr(contact, flag, True)
        touched.append(field)

    if changes.links is not None:
        # A hand-edited list is the complete manual set. Links extracted from
        # the resume stay, so removing one here does not mean it is gone for
        # good — it means the owner did not adopt it.
        await repository.replace_links(contact, changes.links, is_manual=True, keep_manual=False)
        touched.append("links")
    await session.flush()
    written = await reread(repository, profile_id)

    logger.info(
        "contacts.updated",
        profile_id=str(profile_id),
        # Field names, never their values.
        fields=touched,
        link_count=len(written.links),
    )
    return to_read_model(written)


async def prefill_from_resume(
    session: AsyncSession,
    *,
    profile_id: UUID,
    raw_text: str | None,
    full_name: str | None = None,
    city: str | None = None,
) -> ProfileContactRead | None:
    """Fill the gaps in a profile's contact block from its resume.

    Runs as part of parsing an upload. ``full_name`` and ``city`` come from the
    model's reading of the document, which sees the page layout; the email,
    phone and links are matched out of the extracted text, which is enough for
    values with a shape.

    What it will not do is overwrite. A field is written only when the owner
    has not settled it *and* the resume actually offers something — an empty
    result never clears a value, because "not found" and "not there" are not
    the same thing, and a scanned resume finds nothing at all.

    Returns None when the profile is gone, which can happen if it was deleted
    while the background parse was running.
    """
    repository = ContactRepository(session)
    if not await repository.profile_exists(profile_id):
        return None

    contact = await repository.ensure(profile_id)
    carried = await _carry_forward(repository, contact)

    found = contact_extractor.extract(raw_text)
    candidates: dict[str, str | None] = {
        "full_name": full_name,
        "phone": found.phone,
        "email": found.email,
        "city": city,
    }

    filled: list[str] = []
    for field, flag in EDITABLE_FIELDS:
        if getattr(contact, flag):
            continue
        value = acceptable(field, candidates.get(field))
        if value is None:
            continue
        setattr(contact, field, value)
        filled.append(field)

    await repository.replace_links(
        contact, acceptable_links(found.links), is_manual=False, keep_manual=True
    )
    await session.flush()
    written = await reread(repository, profile_id)

    logger.info(
        "contacts.prefilled",
        profile_id=str(profile_id),
        # Which fields, how many links. Never what is in them.
        fields=filled,
        carried_forward=carried,
        link_count=len(written.links),
    )
    return to_read_model(written)


async def _carry_forward(repository: ContactRepository, contact: ProfileContact) -> list[str]:
    """Copy the previous profile's hand-edited contacts onto a fresh block.

    Only into a block that is still untouched, and only the fields a human
    actually corrected there. Anything the old block merely inherited from its
    own resume is left behind: this new resume gets to speak for itself, and
    re-extracting is how a genuinely changed phone number gets picked up.

    Returns the names of the fields carried over, for the log line.
    """
    if any(getattr(contact, flag) for _, flag in EDITABLE_FIELDS) or contact.links:
        return []
    previous = await repository.latest_elsewhere(contact.profile_id)
    if previous is None:
        return []

    carried: list[str] = []
    for field, flag in EDITABLE_FIELDS:
        if not getattr(previous, flag):
            continue
        setattr(contact, field, getattr(previous, field))
        setattr(contact, flag, True)
        carried.append(field)

    manual = [
        ContactLinkWrite(kind=link.kind, url=link.url, label=link.label)
        for link in previous.links
        if link.is_manual
    ]
    if manual:
        await repository.replace_links(contact, manual, is_manual=True, keep_manual=False)
        carried.append("links")
    return carried
