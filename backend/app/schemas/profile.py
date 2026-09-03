"""Candidate profile contracts."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.db.enums import RemoteType, Seniority, SkillLevel
from app.schemas.common import CurrencyCode, ReadModel

Years = Annotated[Decimal, Field(ge=0, le=60, decimal_places=1)]


class SkillCreate(BaseModel):
    """One skill as extracted from a resume."""

    canonical_name: str = Field(min_length=1, max_length=100)
    raw_name: str | None = Field(default=None, max_length=200)
    years: Years | None = None
    level: SkillLevel = SkillLevel.WORKING
    last_used_year: int | None = Field(default=None, ge=1970, le=2100)


class SkillRead(ReadModel):
    """A skill of the candidate."""

    id: UUID
    canonical_name: str
    raw_name: str | None
    years: Decimal | None
    level: SkillLevel
    last_used_year: int | None


class CandidateProfileCreate(BaseModel):
    """Everything the resume extractor produces."""

    name: str | None = Field(default=None, max_length=200)
    headline: str | None = Field(default=None, max_length=300)
    seniority: Seniority | None = None
    total_years: Years | None = None
    summary: str | None = None
    locations: list[str] = Field(default_factory=list)
    relocation: bool = False
    remote_pref: RemoteType | None = None
    salary_min: Decimal | None = Field(default=None, ge=0)
    salary_currency: CurrencyCode | None = None
    languages: list[dict[str, Any]] = Field(default_factory=list)
    raw_text: str | None = None
    skills: list[SkillCreate] = Field(default_factory=list)


class CandidateProfileUpdate(BaseModel):
    """Manual corrections from the UI. Every field optional; unset means unchanged."""

    name: str | None = Field(default=None, max_length=200)
    headline: str | None = Field(default=None, max_length=300)
    seniority: Seniority | None = None
    total_years: Years | None = None
    summary: str | None = None
    locations: list[str] | None = None
    relocation: bool | None = None
    remote_pref: RemoteType | None = None
    salary_min: Decimal | None = Field(default=None, ge=0)
    salary_currency: CurrencyCode | None = None
    languages: list[dict[str, Any]] | None = None
    is_active: bool | None = None


class CandidateProfileRead(ReadModel):
    """Full profile, skills included."""

    id: UUID
    name: str | None
    headline: str | None
    seniority: Seniority | None
    total_years: Decimal | None
    summary: str | None
    locations: list[str]
    relocation: bool
    remote_pref: RemoteType | None
    salary_min: Decimal | None
    salary_currency: str | None
    languages: list[dict[str, Any]]
    is_active: bool
    created_at: datetime
    updated_at: datetime
    skills: list[SkillRead] = Field(default_factory=list)
    #: The raw resume text and the embedding are intentionally absent: one is
    #: large and one is meaningless to a client.
