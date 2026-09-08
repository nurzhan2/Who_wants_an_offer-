"""Contact block persistence.

Everything that touches ``profile_contact`` goes through here, and nothing else
in the data layer touches it at all. :class:`app.db.models.CandidateProfile`
deliberately has no relationship pointing at these rows, so this repository is
the only way to reach a phone number — which is the point: the privacy boundary
is a single, greppable module rather than a convention.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import CandidateProfile, ProfileContact, ProfileContactLink
from app.schemas.contact import ContactLinkWrite


class ContactRepository:
    """Reads and writes for a profile's contact block and its links."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, profile_id: UUID) -> ProfileContact | None:
        """The contact block of one profile, links included."""
        stmt = (
            select(ProfileContact)
            .where(ProfileContact.profile_id == profile_id)
            .options(selectinload(ProfileContact.links))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def profile_exists(self, profile_id: UUID) -> bool:
        """Whether the profile these contacts would belong to is there at all.

        Asked before creating a block: a contact row for a profile that does
        not exist would be an orphan the FK happens to reject, and a 404 is a
        better answer than an integrity error.
        """
        stmt = select(CandidateProfile.id).where(CandidateProfile.id == profile_id)
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None

    async def ensure(self, profile_id: UUID) -> ProfileContact:
        """The profile's contact block, created empty if it has none yet.

        Callers have already established that the profile exists; this is not
        the place to decide what a missing one means.
        """
        existing = await self.get(profile_id)
        if existing is not None:
            return existing
        # ``links=[]`` rather than leaving it to the default: once the flush
        # below makes this row persistent, an untouched collection attribute is
        # not "empty" but "not loaded", and the next read of it emits a lazy
        # SELECT — which under asyncio is a MissingGreenlet, not a query.
        instance = ProfileContact(profile_id=profile_id, links=[])
        self.session.add(instance)
        await self.session.flush()
        return instance

    async def latest_elsewhere(self, exclude_profile_id: UUID) -> ProfileContact | None:
        """The newest contact block belonging to some other profile.

        Uploading a resume creates a *new* profile rather than editing the old
        one, so without this every upload would start the contact block from
        scratch and quietly drop corrections the owner made last month. The
        caller copies only the hand-edited parts forward; see
        ``app.services.contacts.prefill_from_resume``.
        """
        stmt = (
            select(ProfileContact)
            .where(ProfileContact.profile_id != exclude_profile_id)
            .order_by(ProfileContact.updated_at.desc())
            .limit(1)
            .options(selectinload(ProfileContact.links))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def replace_links(
        self,
        contact: ProfileContact,
        links: Sequence[ContactLinkWrite],
        *,
        is_manual: bool,
        keep_manual: bool,
    ) -> None:
        """Rewrite one half of the link set, leaving the other half alone.

        Two callers, two halves. The API replaces the manual links and keeps
        what extraction found; prefill replaces the extracted links and keeps
        what the owner typed. ``keep_manual`` says which half survives.

        The clear-and-flush before the reassignment is load-bearing, for the
        same reason it is in ``ProfileRepository.replace_skills``: assigning a
        new collection in one step lets the unit of work emit INSERTs before
        the orphan DELETEs, and any URL present in both sets then trips the
        ``(contact_id, url)`` unique constraint. Re-parsing the same resume
        produces exactly that overlap, so it is the normal path.
        """
        kept = [link for link in contact.links if link.is_manual is keep_manual]
        contact.links.clear()
        await self.session.flush()

        rebuilt = [
            ProfileContactLink(
                kind=link.kind,
                url=link.url,
                label=link.label,
                is_manual=link.is_manual,
                position=position,
            )
            for position, link in enumerate(kept)
        ]
        taken = {link.url for link in kept}
        for link in links:
            if link.url in taken:
                # The owner already keeps this address by hand. Re-adding it as
                # an extracted link would violate the unique constraint and
                # would also demote a link they chose to one the next upload
                # may replace.
                continue
            taken.add(link.url)
            rebuilt.append(
                ProfileContactLink(
                    kind=link.kind,
                    url=link.url,
                    label=link.label,
                    is_manual=is_manual,
                    position=len(rebuilt),
                )
            )
        contact.links = rebuilt
        await self.session.flush()
