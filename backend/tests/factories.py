"""Deterministic builders for test data.

Deliberately not random: a pagination bug that only shows up on one seed is
worse than no test at all. Anything a test cares about is passed explicitly;
everything else gets a stable, boring default.
"""

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.db.enums import (
    EmploymentType,
    MatchBucket,
    RemoteType,
    SalaryPeriod,
    Seniority,
    SkillLevel,
)
from app.schemas.match import MatchCreate, MatchedSkill, MissingSkill
from app.schemas.profile import CandidateProfileCreate, SkillCreate
from app.schemas.vacancy import VacancyCreate

#: Fixed instant so published_at ordering is reproducible across runs.
EPOCH = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def fingerprint_for(seed: str) -> str:
    """Stable 40-character fingerprint, the same shape the deduplicator emits."""
    return hashlib.sha1(seed.encode()).hexdigest()


def make_vacancy(
    seed: str = "vacancy-1",
    *,
    title: str | None = None,
    company: str = "Acme",
    city: str | None = "Алматы",
    country: str | None = "KZ",
    remote: RemoteType = RemoteType.NO,
    seniority: Seniority | None = Seniority.MIDDLE,
    min_years: Decimal | None = Decimal("3.0"),
    salary_min: Decimal | None = Decimal("500000.00"),
    salary_max: Decimal | None = Decimal("800000.00"),
    currency: str | None = "KZT",
    period: SalaryPeriod | None = SalaryPeriod.MONTH,
    employment_type: EmploymentType | None = EmploymentType.FULL_TIME,
    language: str | None = "ru",
    published_at: datetime | None = EPOCH,
    description_raw: str = "Backend engineer. Python, FastAPI, PostgreSQL.",
    **overrides: Any,
) -> VacancyCreate:
    """A normalised posting ready for upsert."""
    return VacancyCreate(
        fingerprint=fingerprint_for(seed),
        title=title or f"Backend Engineer {seed}",
        company=company,
        description_raw=description_raw,
        city=city,
        country=country,
        remote=remote,
        seniority=seniority,
        min_years=min_years,
        salary_min=salary_min,
        salary_max=salary_max,
        currency=currency,
        period=period,
        employment_type=employment_type,
        language=language,
        published_at=published_at,
        **overrides,
    )


def make_source(seed: str = "vacancy-1", slug: str = "hh") -> tuple[str, str, str, dict[str, Any]]:
    """The (source_slug, external_id, url, raw) tuple bulk_upsert expects."""
    return slug, f"{slug}-{seed}", f"https://example.test/{slug}/{seed}", {"seed": seed}


def make_upsert_item(
    seed: str = "vacancy-1", slug: str = "hh", **vacancy_kwargs: Any
) -> tuple[VacancyCreate, str, str, str, dict[str, Any]]:
    """One element of a bulk_upsert batch."""
    return (make_vacancy(seed, **vacancy_kwargs), *make_source(seed, slug))


def make_profile(
    *,
    name: str = "Nurzhan",
    seniority: Seniority | None = Seniority.MIDDLE,
    total_years: Decimal | None = Decimal("4.0"),
    salary_min: Decimal | None = Decimal("4000.00"),
    salary_currency: str | None = "USD",
    skills: Sequence[str] = ("python", "fastapi", "postgresql"),
    **overrides: Any,
) -> CandidateProfileCreate:
    """A structured profile with a handful of skills."""
    return CandidateProfileCreate(
        name=name,
        headline="Backend Engineer",
        seniority=seniority,
        total_years=total_years,
        locations=["Алматы"],
        relocation=True,
        salary_min=salary_min,
        salary_currency=salary_currency,
        skills=[
            SkillCreate(
                canonical_name=skill,
                raw_name=skill.title(),
                years=Decimal("3.0"),
                level=SkillLevel.STRONG,
                last_used_year=2026,
            )
            for skill in skills
        ],
        **overrides,
    )


def bucket_for(score: Decimal) -> MatchBucket:
    """Bucket boundaries from docs/MATCHING.md."""
    if score >= 85:
        return MatchBucket.APPLY_NOW
    if score >= 70:
        return MatchBucket.STRONG
    if score >= 55:
        return MatchBucket.STRETCH
    return MatchBucket.SKIP


def make_match(
    profile_id: UUID,
    vacancy_id: UUID,
    score: Decimal | float | int = Decimal("80.00"),
    *,
    bucket: MatchBucket | None = None,
    missing_required: Sequence[str] = (),
    matched: Sequence[str] = ("python",),
    **overrides: Any,
) -> MatchCreate:
    """A scoring result. Bucket follows the score unless overridden."""
    value = Decimal(str(score)).quantize(Decimal("0.01"))
    return MatchCreate(
        profile_id=profile_id,
        vacancy_id=vacancy_id,
        score=value,
        rule_score=value,
        bucket=bucket or bucket_for(value),
        matched_skills=[
            MatchedSkill(canonical_name=name, coverage=Decimal("1.0")) for name in matched
        ],
        missing_required=[
            MissingSkill(canonical_name=name, weight=Decimal("1.0")) for name in missing_required
        ],
        **overrides,
    )


def published_at(days_ago: int) -> datetime:
    """A publication timestamp relative to the fixed EPOCH."""
    return EPOCH - timedelta(days=days_ago)
