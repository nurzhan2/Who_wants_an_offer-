"""Storing the job titles the owner searches for, and carrying them to a new resume.

The titles are typed by hand; a new file is not a new job search, and losing
them on upload would silently put the search back on skill keywords.
"""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CandidateProfile
from app.db.repositories.profile import ProfileRepository
from factories import make_profile


async def stored_profile(
    profiles: ProfileRepository,
    session: AsyncSession,
    *,
    titles: list[str] | None = None,
    active: bool = True,
) -> CandidateProfile:
    """A profile row with titles set on it; the extraction schema carries none."""
    instance = await profiles.create(make_profile())
    instance.target_titles = list(titles or [])
    instance.is_active = active
    await session.flush()
    return instance


async def test_patch_stores_the_cleaned_list(
    async_client: AsyncClient, profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """What the owner typed, cleaned, is what the row holds."""
    profile = await stored_profile(profiles, db_session)

    response = await async_client.patch(
        f"/api/v1/profile/{profile.id}",
        json={"target_titles": [" Python Developer", "python developer", "Junior Python"]},
    )

    assert response.status_code == 200
    assert response.json()["target_titles"] == ["Python Developer", "Junior Python"]
    stored = await db_session.scalar(
        select(CandidateProfile.target_titles).where(CandidateProfile.id == profile.id)
    )
    assert stored == ["Python Developer", "Junior Python"]


async def test_a_new_resume_inherits_the_live_profiles_titles(
    profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """Uploading a new file is not a new job search."""
    old = await stored_profile(profiles, db_session, titles=["Python Developer"])
    new = await stored_profile(profiles, db_session, active=False)
    await profiles.activate(new.id)

    inherited = await profiles.inherit_target_titles(new.id)
    await profiles.deactivate_others(new.id)

    assert inherited == ["Python Developer"]
    refreshed = await profiles.get(new.id)
    assert refreshed is not None
    assert refreshed.target_titles == ["Python Developer"]
    assert old.id != new.id


async def test_a_profile_with_its_own_titles_keeps_them(
    profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """Inheritance fills a gap; it never overwrites a choice."""
    await stored_profile(profiles, db_session, titles=["Python Developer"])
    new = await stored_profile(profiles, db_session, titles=["Data Engineer"])

    assert await profiles.inherit_target_titles(new.id) == ["Data Engineer"]


async def test_nothing_to_inherit_is_an_empty_list(
    profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """The first resume ever has nobody to inherit from."""
    only = await stored_profile(profiles, db_session)

    assert await profiles.inherit_target_titles(only.id) == []
