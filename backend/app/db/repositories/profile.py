"""Candidate profile persistence."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.enums import ParseStatus
from app.db.models import CandidateProfile, ProfileExperience, ProfileSkill
from app.schemas.ats import ATSReport
from app.schemas.profile import (
    CandidateProfileCreate,
    CandidateProfileUpdate,
    ExperienceCreate,
    SkillCreate,
    SkillElsewhere,
)


class ProfileRepository:
    """Reads and writes for candidate profiles and their skills."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, profile: CandidateProfileCreate) -> CandidateProfile:
        """Persist a freshly extracted profile together with its skills."""
        payload = profile.model_dump(exclude={"skills", "experience"})
        instance = CandidateProfile(**payload)
        instance.skills = [ProfileSkill(**skill.model_dump()) for skill in profile.skills]
        instance.experience = [ProfileExperience(**job.model_dump()) for job in profile.experience]
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

    async def list_all(self) -> list[CandidateProfile]:
        """Every resume ever uploaded, newest first, with its skills.

        The documents screen shows all of them, not only the active one: an
        older resume is what the "this skill is not in *this* CV" reading is
        measured against, and a resume whose extraction failed is exactly the
        row a person needs to see.
        """
        stmt = (
            select(CandidateProfile)
            .order_by(CandidateProfile.created_at.desc())
            .options(selectinload(CandidateProfile.skills))
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def skills_elsewhere(self, exclude_profile_id: UUID) -> list[SkillElsewhere]:
        """Skills that some *other* resume of this owner lists.

        The evidence behind the middle case of the vacancy card: a requirement
        the scorer counted as missing may be something this person does, which
        the resume in hand simply never named. An earlier CV saying so is the
        only evidence in this database that is not the CV being scored.

        Newest resume first, so the caller showing one of them shows the most
        recent claim rather than an arbitrary one.
        """
        stmt = (
            select(
                ProfileSkill.canonical_name,
                ProfileSkill.profile_id,
                CandidateProfile.resume_filename,
                CandidateProfile.name,
            )
            .join(CandidateProfile, CandidateProfile.id == ProfileSkill.profile_id)
            .where(ProfileSkill.profile_id != exclude_profile_id)
            .order_by(CandidateProfile.created_at.desc(), ProfileSkill.canonical_name)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            SkillElsewhere(
                canonical_name=row.canonical_name,
                profile_id=row.profile_id,
                resume_filename=row.resume_filename,
                profile_name=row.name,
            )
            for row in rows
        ]

    async def update(
        self, profile_id: UUID, changes: CandidateProfileUpdate
    ) -> CandidateProfile | None:
        """Apply manual corrections. Unset fields stay as they are."""
        instance = await self.get(profile_id)
        if instance is None:
            return None

        fields = changes.model_dump(exclude_unset=True)
        # Skills are a relationship, not a column: assigning the dumped dicts
        # would replace ORM objects with plain mappings. They go through
        # replace_skills, which handles the delete-before-insert ordering.
        skills = fields.pop("skills", None)
        for field, value in fields.items():
            setattr(instance, field, value)
        await self.session.flush()

        if skills is not None and changes.skills is not None:
            await self.replace_skills(profile_id, changes.skills)
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

    async def replace_experience(
        self, profile_id: UUID, experience: Sequence[ExperienceCreate]
    ) -> CandidateProfile | None:
        """Swap the whole set of jobs.

        Replacing rather than merging, for the reason ``replace_skills`` gives:
        the extractor produces a complete list every run, and a merge would keep
        a job the person has just removed from their resume — which, in a
        document generated from these rows, is an employer the candidate no
        longer claims still appearing on their CV.

        The clear-and-flush before the reassignment is load-bearing in exactly
        the way it is for skills: assigning the new collection in one step lets
        the unit of work emit INSERTs before the orphan DELETEs, and any
        ``position`` present in both sets then violates the
        ``(profile_id, position)`` unique constraint. Re-parsing the same resume
        overlaps completely, so that is the normal path rather than an edge case.
        """
        instance = await self.get(profile_id)
        if instance is None:
            return None
        instance.experience.clear()
        await self.session.flush()
        instance.experience = [ProfileExperience(**job.model_dump()) for job in experience]
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

    async def set_headline_embedding(self, profile_id: UUID, embedding: Sequence[float]) -> None:
        """Store the headline embedding, the profile's side of title similarity."""
        stmt = (
            sa_update(CandidateProfile)
            .where(CandidateProfile.id == profile_id)
            .values(headline_embedding=list(embedding), updated_at=func.now())
        )
        await self.session.execute(stmt)

    async def create_pending(
        self,
        *,
        filename: str,
        size_bytes: int,
        source_format: str,
        started_at: datetime,
        ats_report: ATSReport | None = None,
    ) -> CandidateProfile:
        """Reserve a profile row before parsing starts.

        The upload endpoint answers with this id immediately, so the client has
        something to poll while the background task works. The row stays
        inactive until parsing succeeds — an empty profile must never become
        the one the dashboard scores against.
        """
        instance = CandidateProfile(
            parse_status=ParseStatus.PENDING,
            parse_started_at=started_at,
            resume_filename=filename,
            resume_size_bytes=size_bytes,
            resume_format=source_format,
            # Stored now because the staged file is deleted when parsing ends.
            ats_report=ats_report.model_dump(mode="json") if ats_report else None,
            is_active=False,
        )
        self.session.add(instance)
        await self.session.flush()
        return instance

    async def set_ats_report(self, profile_id: UUID, report: ATSReport) -> None:
        """Replace the report stored at upload with the fuller one.

        The upload could only judge the file. Once extraction has finished there
        is a second reading to compare against, so the report is recomputed and
        overwritten rather than merged: one function builds it, in one place,
        and a half-updated report would be a shape nothing else produces.
        """
        await self.session.execute(
            sa_update(CandidateProfile)
            .where(CandidateProfile.id == profile_id)
            .values(ats_report=report.model_dump(mode="json"), updated_at=func.now())
        )

    async def get_ats_report(self, profile_id: UUID) -> ATSReport | None:
        """The stored readability report, or None if none was recorded.

        Validated on the way out rather than trusted: the column is JSONB
        written by an older version of the schema for older profiles.
        """
        stmt = select(CandidateProfile.ats_report).where(CandidateProfile.id == profile_id)
        stored = (await self.session.execute(stmt)).scalar_one_or_none()
        return ATSReport.model_validate(stored) if stored else None

    async def update_from_extraction(
        self, profile_id: UUID, payload: CandidateProfileCreate
    ) -> None:
        """Write an extraction onto the reserved row.

        Skills and jobs are handled separately by ``replace_skills`` and
        ``replace_experience`` — both are relationships, and both are replaced
        wholesale rather than merged. Everything else is a plain column update.
        """
        values = payload.model_dump(exclude={"skills", "experience"})
        values["updated_at"] = func.now()
        await self.session.execute(
            sa_update(CandidateProfile).where(CandidateProfile.id == profile_id).values(**values)
        )

    async def activate(self, profile_id: UUID) -> None:
        """Make this profile the one the dashboard scores against.

        Separate from ``deactivate_others`` and called before it: the row is
        created inactive so a half-parsed profile can never be the live one, and
        something has to switch it on once parsing has actually succeeded.
        Forgetting this leaves the old profile retired and the new one inactive,
        so ``get_active`` returns nothing at all.
        """
        await self.session.execute(
            sa_update(CandidateProfile)
            .where(CandidateProfile.id == profile_id)
            .values(is_active=True, updated_at=func.now())
        )

    async def inherit_target_titles(self, profile_id: UUID) -> list[str]:
        """Give a new profile the job titles the live one was searching for.

        Called before ``deactivate_others``, while the previous profile is still
        the active one. The titles are the owner's intent, typed by hand, and a
        new resume file does not change what they want to be hired as; losing
        the list on every upload would quietly return the search to skill
        keywords. A profile that already holds titles keeps its own.
        """
        instance = await self.get(profile_id)
        if instance is None or instance.target_titles:
            return list(instance.target_titles) if instance is not None else []
        stmt = (
            select(CandidateProfile.target_titles)
            .where(CandidateProfile.id != profile_id, CandidateProfile.is_active.is_(True))
            .order_by(CandidateProfile.updated_at.desc())
            .limit(1)
        )
        previous = (await self.session.execute(stmt)).scalar_one_or_none()
        if not previous:
            return []
        instance.target_titles = list(previous)
        await self.session.flush()
        return list(previous)

    async def deactivate_others(self, keep_id: UUID) -> int:
        """Retire every other profile so exactly one is live.

        Uploading a new resume supersedes the old profile rather than editing
        it: the matches, applications and scores attached to the old one stay
        readable instead of being silently rewritten.
        """
        stmt = (
            sa_update(CandidateProfile)
            .where(CandidateProfile.id != keep_id, CandidateProfile.is_active.is_(True))
            .values(is_active=False, updated_at=func.now())
        )
        result = cast("CursorResult[Any]", await self.session.execute(stmt))
        return int(result.rowcount or 0)

    async def set_parse_status(
        self,
        profile_id: UUID,
        status: ParseStatus,
        *,
        error: str | None = None,
        started_at: datetime | None = None,
    ) -> None:
        """Record where extraction got to.

        ``error`` reaches the API and the logs, so callers must pass a reason,
        never a fragment of the resume.
        """
        values: dict[str, Any] = {
            "parse_status": status,
            "parse_error": error,
            "updated_at": func.now(),
        }
        if started_at is not None:
            values["parse_started_at"] = started_at
        await self.session.execute(
            sa_update(CandidateProfile).where(CandidateProfile.id == profile_id).values(**values)
        )

    async def fail_stale_pending(self, cutoff: datetime, reason: str) -> int:
        """Fail profiles whose parse has been pending since before ``cutoff``.

        Extraction runs in a FastAPI background task, which does not survive a
        restart. Without this a killed process leaves a profile pending for
        ever, and the dashboard spins on it.
        """
        stmt = (
            sa_update(CandidateProfile)
            .where(
                CandidateProfile.parse_status == ParseStatus.PENDING,
                CandidateProfile.parse_started_at < cutoff,
            )
            .values(parse_status=ParseStatus.FAILED, parse_error=reason, updated_at=func.now())
        )
        result = cast("CursorResult[Any]", await self.session.execute(stmt))
        return int(result.rowcount or 0)

    async def delete(self, profile_id: UUID) -> bool:
        """Remove a profile; skills and matches go with it via ON DELETE CASCADE."""
        instance = await self.session.get(CandidateProfile, profile_id)
        if instance is None:
            return False
        await self.session.delete(instance)
        await self.session.flush()
        return True
