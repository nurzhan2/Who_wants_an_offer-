"""Reading the rows a letter is written from, and writing the letter back.

Every query here is explicit about its columns because ``Vacancy.matches``,
``Vacancy.applications`` and ``Match.vacancy`` are all ``lazy="raise"`` — the
models make an accidental N+1 an error rather than a slow afternoon — so a
letter run over a hundred vacancies has to say what it wants.

The queries live in this package rather than under ``db/repositories/`` for the
same reason ``app/sources/hh.py`` keeps its own payload models: they answer one
feature's questions and nothing else asks them. If a second caller ever needs
the queue, that is the moment to move it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.base import uuid7
from app.db.models import (
    Application,
    CandidateProfile,
    Match,
    ProfileSkill,
    Vacancy,
    VacancySkill,
    VacancySource,
)
from app.letters.context import (
    ProfileFacts,
    SkillFact,
    VacancyFacts,
    letter_max_length,
    role_names,
)

logger = get_logger(__name__)

#: hh keeps its structured requirement list here. Read through the same helper
#: the rest of the payload is read through, so a source that nests it elsewhere
#: still works.
DERIVED_KEY = "_derived"


@dataclass(frozen=True, slots=True)
class QueuedVacancy:
    """One vacancy waiting for a letter, with what put it in the queue."""

    vacancy_id: UUID
    title: str
    company: str | None
    score: Decimal
    #: True when an application row already holds a letter for this vacancy.
    has_letter: bool


async def load_vacancy_facts(session: AsyncSession, vacancy_id: UUID) -> VacancyFacts | None:
    """Everything about one vacancy the letter may use, or None if it is gone."""
    vacancy = await session.get(Vacancy, vacancy_id)
    if vacancy is None:
        return None

    raws = [
        source.raw
        for source in (await session.scalars(_sources_of(vacancy_id))).all()
        if isinstance(source.raw, dict)
    ]
    derived = [raw[DERIVED_KEY] for raw in raws if isinstance(raw.get(DERIVED_KEY), dict)]

    return VacancyFacts(
        vacancy_id=vacancy.id,
        title=vacancy.title,
        company=vacancy.company,
        city=vacancy.city,
        description=vacancy.description_md or vacancy.description_raw,
        key_skills=await _required_skills(session, vacancy_id, derived),
        language_requirements=_strings(derived, "language_requirements"),
        work_experience=_work_experience(derived),
        professional_roles=role_names(raws),
        letter_max_length=letter_max_length(raws),
    )


async def load_profile_facts(
    session: AsyncSession, profile_id: UUID | None = None
) -> ProfileFacts | None:
    """The candidate the letters are for: the named profile, or the active one."""
    if profile_id is None:
        profile = await session.scalar(
            select(CandidateProfile)
            .where(CandidateProfile.is_active.is_(True))
            .order_by(CandidateProfile.created_at.desc())
            .limit(1)
        )
    else:
        profile = await session.get(CandidateProfile, profile_id)
    if profile is None:
        return None

    skills = (
        await session.scalars(
            select(ProfileSkill)
            .where(ProfileSkill.profile_id == profile.id)
            .order_by(ProfileSkill.canonical_name)
        )
    ).all()

    return ProfileFacts(
        profile_id=profile.id,
        name=profile.name,
        headline=profile.headline,
        summary=profile.summary,
        seniority=profile.seniority.value if profile.seniority else None,
        total_years=float(profile.total_years) if profile.total_years is not None else None,
        locations=tuple(str(item) for item in profile.locations),
        languages=_languages(profile.languages),
        skills=tuple(_skill_fact(skill) for skill in skills),
    )


async def queue(
    session: AsyncSession,
    *,
    profile_id: UUID,
    limit: int = 10,
    min_score: Decimal = Decimal("70"),
    include_written: bool = False,
) -> list[QueuedVacancy]:
    """The best-scoring vacancies that still need a letter, best first.

    "Still need" means no application row holds one. A vacancy whose letter was
    already written is skipped rather than rewritten, so a batch run is safe to
    repeat — the same idempotence rule the connectors follow, for the same
    reason: the expensive call is the one worth not making twice.
    """
    written = (
        select(Application.vacancy_id)
        .where(Application.cover_letter.is_not(None))
        .where(Application.vacancy_id == Match.vacancy_id)
    )
    stmt = (
        select(
            Match.vacancy_id,
            Vacancy.title,
            Vacancy.company,
            Match.score,
            written.exists().label("has_letter"),
        )
        .join(Vacancy, Vacancy.id == Match.vacancy_id)
        .where(Match.profile_id == profile_id)
        .where(Match.score >= min_score)
        .where(Vacancy.is_active.is_(True))
        .where(Vacancy.is_spam.is_(False))
        .order_by(Match.score.desc(), Match.vacancy_id)
        .limit(limit)
    )
    if not include_written:
        stmt = stmt.where(~written.exists())

    rows = (await session.execute(stmt)).all()
    return [
        QueuedVacancy(
            vacancy_id=row.vacancy_id,
            title=row.title,
            company=row.company,
            score=row.score,
            has_letter=bool(row.has_letter),
        )
        for row in rows
    ]


async def save_letter(session: AsyncSession, *, vacancy_id: UUID, text: str) -> tuple[UUID, bool]:
    """Store the letter on this vacancy's application row, and say what happened.

    Returns the row's id and whether it had to be created. The tracker has no
    unique constraint on ``vacancy_id`` — a person may legitimately track two
    attempts at the same job — so this updates the oldest row rather than
    inserting a second one, which is what keeps a repeated batch run idempotent.

    The letter is saved, never sent. Sending is ``agent/``'s, after a human
    confirms it, and nothing in ``backend/`` can do it.
    """
    application = await session.scalar(
        select(Application)
        .where(Application.vacancy_id == vacancy_id)
        .order_by(Application.created_at, Application.id)
        .limit(1)
    )
    if application is None:
        application = Application(id=uuid7(), vacancy_id=vacancy_id, cover_letter=text)
        session.add(application)
        await session.flush()
        return application.id, True

    application.cover_letter = text
    await session.flush()
    return application.id, False


async def existing_letter(session: AsyncSession, vacancy_id: UUID) -> str | None:
    """The letter already stored for this vacancy, if there is one."""
    return await session.scalar(
        select(Application.cover_letter)
        .where(Application.vacancy_id == vacancy_id)
        .where(Application.cover_letter.is_not(None))
        .order_by(Application.created_at, Application.id)
        .limit(1)
    )


def _sources_of(vacancy_id: UUID) -> Select[tuple[VacancySource]]:
    """Every posting this vacancy was deduplicated from."""
    return select(VacancySource).where(VacancySource.vacancy_id == vacancy_id)


async def _required_skills(
    session: AsyncSession, vacancy_id: UUID, derived: list[dict[str, Any]]
) -> tuple[str, ...]:
    """The vacancy's requirement list, from wherever this row actually holds it.

    Normalised ``vacancy_skill`` rows first, because that is the schema of
    record. Nothing writes them today — phase 3 stores hh's ``keySkills`` in the
    source payload and normalisation into rows is phase 4's — so in practice the
    second branch runs, and it keeps running correctly on the day the first one
    starts producing rows.

    Required skills lead, but nice-to-haves are kept: they are still things the
    employer asked for, and a letter that answers one is answering the vacancy.
    """
    rows = (
        await session.scalars(
            select(VacancySkill)
            .where(VacancySkill.vacancy_id == vacancy_id)
            .order_by(VacancySkill.is_required.desc(), VacancySkill.canonical_name)
        )
    ).all()
    if rows:
        return tuple(row.canonical_name for row in rows)
    return _strings(derived, "key_skills")


def _strings(derived: list[dict[str, Any]], key: str) -> tuple[str, ...]:
    """One string list out of the derived blocks, deduplicated, order kept."""
    values: list[str] = []
    for block in derived:
        for item in block.get(key) or []:
            if isinstance(item, str) and item.strip() and item.strip() not in values:
                values.append(item.strip())
    return tuple(values)


def _work_experience(derived: list[dict[str, Any]]) -> str | None:
    """What the vacancy asks for, rendered if the payload rendered it.

    hh ships ``workExperience`` as a code (``between1And3``) and its own Russian
    rendering in the page's translations, which the connector keeps in
    ``labels``. The rendering is what a letter can use; the code is a fallback
    so something true is passed on rather than nothing.
    """
    for block in derived:
        labels = block.get("labels")
        if isinstance(labels, dict):
            rendered = labels.get("workExperience")
            if isinstance(rendered, str) and rendered.strip():
                return rendered.strip()
    for block in derived:
        code = block.get("work_experience")
        if isinstance(code, str) and code.strip():
            return code.strip()
    return None


def _languages(stored: Sequence[object]) -> tuple[str, ...]:
    """The profile's languages as "en C1" strings.

    The level vocabulary is not this project's — CEFR from one resume, "native"
    from another — so it is passed through rather than mapped.

    ``Sequence[object]`` rather than the column's declared
    ``list[dict[str, Any]]``: the annotation on a JSONB column is a promise the
    database does not enforce, and this list was last written by an LLM
    extraction. Checking what actually arrived costs one line.
    """
    rendered: list[str] = []
    for item in stored:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        level = str(item.get("level") or "").strip()
        text = f"{code} {level}".strip()
        if text:
            rendered.append(text)
    return tuple(rendered)


def _skill_fact(skill: ProfileSkill) -> SkillFact:
    """One profile skill, preferring the spelling the resume actually used.

    ``canonical_name`` is a lookup key: it is lowercase and stripped of
    punctuation, so a letter written from it says "postgresql" where the person
    wrote "PostgreSQL". The first raw name is what they wrote.
    """
    spelling = next(
        (name for name in skill.raw_names if isinstance(name, str) and name.strip()),
        skill.canonical_name,
    )
    return SkillFact(
        canonical_name=skill.canonical_name,
        spelling=spelling.strip(),
        years=float(skill.years) if skill.years is not None else None,
        level=skill.level.value,
    )
