"""Filling ``vacancy_skill`` and ``vacancy.min_years`` from what the crawl stored.

One function, two callers. The pipeline runs it over the ids it has just
written, so a fresh vacancy is scoreable the moment it lands; a script runs it
over everything, so the corpus already collected does not have to be crawled
again to become scoreable. Writing it twice — one path for new rows and one for
old — is how the two quietly stop agreeing about what a skill is.

It reads ``vacancy_source.raw["_derived"]`` rather than taking values from a
caller, which is what makes that possible: the payload is already in the
database, so "backfill" and "keep up to date" are the same operation over
different id sets.

**Skills are replaced, not merged.** A vacancy that dropped a requirement should
stop asking for it — hh edits postings in place and the sitemap's ``lastmod``
moves when it happens — and a merge would accumulate every requirement the
posting ever had, which reads as a job wanting nine languages.
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import delete, select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import uuid7
from app.db.models import Vacancy, VacancySkill, VacancySource
from app.normalize.requirements import min_years, skill_names

logger = structlog.get_logger(__name__)

#: Vacancies read per round trip while backfilling. Large enough that 643 rows
#: are a handful of statements, small enough that one bad payload is a small
#: transaction to lose.
CHUNK = 200


@dataclass(slots=True)
class SyncOutcome:
    """What one pass over the corpus changed, for a report a person reads."""

    considered: int = 0
    skills_written: int = 0
    years_written: int = 0
    #: Vacancies that ended with no skill rows. Not a failure and not one cause:
    #: a posting can carry no ``_derived`` at all, carry one whose ``key_skills``
    #: is absent or empty, or carry a list that was entirely language
    #: requirements. Measured over this corpus, 449 of 643 land here — the
    #: employer simply left the field blank, which on hh is the common case.
    without_skills: int = 0

    def as_dict(self) -> dict[str, int]:
        """The counters, for logging."""
        return asdict(self)


async def sync_requirements(
    session: AsyncSession, *, vacancy_ids: Sequence[UUID] | None = None
) -> SyncOutcome:
    """Derive skills and required experience for these vacancies, or for all of them.

    ``None`` means the whole corpus, which is the backfill. A sequence is what
    the pipeline passes after a batch write.
    """
    outcome = SyncOutcome()
    ids = list(vacancy_ids) if vacancy_ids is not None else await _all_ids(session)
    for start in range(0, len(ids), CHUNK):
        await _sync_chunk(session, ids[start : start + CHUNK], outcome)
    logger.info("normalize.requirements", **outcome.as_dict())
    return outcome


async def _all_ids(session: AsyncSession) -> list[UUID]:
    """Every vacancy that has a source payload to read."""
    rows = await session.execute(select(VacancySource.vacancy_id).distinct())
    return [row[0] for row in rows.all()]


async def _sync_chunk(session: AsyncSession, ids: Sequence[UUID], outcome: SyncOutcome) -> None:
    """One round trip's worth, in three statements."""
    if not ids:
        return
    rows = await session.execute(
        select(VacancySource.vacancy_id, VacancySource.raw).where(VacancySource.vacancy_id.in_(ids))
    )

    # A vacancy can carry more than one source row — the same job cross-posted —
    # so the requirements of each are pooled rather than one arbitrarily winning.
    skills: dict[UUID, list[str]] = {}
    years: dict[UUID, Any] = {}
    seen: set[UUID] = set()
    for vacancy_id, raw in rows.all():
        seen.add(vacancy_id)
        derived = (raw or {}).get("_derived") if isinstance(raw, dict) else None
        if not isinstance(derived, dict):
            continue
        for name in skill_names(derived):
            skills.setdefault(vacancy_id, [])
            if name not in skills[vacancy_id]:
                skills[vacancy_id].append(name)
        stated = min_years(derived)
        if stated is not None and vacancy_id not in years:
            years[vacancy_id] = stated

    outcome.considered += len(seen)
    outcome.without_skills += len(seen) - len(skills)

    await session.execute(delete(VacancySkill).where(VacancySkill.vacancy_id.in_(ids)))
    payload = [
        {
            "id": uuid7(),
            "vacancy_id": vacancy_id,
            "canonical_name": name,
            # hh's ``keySkills`` carries no required/nice split, so every entry
            # is a requirement at full weight — which is what docs/MATCHING.md
            # prescribes for a source that hands the list over structured.
            "is_required": True,
            "weight": 1,
        }
        for vacancy_id, names in skills.items()
        for name in names
    ]
    if payload:
        await session.execute(pg_insert(VacancySkill).values(payload))
        outcome.skills_written += len(payload)

    for value, group in _grouped(years).items():
        await session.execute(
            sa_update(Vacancy).where(Vacancy.id.in_(group)).values(min_years=value)
        )
        outcome.years_written += len(group)


def _grouped(years: dict[UUID, Any]) -> dict[Any, list[UUID]]:
    """Ids by the value they get, so one statement serves each of the four bands.

    hh states experience as one of four, so a chunk of two hundred vacancies
    needs at most four updates rather than two hundred.
    """
    grouped: dict[Any, list[UUID]] = {}
    for vacancy_id, value in years.items():
        grouped.setdefault(value, []).append(vacancy_id)
    return grouped
