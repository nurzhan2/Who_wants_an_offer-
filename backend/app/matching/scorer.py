"""Scoring the corpus against the active profile, and storing why.

The formula lives in ``rules.py`` and is pure; this is the part that reads rows,
computes similarity in the database and writes ``match``. Kept apart so the
formula can be tested without a session and this can be tested without
re-testing the formula.

**Similarity is computed by PostgreSQL, not here.** pgvector's ``<=>`` over the
HNSW index is one indexed expression per row; pulling 643 vectors of 1024
floats into Python to do it would be about five megabytes of round trip to
compute something the database is holding an index for.

**Seed rows are scored but never rewritten.** ``scripts/seed.py`` writes 60
``match`` rows against vacancies whose ``external_id`` contains ``-dev-``, and
the queue already refuses to serve those. A real scoring pass must not
overwrite them with real numbers, because that would quietly turn demo data
into data that looks measured. They are skipped, and the report says how many.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import MatchBucket, RemoteType, RequirementSource, Seniority
from app.db.models import CandidateProfile, ProfileSkill, Vacancy, VacancySkill, VacancySource
from app.db.repositories.match import MatchRepository
from app.matching.rules import (
    DEFAULT_FORMULA,
    DEFAULT_UNSTATED,
    Formula,
    ProfileFacts,
    Score,
    SkillMatch,
    UnstatedRequirement,
    VacancyFacts,
    normalise_similarity,
    score_vacancy,
)
from app.normalize.requirements import language_requirements
from app.resume.skills import default_canonicalizer
from app.schemas.match import MatchComponentScores, MatchCreate, MatchedSkill, MissingSkill

logger = structlog.get_logger(__name__)

#: Vacancies scored per round trip. One statement fetches the row, its skills
#: and its similarity together, so this bounds memory rather than statements.
CHUNK = 200

#: Matches written per statement. ``bulk_upsert`` is a single multi-values
#: INSERT, and PostgreSQL's parameter limit is 65535: each match carries
#: thirteen refreshable columns plus keys, so five hundred is comfortable.
WRITE_BATCH = 500

#: The middle of how ``scripts/seed.py`` names what it invents. The same
#: constant as ``app.services.agent_queue.SEED_ID_MARKER``, and deliberately
#: not imported from there: this module is not allowed to depend on the agent
#: queue, and one string in two places is cheaper than that dependency.
SEED_ID_MARKER = "-dev-"


@dataclass(slots=True)
class ScoringOutcome:
    """What one scoring pass did, for a report a person reads."""

    considered: int = 0
    written: int = 0
    skipped_seeds: int = 0
    filtered: int = 0
    without_embedding: int = 0
    without_title_embedding: int = 0
    without_skills: int = 0
    buckets: dict[str, int] = field(default_factory=dict)
    #: Which rule weighed the unwritten requirement. Carried with the counts
    #: because two passes are only comparable when this is known.
    unstated: UnstatedRequirement = DEFAULT_UNSTATED
    #: Which number was computed, for the same reason.
    formula: Formula = DEFAULT_FORMULA

    def as_dict(self) -> dict[str, Any]:
        """The counters, for logging."""
        return {
            "formula": str(self.formula),
            "unstated": str(self.unstated),
            "considered": self.considered,
            "written": self.written,
            "skipped_seeds": self.skipped_seeds,
            "filtered": self.filtered,
            "without_embedding": self.without_embedding,
            "without_title_embedding": self.without_title_embedding,
            "without_skills": self.without_skills,
        }


class ProfileNotReadyError(Exception):
    """The profile cannot be scored against yet, and the reason a person needs.

    Raised rather than returning an empty result, because "nothing matched" and
    "nothing could be compared" look identical in a report and mean opposite
    things. ``wwao match`` already sets the example: when a link is missing, say
    which one.
    """


async def score_corpus(
    session: AsyncSession,
    *,
    profile_id: UUID | None = None,
    limit: int | None = None,
    unstated: UnstatedRequirement = DEFAULT_UNSTATED,
    formula: Formula = DEFAULT_FORMULA,
) -> ScoringOutcome:
    """Score every vacancy against the active profile and store the reasons."""
    profile = await _profile(session, profile_id)
    facts = await _profile_facts(session, profile)
    outcome = ScoringOutcome(unstated=unstated, formula=formula)

    ids = await _vacancy_ids(session, limit=limit)
    canonicalizer = default_canonicalizer()
    pending: list[MatchCreate] = []
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start : start + CHUNK]
        for row in await _facts_for(session, chunk, profile.id):
            outcome.considered += 1
            if row.is_seed:
                outcome.skipped_seeds += 1
                continue
            if row.facts.similarity is None:
                outcome.without_embedding += 1
            if row.facts.title_similarity is None:
                outcome.without_title_embedding += 1
            if not row.facts.required_skills:
                outcome.without_skills += 1
            score = score_vacancy(
                row.facts, facts, canonicalizer=canonicalizer, unstated=unstated, formula=formula
            )
            outcome.buckets[score.bucket] = outcome.buckets.get(score.bucket, 0) + 1
            if score.bucket == MatchBucket.FILTERED:
                outcome.filtered += 1
            pending.append(_to_match(profile.id, row.vacancy_id, score))
        if len(pending) >= WRITE_BATCH:
            outcome.written += await MatchRepository(session).bulk_upsert(pending)
            pending = []

    if pending:
        outcome.written += await MatchRepository(session).bulk_upsert(pending)
    logger.info("matching.scored", **outcome.as_dict())
    return outcome


async def _profile(session: AsyncSession, profile_id: UUID | None) -> CandidateProfile:
    """The profile to score against, with the reason if there is not one."""
    stmt = select(CandidateProfile)
    stmt = (
        stmt.where(CandidateProfile.id == profile_id)
        if profile_id is not None
        else stmt.where(CandidateProfile.is_active.is_(True))
    )
    profile = (await session.execute(stmt.limit(1))).scalar_one_or_none()
    if profile is None:
        raise ProfileNotReadyError(
            "нет активного профиля: загрузите резюме через POST /api/v1/profile/resume "
            "или выполните python scripts/seed.py"
        )
    return profile


async def _profile_facts(session: AsyncSession, profile: CandidateProfile) -> ProfileFacts:
    """Everything the formula needs about the candidate, in one read."""
    rows = await session.execute(
        select(ProfileSkill.canonical_name, ProfileSkill.level).where(
            ProfileSkill.profile_id == profile.id
        )
    )
    skills = {name: str(level) for name, level in rows.all()}
    return ProfileFacts(
        total_years=profile.total_years,
        seniority=Seniority(profile.seniority) if profile.seniority else None,
        skills=skills,
        languages=_languages(profile.languages),
        locations=[str(place) for place in (profile.locations or []) if isinstance(place, str)],
        relocation=bool(profile.relocation),
        remote_pref=RemoteType(profile.remote_pref) if profile.remote_pref else None,
        salary_min=profile.salary_min,
        has_embedding=profile.embedding is not None,
    )


def _languages(stored: Any) -> dict[str, str]:
    """``[{"code": "en", "level": "B2"}]`` as a lookup, defensively.

    JSONB written by the resume extractor, so the shape is a promise rather than
    a guarantee, and a malformed entry must cost that entry rather than the gate.
    """
    spoken: dict[str, str] = {}
    for entry in stored if isinstance(stored, list) else []:
        if not isinstance(entry, dict):
            continue
        code, level = entry.get("code"), entry.get("level")
        if isinstance(code, str) and isinstance(level, str):
            spoken[code.strip().casefold()] = level.strip()
    return spoken


async def _vacancy_ids(session: AsyncSession, *, limit: int | None) -> list[UUID]:
    """Every active vacancy, newest first so a truncated run scores the freshest."""
    stmt: Select[Any] = (
        select(Vacancy.id)
        .where(Vacancy.is_active.is_(True), Vacancy.is_spam.is_(False))
        .order_by(Vacancy.first_seen_at.desc(), Vacancy.id)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return [row[0] for row in (await session.execute(stmt)).all()]


@dataclass(frozen=True, slots=True)
class _Row:
    """One vacancy's facts, plus whether it is a seed."""

    vacancy_id: UUID
    facts: VacancyFacts
    is_seed: bool


async def _facts_for(session: AsyncSession, ids: Sequence[UUID], profile_id: UUID) -> list[_Row]:
    """Read one chunk: the columns, the skills, the payload and the distance."""
    if not ids:
        return []

    vector = select(CandidateProfile.embedding).where(CandidateProfile.id == profile_id)
    distance = Vacancy.embedding.cosine_distance(vector.scalar_subquery())
    headline = select(CandidateProfile.headline_embedding).where(CandidateProfile.id == profile_id)
    title_distance = Vacancy.title_embedding.cosine_distance(headline.scalar_subquery())
    rows = (
        await session.execute(
            select(
                Vacancy.id,
                Vacancy.min_years,
                Vacancy.seniority,
                Vacancy.city,
                Vacancy.remote,
                Vacancy.salary_min_normalized,
                Vacancy.employment_type,
                distance.label("distance"),
                title_distance.label("title_distance"),
            ).where(Vacancy.id.in_(ids))
        )
    ).all()

    skills, sources = await _skills_for(session, ids)
    derived = await _derived_for(session, ids)
    seeds = await _seed_ids(session, ids)

    built: list[_Row] = []
    for row in rows:
        payload = derived.get(row.id, {})
        built.append(
            _Row(
                vacancy_id=row.id,
                facts=VacancyFacts(
                    required_skills=skills.get(row.id, {}),
                    requirement_sources=sources.get(row.id, {}),
                    min_years=row.min_years,
                    seniority=Seniority(row.seniority) if row.seniority else None,
                    city=row.city,
                    remote=RemoteType(row.remote),
                    salary_min=row.salary_min_normalized,
                    employment_type=str(row.employment_type) if row.employment_type else None,
                    language_requirements=language_requirements(payload),
                    closed_for_applicants=bool(payload.get("closed_for_applicants", False)),
                    # ``<=>`` is cosine DISTANCE in [0;2]; the document's
                    # similarity is the cosine itself mapped onto [0;1].
                    similarity=(
                        normalise_similarity(1 - float(row.distance))
                        if row.distance is not None
                        else None
                    ),
                    title_similarity=(
                        normalise_similarity(1 - float(row.title_distance))
                        if row.title_distance is not None
                        else None
                    ),
                ),
                is_seed=row.id in seeds,
            )
        )
    return built


async def _skills_for(
    session: AsyncSession, ids: Sequence[UUID]
) -> tuple[dict[UUID, dict[str, Decimal]], dict[UUID, dict[str, RequirementSource]]]:
    """Required skills by vacancy: their weights, and who says they are required.

    Two mappings rather than one of pairs because the formula wants the weights
    and only the explanation wants the provenance, and ``VacancyFacts`` keeps
    them in two fields for the same reason: adding a source must not change
    what any score is.
    """
    rows = await session.execute(
        select(
            VacancySkill.vacancy_id,
            VacancySkill.canonical_name,
            VacancySkill.weight,
            VacancySkill.source,
        ).where(VacancySkill.vacancy_id.in_(ids), VacancySkill.is_required.is_(True))
    )
    found: dict[UUID, dict[str, Decimal]] = {}
    sources: dict[UUID, dict[str, RequirementSource]] = {}
    for vacancy_id, name, weight, source in rows.all():
        found.setdefault(vacancy_id, {})[name] = weight
        sources.setdefault(vacancy_id, {})[name] = source
    return found, sources


async def _derived_for(session: AsyncSession, ids: Sequence[UUID]) -> dict[UUID, dict[str, Any]]:
    """The connector's derived block per vacancy, pooled across source rows."""
    rows = await session.execute(
        select(VacancySource.vacancy_id, VacancySource.raw).where(VacancySource.vacancy_id.in_(ids))
    )
    found: dict[UUID, dict[str, Any]] = {}
    for vacancy_id, raw in rows.all():
        block = (raw or {}).get("_derived") if isinstance(raw, dict) else None
        if isinstance(block, dict):
            found.setdefault(vacancy_id, {}).update(block)
    return found


async def _seed_ids(session: AsyncSession, ids: Sequence[UUID]) -> set[UUID]:
    """Vacancies invented by scripts/seed.py, which a real pass must not rewrite."""
    rows = await session.execute(
        select(VacancySource.vacancy_id)
        .where(VacancySource.vacancy_id.in_(ids))
        .where(VacancySource.external_id.contains(SEED_ID_MARKER))
    )
    return {row[0] for row in rows.all()}


def _to_match(profile_id: UUID, vacancy_id: UUID, score: Score) -> MatchCreate:
    """The stored form: the number, and every reason behind it."""
    return MatchCreate(
        profile_id=profile_id,
        vacancy_id=vacancy_id,
        score=score.final_score,
        rule_score=score.rule_score,
        semantic_score=(
            (score.similarity * 100).quantize(Decimal("0.01"))
            if score.similarity is not None
            else None
        ),
        bucket=score.bucket,
        component_scores=MatchComponentScores(**score.components),
        matched_skills=[
            MatchedSkill(
                canonical_name=item.canonical_name, coverage=item.coverage, source=item.source
            )
            for item in score.matched
        ],
        missing_required=[
            MissingSkill(canonical_name=item.canonical_name, weight=item.weight, source=item.source)
            for item in score.missing
        ],
        red_flags=_flags(score),
        experience_gap_years=score.experience_gap_years,
        verdict=_verdict(score),
    )


def _flags(score: Score) -> list[str]:
    """Red flags, plus a note for anything the score could not measure.

    The note is the honest half of renormalisation. A vacancy scored without
    semantic similarity is not the same as one scored with it, and the card the
    owner reads before sending an application should say which it is looking at
    rather than presenting both as the same number.
    """
    flags = list(score.red_flags)
    if score.similarity is None:
        flags.append("семантика не посчитана: у вакансии нет эмбеддинга")
    if score.formula is Formula.TITLE and score.title_similarity is None:
        flags.append("название не сравнено: нет вектора названия вакансии или заголовка профиля")
    requirements = [*score.matched, *score.missing]
    if not requirements:
        flags.append("работодатель не указал ключевые навыки")
    else:
        flags.extend(_provenance_flags(requirements))
    return flags


def _provenance_flags(requirements: Sequence[SkillMatch]) -> list[str]:
    """The third state, said out loud on the card.

    Two states existed before: the employer listed requirements, or they listed
    none. A third one now reaches this card — requirements nobody stated, read
    out of the description — and it must not look like either. It is not the
    silence it replaced, and it is not a stated list: the same sentence can be
    read two ways, and the person about to spend an evening on an application is
    the one who should get to judge which reading this was.
    """
    from_text = sum(1 for item in requirements if item.source is RequirementSource.DESCRIPTION_TEXT)
    if not from_text:
        return []
    if from_text == len(requirements):
        return ["навыки не названы работодателем: требования выведены из текста описания"]
    return [f"часть требований выведена из текста описания: {from_text} из {len(requirements)}"]


def _verdict(score: Score) -> str | None:
    """One Russian sentence saying what the number rests on.

    Not an LLM verdict — that is stage 2 and is written over this one when it
    runs. This is the rule-based explanation, and it exists because a bare
    number is useless in all three places that read it: the letter generator,
    the dashboard, and the confirmation card a person reads before an
    application goes out.
    """
    if score.filtered_reason is not None:
        return f"отфильтровано: {score.filtered_reason}"
    counted = ", ".join(COMPONENT_NAMES.get(name, name) for name in score.counted)
    if not counted:
        return "нечего сравнивать: ни навыков, ни опыта, ни эмбеддинга"
    return f"считано по: {counted}"


#: Russian names for the components, for the one-line verdict.
COMPONENT_NAMES: dict[str, str] = {
    "title_similarity": "названию",
    "skill_coverage_required": "навыкам",
    "skill_coverage_nice": "желательным навыкам",
    "semantic_similarity": "семантике",
    "experience_fit": "опыту",
    "domain_fit": "домену",
    "logistics_fit": "логистике",
}
