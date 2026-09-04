"""Vacancy contracts, including the single filter object the list endpoint takes."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.enums import (
    EmploymentType,
    MatchBucket,
    RemoteType,
    SalaryPeriod,
    Seniority,
    VacancyCompleteness,
)
from app.normalize.fingerprint import VERSION as FINGERPRINT_VERSION
from app.schemas.common import (
    CountryCode,
    CurrencyCode,
    LanguageCode,
    ReadModel,
    SortDirection,
    SortField,
)

Score = Annotated[Decimal, Field(ge=0, le=100, decimal_places=2)]
Years = Annotated[Decimal, Field(ge=0, le=60, decimal_places=1)]

#: Nothing older than this is worth looking at, and it caps the filter input.
MAX_POSTED_WITHIN_DAYS = 365


class VacancySourceRead(ReadModel):
    """One place this vacancy was found."""

    id: UUID
    source_slug: str
    external_id: str
    url: str


class VacancySkillRead(ReadModel):
    """A skill the vacancy asks for."""

    canonical_name: str
    is_required: bool
    weight: Decimal


class SalaryRead(BaseModel):
    """Salary exactly as advertised, plus the comparable monthly USD amount."""

    min: Decimal | None = None
    max: Decimal | None = None
    currency: str | None = None
    is_gross: bool | None = None
    period: SalaryPeriod | None = None
    #: Monthly USD equivalent. None when the currency has no known rate — such
    #: postings sort last instead of sorting wrong.
    min_normalized: Decimal | None = None
    max_normalized: Decimal | None = None
    normalized_at: datetime | None = None


class VacancyCreate(BaseModel):
    """Normalised posting, ready to be upserted."""

    fingerprint: str = Field(min_length=1, max_length=40)
    #: Which algorithm produced the fingerprint. Always set by
    #: app.normalize.fingerprint, never by a connector, so a row can only carry
    #: the version that actually computed its key.
    fingerprint_version: int = Field(default=FINGERPRINT_VERSION, ge=1)
    title: str = Field(min_length=1, max_length=300)
    company: str | None = Field(default=None, max_length=200)
    company_url: str | None = Field(default=None, max_length=500)
    description_raw: str | None = None
    description_md: str | None = None
    seniority: Seniority | None = None
    min_years: Years | None = None
    city: str | None = Field(default=None, max_length=120)
    country: CountryCode | None = None
    remote: RemoteType = RemoteType.NO
    salary_min: Decimal | None = Field(default=None, ge=0)
    salary_max: Decimal | None = Field(default=None, ge=0)
    currency: CurrencyCode | None = None
    is_gross: bool | None = None
    period: SalaryPeriod | None = None
    employment_type: EmploymentType | None = None
    language: LanguageCode | None = None
    published_at: datetime | None = None
    expires_at: datetime | None = None
    #: How much of the posting this record holds. A source that returns only a
    #: title and a link must say so rather than presenting a stub as a full
    #: posting that merely scored badly.
    completeness: VacancyCompleteness = VacancyCompleteness.FULL

    @model_validator(mode="after")
    def _salary_range_is_ordered(self) -> Self:
        """A range where the floor is above the ceiling is a parsing bug."""
        both_set = self.salary_min is not None and self.salary_max is not None
        if both_set and self.salary_min > self.salary_max:  # type: ignore[operator]
            raise ValueError("salary_min must not exceed salary_max")
        return self


class VacancyListItem(BaseModel):
    """Row of the dashboard table: exactly what the table renders, nothing more.

    Built from a repository row rather than an ORM object, because the score,
    the source slugs and the applied flag come from joins.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    company: str | None
    source_slugs: list[str] = Field(default_factory=list)
    city: str | None
    country: str | None
    remote: RemoteType
    salary_min: Decimal | None
    salary_max: Decimal | None
    currency: str | None
    salary_min_normalized: Decimal | None
    score: Decimal | None
    bucket: MatchBucket | None
    missing_required_count: int = 0
    published_at: datetime | None
    is_applied: bool = False

    @field_validator("source_slugs", mode="before")
    @classmethod
    def _empty_aggregate_is_empty_list(cls, value: Any) -> Any:
        """array_agg over no rows returns NULL, not an empty array."""
        return [] if value is None else value


class VacancyRead(ReadModel):
    """Full vacancy card."""

    id: UUID
    fingerprint: str
    title: str
    company: str | None
    company_url: str | None
    description_md: str | None
    description_raw: str | None
    seniority: Seniority | None
    min_years: Decimal | None
    city: str | None
    country: str | None
    remote: RemoteType
    employment_type: EmploymentType | None
    language: str | None
    published_at: datetime | None
    expires_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    is_active: bool
    # Salary stays flat, mirroring the ORM. Grouping it into SalaryRead would
    # need a before-validator that reaches into the ORM object, and the card is
    # the only consumer.
    salary_min: Decimal | None
    salary_max: Decimal | None
    currency: str | None
    is_gross: bool | None
    period: SalaryPeriod | None
    salary_min_normalized: Decimal | None
    salary_max_normalized: Decimal | None
    salary_normalized_at: datetime | None

    sources: list[VacancySourceRead] = Field(default_factory=list)
    skills: list[VacancySkillRead] = Field(default_factory=list)


class VacancyFilter(BaseModel):
    """Every filter the vacancy list accepts, as one validated object.

    Kept as a single model rather than a pile of query parameters so the
    validation lives in one place and the repository takes one argument.
    """

    score_min: Score | None = None
    score_max: Score | None = None
    bucket: list[MatchBucket] | None = None
    source: list[str] | None = None
    remote: list[RemoteType] | None = None
    city: str | None = Field(default=None, max_length=120)
    country: CountryCode | None = None
    #: Always in USD: compared against the normalised monthly amount, never
    #: against the advertised figure, which is not comparable across currencies.
    salary_min: Decimal | None = Field(default=None, ge=0)
    #: Filters by the currency a posting advertises. Independent of salary_min.
    currency: CurrencyCode | None = None
    seniority: list[Seniority] | None = None
    posted_within_days: int | None = Field(default=None, ge=1, le=MAX_POSTED_WITHIN_DAYS)
    has_salary: bool | None = None
    missing_skills_max: int | None = Field(default=None, ge=0, le=50)
    company: str | None = Field(default=None, max_length=200)
    #: Full-text query against title, company and description.
    q: str | None = Field(default=None, max_length=200)
    exclude_applied: bool = False
    #: Hidden by default: hard-failed vacancies are noise in the dashboard.
    include_filtered: bool = False
    sort: SortField = SortField.SCORE
    direction: SortDirection = SortDirection.DESC

    @model_validator(mode="after")
    def _score_range_is_ordered(self) -> Self:
        """An inverted score range silently returns nothing; fail loudly instead."""
        both_set = self.score_min is not None and self.score_max is not None
        if both_set and self.score_min > self.score_max:  # type: ignore[operator]
            raise ValueError("score_min must not exceed score_max")
        return self

    def as_cache_key(self) -> tuple[tuple[str, Any], ...]:
        """Stable, hashable representation for caching facet counts."""
        return tuple(sorted(self.model_dump(exclude_none=True).items(), key=lambda kv: kv[0]))
