"""Candidate profile contracts."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Self
from uuid import UUID

from pydantic import AfterValidator, BaseModel, Field, model_validator

from app.db.enums import ParseStatus, RemoteType, Seniority, SkillEvidence, SkillLevel
from app.schemas.common import CurrencyCode, ReadModel

Years = Annotated[Decimal, Field(ge=0, le=60, decimal_places=1)]

#: Longest job title the owner may type. A title, not a paragraph: the value is
#: sent verbatim as a search query, and a sentence matches nothing.
MAX_TARGET_TITLE_CHARS = 100
#: Most titles one profile may hold. Each one becomes at least one query per
#: run, and the run's query budget is eight.
MAX_TARGET_TITLES = 12


def clean_target_titles(titles: list[str]) -> list[str]:
    """Collapse whitespace, drop blanks and repeats, keep the owner's order.

    Repeats are compared case-insensitively — «Python Developer» and «python
    developer» are one search — and the first spelling wins, since that is the
    one the owner typed first.
    """
    seen: set[str] = set()
    cleaned: list[str] = []
    for title in titles:
        text = " ".join(title.split())
        if not text or text.casefold() in seen:
            continue
        if len(text) > MAX_TARGET_TITLE_CHARS:
            raise ValueError(
                f"job title longer than {MAX_TARGET_TITLE_CHARS} characters: {text[:40]}..."
            )
        seen.add(text.casefold())
        cleaned.append(text)
    if len(cleaned) > MAX_TARGET_TITLES:
        raise ValueError(f"at most {MAX_TARGET_TITLES} job titles")
    return cleaned


TargetTitles = Annotated[list[str], AfterValidator(clean_target_titles)]


def reject_duplicate_skills(skills: list["SkillCreate"] | None) -> None:
    """Refuse a skill set that would violate the database's unique constraint.

    ``profile_skill`` is unique on ``(profile_id, canonical_name)``, so a
    repeated canonical name reaches PostgreSQL as an IntegrityError and, with
    nothing catching it, becomes an opaque 500 that does not say which skill was
    duplicated. A hand-typed correction list is exactly where a duplicate comes
    from, so it is caught here and reported as a validation error naming it.
    """
    if not skills:
        return
    seen: set[str] = set()
    duplicates: list[str] = []
    for skill in skills:
        name = skill.canonical_name
        if name in seen and name not in duplicates:
            duplicates.append(name)
        seen.add(name)
    if duplicates:
        raise ValueError(f"canonical_name must be unique; repeated: {sorted(duplicates)}")


class SkillCreate(BaseModel):
    """One skill as extracted from a resume."""

    canonical_name: str = Field(min_length=1, max_length=100)
    #: Every spelling the resume used. Canonicalisation collapses variants, so
    #: this is a list rather than one string.
    raw_names: list[str] = Field(default_factory=list)
    years: Years | None = None
    level: SkillLevel = SkillLevel.WORKING
    #: Whether dated work backs the level up. Shown in the UI and given to the
    #: LLM verdict; whether it changes the score is a phase 5 decision.
    evidence: SkillEvidence = SkillEvidence.STATED
    last_used_year: int | None = Field(default=None, ge=1970, le=2100)


class ExperienceCreate(BaseModel):
    """One job as extracted from a resume, on its way to ``profile_experience``.

    The extraction has always produced these and nothing stored them, which was
    harmless while the only consumers were a total-years figure and a per-skill
    year count. A CV generated for a vacancy *is* this list, so from migration
    0010 they are rows — and the generator quotes them rather than restating
    them, which is what makes "the generated CV does not change dates, company
    names or job titles" a property of the schema.
    """

    #: Order in the resume, newest first as resumes are written. Unique per
    #: profile, and the handle the generator refers to a job by.
    position: int = Field(ge=0)
    company: str = Field(max_length=300)
    title: str = Field(max_length=300)
    #: "YYYY-MM". A string and not a date: a resume that gives only a year does
    #: not state a month, and a date column would have to invent one.
    start: str | None = Field(default=None, max_length=7)
    end: str | None = Field(default=None, max_length=7)
    is_current: bool = False
    stack: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)


class SkillRead(ReadModel):
    """A skill of the candidate."""

    id: UUID
    canonical_name: str
    raw_names: list[str]
    years: Decimal | None
    level: SkillLevel
    evidence: SkillEvidence
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
    #: Degrees, as the extraction recorded them. A JSONB column on the profile,
    #: like ``languages`` beside it: a short list of flat records, read whole.
    education: list[dict[str, Any]] = Field(default_factory=list)
    raw_text: str | None = None
    skills: list[SkillCreate] = Field(default_factory=list)
    #: Rows of their own, so a generated CV can refer to a job rather than
    #: retype one. Written by ``ProfileRepository.replace_experience``.
    experience: list[ExperienceCreate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _skills_are_unique(self) -> Self:
        """Defence in depth: the enricher already merges, this catches a regression."""
        reject_duplicate_skills(self.skills)
        return self


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
    #: What the owner wants to be hired as. Replaces the whole list when present;
    #: an empty list returns the search to skill keywords.
    target_titles: TargetTitles | None = None
    is_active: bool | None = None
    #: Replaces the whole skill set when present. The extractor is wrong often
    #: enough that hand-correcting skills is a first-class operation, not an
    #: afterthought.
    skills: list[SkillCreate] | None = None

    @model_validator(mode="after")
    def _skills_are_unique(self) -> Self:
        """A repeated canonical name is a 422 with a name in it, not a 500."""
        reject_duplicate_skills(self.skills)
        return self


class ResumeUploadResponse(BaseModel):
    """Answer to an upload: an id to poll, and where parsing has got to."""

    profile_id: UUID
    parse_status: ParseStatus


class CandidateProfileRead(ReadModel):
    """Full profile, skills included."""

    id: UUID
    name: str | None
    headline: str | None
    seniority: Seniority | None
    total_years: Decimal | None
    summary: str | None
    locations: list[str]
    target_titles: list[str] = Field(default_factory=list)
    relocation: bool
    remote_pref: RemoteType | None
    salary_min: Decimal | None
    salary_currency: str | None
    languages: list[dict[str, Any]]
    is_active: bool

    parse_status: ParseStatus
    #: Why parsing failed, safe to show a user. Never contains resume text.
    parse_error: str | None
    parse_started_at: datetime | None
    resume_filename: str | None
    resume_size_bytes: int | None
    resume_format: str | None

    created_at: datetime
    updated_at: datetime
    skills: list[SkillRead] = Field(default_factory=list)
    #: The raw resume text and the embedding are intentionally absent: one is
    #: large and one is meaningless to a client.


class SkillElsewhere(BaseModel):
    """A skill some other resume of the same owner lists.

    Here rather than in ``schemas/dashboard.py``, where the screen that shows it
    lives: the data layer reads it — ``ProfileRepository.skills_elsewhere`` —
    and that module imports the letter service for another model, so a
    repository importing it there closes a cycle through ``services/ats.py``.
    Re-exported from ``dashboard`` so nothing on the screen side moved.
    """

    canonical_name: str
    profile_id: UUID
    resume_filename: str | None = None
    profile_name: str | None = None
