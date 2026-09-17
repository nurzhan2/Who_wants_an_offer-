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
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
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
    #: Who first published the posting, when this source republished it —
    #: "LinkedIn" behind a jsearch row. None for a source that is the original.
    publisher: str | None = None
    #: The row the card links to first: the one holding the most data.
    is_primary: bool = False
    #: Invented by ``scripts/seed.py``; its address leads nowhere.
    is_seed: bool = False


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
    #: Fullest source first; the first one is the one ``source_url`` points at.
    source_slugs: list[str] = Field(default_factory=list)
    #: The original posting, on the source that holds the most data about it.
    #: Taken from ``vacancy_source.url`` and never rebuilt from an id: sources
    #: differ in domain, and hh in regional subdomain too.
    source_url: str | None = None
    #: Every source row was invented by ``scripts/seed.py``.
    is_seed: bool = False
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
    #: The crawler's last sighting and its verdict that the posting is gone, so
    #: the list can show an archived or long-unseen vacancy before anybody
    #: opens it — a third of the first real queue was archived (2026-09-16).
    last_seen_at: datetime | None = None
    is_active: bool = True
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
    #: Whether postings that advertise no salary survive :attr:`salary_min`.
    #:
    #: True by default, and the default is the measured one. Five vacancies in
    #: six on this corpus carry no salary at all — hh does not require one — so
    #: ``salary_min >= 2000`` on its own answers with the sixth, silently, and a
    #: person reading that list concludes the market is empty rather than that
    #: the field is. A filter whose default hides most of the data is a filter
    #: that lies about it.
    #:
    #: Set it to false to ask the other question — "only postings that state a
    #: figure, and at least this one" — which is a real question and is simply
    #: not the one somebody typing a floor is usually asking. It does nothing
    #: unless :attr:`salary_min` is set; the way to see priced or unpriced rows
    #: on their own is :attr:`has_salary`.
    include_unpriced: bool = True
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


class VacancyQuery(VacancyFilter):
    """Everything the list endpoint reads off the query string.

    The filter plus the paging, in one model, and the reason is a rule of
    FastAPI rather than a preference: a Pydantic model is expanded into
    individual query parameters **only when it is the sole query field of the
    handler** (``request_params_to_args``). Declared beside a ``limit`` or a
    ``cursor``, the same model silently stops expanding and becomes one opaque
    parameter named ``filters`` — the endpoint then answers 422 for every
    request, or, with a default, ignores every filter it is given. Both were
    observed here before the models were merged.

    :meth:`filters` hands the repository the half it takes, so the layer below
    keeps taking one validated filter object and knows nothing about paging.
    """

    #: Keyset position from the previous page's ``next_cursor``. Never an
    #: offset — see ``app/db/repositories/cursor.py``.
    cursor: str | None = None
    limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    #: Both cost a second query, so both are asked for rather than assumed. The
    #: table needs neither; the filter sidebar needs both.
    with_total: bool = False
    with_facets: bool = False

    def filters(self) -> VacancyFilter:
        """Just the filtering half, as the repository's own contract.

        Rebuilt from the filter's own field list rather than validated from
        ``self``: this is a *subclass* of VacancyFilter, and Pydantic hands a
        subclass instance straight back, paging and all. That passes every type
        check and every test that only reads filter fields, and quietly poisons
        :meth:`VacancyFilter.as_cache_key`, where a cursor would then make every
        page of one filter a different cache entry.
        """
        kept = set(VacancyFilter.model_fields)
        return VacancyFilter.model_validate(self.model_dump(include=kept))
