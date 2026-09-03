"""Candidate profile persistence."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import CandidateProfile, ProfileSkill
from app.schemas.profile import CandidateProfileCreate, CandidateProfileUpdate, SkillCreate


class ProfileRepository:
    """Reads and writes for candidate profiles and their skills."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, profile: CandidateProfileCreate) -> CandidateProfile:
        """Persist a freshly extracted profile together with its skills."""
        payload = profile.model_dump(exclude={"skills"})
        instance = CandidateProfile(**payload)
        instance.skills = [ProfileSkill(**skill.model_dump()) for skill in profile.skills]
        self.session.add(instance)
        await self.session.flush()
        return instance

    async def get(self, profile_id: UUID) -> CandidateProfile | None:
        """One profile with its skills."""
        stmt = (
            select(CandidateProfile)
            .where(CandidateProfile.id == profile_id)
            .options(selectinload(CandidateProfile.skills))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_active(self) -> CandidateProfile | None:
        """The profile the dashboard scores against.

        v1 is single-user, so "active" is a flag rather than a session. The
        models already key everything on profile_id, so multi-user later is a
        routing change, not a schema change.
        """
        stmt = (
            select(CandidateProfile)
            .where(CandidateProfile.is_active.is_(True))
            .order_by(CandidateProfile.created_at.desc())
            .limit(1)
            .options(selectinload(CandidateProfile.skills))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def update(
        self, profile_id: UUID, changes: CandidateProfileUpdate
    ) -> CandidateProfile | None:
        """Apply manual corrections. Unset fields stay as they are."""
        instance = await self.get(profile_id)
        if instance is None:
            return None
        for field, value in changes.model_dump(exclude_unset=True).items():
            setattr(instance, field, value)
        await self.session.flush()
        return instance

    async def replace_skills(
        self, profile_id: UUID, skills: Sequence[SkillCreate]
    ) -> CandidateProfile | None:
        """Swap the whole skill set.

        Replacing rather than merging: the extractor produces a complete set
        every run, and a merge would keep skills the user has just deleted.

        The clear-and-flush before the reassignment is load-bearing. Assigning
        the new collection in one step lets SQLAlchemy's unit of work emit the
        INSERTs before the orphan DELETEs, so any skill present in both the old
        and the new set violates the (profile_id, canonical_name) unique
        constraint. Re-extracting the same resume overlaps almost completely
        with the previous set, which makes that the normal path, not an edge
        case.
        """
        instance = await self.get(profile_id)
        if instance is None:
            return None
        instance.skills.clear()
        await self.session.flush()
        instance.skills = [ProfileSkill(**skill.model_dump()) for skill in skills]
        await self.session.flush()
        return instance

    async def set_embedding(self, profile_id: UUID, embedding: Sequence[float]) -> None:
        """Store the resume embedding."""
        stmt = (
            sa_update(CandidateProfile)
            .where(CandidateProfile.id == profile_id)
            .values(embedding=list(embedding), updated_at=func.now())
        )
        await self.session.execute(stmt)

    async def delete(self, profile_id: UUID) -> bool:
        """Remove a profile; skills and matches go with it via ON DELETE CASCADE."""
        instance = await self.session.get(CandidateProfile, profile_id)
        if instance is None:
            return False
        await self.session.delete(instance)
        await self.session.flush()
        return True
