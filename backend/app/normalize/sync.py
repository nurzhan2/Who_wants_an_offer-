"""Filling ``vacancy_skill`` and ``vacancy.min_years`` from what the crawl stored.

One function, two callers. The pipeline runs it over the ids it has just
written, so a fresh vacancy is scoreable the moment it lands; a script runs it
over everything, so the corpus already collected does not have to be crawled
again to become scoreable. Writing it twice — one path for new rows and one for
old — is how the two quietly stop agreeing about what a skill is.

It reads ``vacancy_source.raw["_derived"]`` and ``vacancy.description_raw``
rather than taking values from a caller, which is what makes that possible:
both are already in the database, so "backfill" and "keep up to date" are the
same operation over different id sets.

**Two kinds of requirement, never mixed.** ``_derived.key_skills`` is a list the
employer typed into hh's own field; the description is prose this project reads
with :mod:`app.normalize.description`. Each row records which it is in
``vacancy_skill.source`` and is priced accordingly — 1.00 against 0.60, the
numbers ``docs/MATCHING.md`` has always specified for a stated requirement and a
mention. Where both name the same skill the employer's list wins the row: their
statement is not improved by our having also found the word.

**Skills are replaced, not merged.** A vacancy that dropped a requirement should
stop asking for it — hh edits postings in place and the sitemap's ``lastmod``
moves when it happens — and a merge would accumulate every requirement the
posting ever had, which reads as a job wanting nine languages.
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import delete, select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import uuid7
from app.db.enums import RequirementSource
from app.db.models import Vacancy, VacancySkill, VacancySource
from app.normalize.description import skills_in_text
from app.normalize.requirements import min_years, skill_names

logger = structlog.get_logger(__name__)

#: Vacancies read per round trip while backfilling. Large enough that 1958 rows
#: are a handful of statements, small enough that one bad payload is a small
#: transaction to lose.
CHUNK = 200

#: What a requirement the employer listed themselves is worth, and what one
#: found in their prose is worth. Both from ``docs/MATCHING.md``; the reasoning
#: for the second, and for what it does and does not change, is there.
FIELD_WEIGHT = Decimal("1.00")
TEXT_WEIGHT = Decimal("0.60")


@dataclass(frozen=True, slots=True)
class _Requirement:
    """One row-to-be, before it is a row."""

    canonical_name: str
    is_required: bool
    weight: Decimal
    source: RequirementSource


@dataclass(slots=True)
class SyncOutcome:
    """What one pass over the corpus changed, for a report a person reads."""

    considered: int = 0
    skills_written: int = 0
    years_written: int = 0
    #: Vacancies that ended with no skill rows at all. Not a failure and not one
    #: cause: a posting can carry no ``_derived``, carry one whose ``key_skills``
    #: is absent or empty, or carry a list that was entirely language
    #: requirements — and now also have a description that names nothing this
    #: dictionary knows, which is what a vacancy for a driver looks like.
    without_skills: int = 0
    #: Vacancies whose employer left the structured field empty. The 893 of the
    #: live corpus this whole change is about, counted before the description is
    #: read, so that "how many were rescued" stays answerable after it is — on
    #: 9 September 2026 the answer was 450.
    without_field_skills: int = 0
    #: Rows by where they came from. These sum to ``skills_written``.
    from_field: int = 0
    from_text: int = 0
    #: Vacancies that would have had nothing without the description.
    rescued_by_text: int = 0
    #: Text findings the description marked optional («будет плюсом»), and ones
    #: it denied outright («не требуется»). The second is written nowhere; it is
    #: counted because whether these rules are worth their code is a question
    #: about this corpus, and this is the measurement that answers it.
    optional_from_text: int = 0
    negated_in_text: int = 0

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
    """One round trip's worth: read both sides, decide, write."""
    if not ids:
        return

    stated, years, from_payloads = await _from_payloads(session, ids)
    found, from_rows = await _from_descriptions(session, ids, outcome)
    seen = from_payloads | from_rows

    requirements: dict[UUID, dict[str, _Requirement]] = {}
    for vacancy_id in seen:
        merged = _merge(stated.get(vacancy_id, ()), found.get(vacancy_id, ()))
        if merged:
            requirements[vacancy_id] = merged

    outcome.considered += len(seen)
    outcome.without_skills += len(seen) - len(requirements)
    outcome.without_field_skills += len(seen) - len(stated)
    outcome.rescued_by_text += sum(1 for vacancy_id in requirements if vacancy_id not in stated)

    await session.execute(delete(VacancySkill).where(VacancySkill.vacancy_id.in_(ids)))
    payload = [
        {
            "id": uuid7(),
            "vacancy_id": vacancy_id,
            "canonical_name": item.canonical_name,
            "is_required": item.is_required,
            "weight": item.weight,
            "source": item.source,
        }
        for vacancy_id, items in requirements.items()
        for item in items.values()
    ]
    if payload:
        await session.execute(pg_insert(VacancySkill).values(payload))
        outcome.skills_written += len(payload)
        outcome.from_field += sum(
            1 for row in payload if row["source"] is RequirementSource.EMPLOYER_FIELD
        )
        outcome.from_text += sum(
            1 for row in payload if row["source"] is RequirementSource.DESCRIPTION_TEXT
        )

    for value, group in _grouped(years).items():
        await session.execute(
            sa_update(Vacancy).where(Vacancy.id.in_(group)).values(min_years=value)
        )
        outcome.years_written += len(group)


async def _from_payloads(
    session: AsyncSession, ids: Sequence[UUID]
) -> tuple[dict[UUID, list[str]], dict[UUID, Any], set[UUID]]:
    """What the employers stated: their skill lists and their experience bands.

    A vacancy can carry more than one source row — the same job cross-posted —
    so the requirements of each are pooled rather than one arbitrarily winning.
    """
    rows = await session.execute(
        select(VacancySource.vacancy_id, VacancySource.raw).where(VacancySource.vacancy_id.in_(ids))
    )

    stated: dict[UUID, list[str]] = {}
    years: dict[UUID, Any] = {}
    seen: set[UUID] = set()
    for vacancy_id, raw in rows.all():
        seen.add(vacancy_id)
        derived = (raw or {}).get("_derived") if isinstance(raw, dict) else None
        if not isinstance(derived, dict):
            continue
        for name in skill_names(derived):
            stated.setdefault(vacancy_id, [])
            if name not in stated[vacancy_id]:
                stated[vacancy_id].append(name)
        band = min_years(derived)
        if band is not None and vacancy_id not in years:
            years[vacancy_id] = band
    return stated, years, seen


async def _from_descriptions(
    session: AsyncSession, ids: Sequence[UUID], outcome: SyncOutcome
) -> tuple[dict[UUID, list[tuple[str, bool]]], set[UUID]]:
    """What the descriptions say, as ``(canonical name, is_required)`` pairs.

    The title is read together with the description and under the same weight.
    ``docs/MATCHING.md`` prices a technology in the title at 1.00 — as a stated
    requirement — and that tier is deliberately not implemented here: a title is
    the employer's own words about the role, but «Python-разработчик» is still
    our reading of a sentence rather than a list they filled in, and the brief's
    rule for this whole change is to err towards "not required".
    """
    rows = await session.execute(
        select(Vacancy.id, Vacancy.title, Vacancy.description_raw).where(Vacancy.id.in_(ids))
    )
    found: dict[UUID, list[tuple[str, bool]]] = {}
    read: set[UUID] = set()
    for vacancy_id, title, description in rows.all():
        read.add(vacancy_id)
        text = "\n".join(part for part in (title, description) if part)
        skills = skills_in_text(text)
        outcome.optional_from_text += len(skills.optional)
        outcome.negated_in_text += len(skills.negated)
        pairs = [(name, True) for name in skills.required]
        pairs += [(name, False) for name in skills.optional]
        if pairs:
            found[vacancy_id] = pairs
    return found, read


def _merge(stated: Sequence[str], found: Sequence[tuple[str, bool]]) -> dict[str, _Requirement]:
    """One vacancy's requirements, the employer's list first.

    ``vacancy_skill`` is unique on ``(vacancy_id, canonical_name)``, so a skill
    named in both places is one row and the question is which one. The stated
    one: it is the stronger evidence, it carries the higher weight, and a card
    saying «названо работодателем» about it is true.
    """
    merged: dict[str, _Requirement] = {
        name: _Requirement(
            canonical_name=name,
            # hh's ``keySkills`` carries no required/nice split, so every entry
            # is a requirement at full weight — which is what docs/MATCHING.md
            # prescribes for a source that hands the list over structured.
            is_required=True,
            weight=FIELD_WEIGHT,
            source=RequirementSource.EMPLOYER_FIELD,
        )
        for name in stated
    }
    for name, is_required in found:
        merged.setdefault(
            name,
            _Requirement(
                canonical_name=name,
                is_required=is_required,
                weight=TEXT_WEIGHT,
                source=RequirementSource.DESCRIPTION_TEXT,
            ),
        )
    return merged


def _grouped(years: dict[UUID, Any]) -> dict[Any, list[UUID]]:
    """Ids by the value they get, so one statement serves each of the four bands.

    hh states experience as one of four, so a chunk of two hundred vacancies
    needs at most four updates rather than two hundred.
    """
    grouped: dict[Any, list[UUID]] = {}
    for vacancy_id, value in years.items():
        grouped.setdefault(value, []).append(vacancy_id)
    return grouped
