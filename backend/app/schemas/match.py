"""Match contracts. The UI never shows a bare number, so neither does the API."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field

from app.db.enums import MatchBucket, RequirementSource
from app.schemas.common import ReadModel

Score = Annotated[Decimal, Field(ge=0, le=100, decimal_places=2)]


class MatchComponentScores(BaseModel):
    """Per-component breakdown behind the final score.

    All are on the 0-100 scale, even though docs/MATCHING.md defines them as
    0..1 fractions — the conversion happens on write so nothing downstream has
    to remember which column uses which scale. Which of them moved the score
    depends on the formula, and the stored verdict names them.
    """

    #: Title against headline. Defaulted so that every match stored before
    #: ``0015_title_embedding`` keeps validating.
    title_similarity: Score = Decimal("0.00")
    skill_coverage_required: Score = Decimal("0.00")
    skill_coverage_nice: Score = Decimal("0.00")
    semantic_similarity: Score = Decimal("0.00")
    experience_fit: Score = Decimal("0.00")
    domain_fit: Score = Decimal("0.00")
    logistics_fit: Score = Decimal("0.00")


class MatchedSkill(BaseModel):
    """A required skill the candidate covers, and how well."""

    canonical_name: str
    #: 1.0 exact, 0.95 alias, 0.5-0.7 related technology, 0.25 same group.
    coverage: Decimal = Field(ge=0, le=1)
    is_required: bool = True
    #: Whether the employer named this requirement or it was read out of their
    #: description. Defaulted to the employer's own field so that every match
    #: stored before ``0014_requirement_source`` keeps validating as what it
    #: was — at that point nothing else could write a requirement.
    source: RequirementSource = RequirementSource.EMPLOYER_FIELD


class MissingSkill(BaseModel):
    """A skill the vacancy wants and the candidate does not have."""

    canonical_name: str
    weight: Decimal = Field(ge=0, le=1)
    #: As on :class:`MatchedSkill`, and it matters more here: "you do not have
    #: this" about a requirement nobody stated is a gap this project inferred,
    #: and a person deciding whether to apply may see which kind it is.
    source: RequirementSource = RequirementSource.EMPLOYER_FIELD


class MatchCreate(BaseModel):
    """Scoring result, ready to be upserted."""

    profile_id: UUID
    vacancy_id: UUID
    score: Score
    rule_score: Score
    semantic_score: Score | None = None
    llm_score: Score | None = None
    bucket: MatchBucket
    component_scores: MatchComponentScores = Field(default_factory=MatchComponentScores)
    matched_skills: list[MatchedSkill] = Field(default_factory=list)
    missing_required: list[MissingSkill] = Field(default_factory=list)
    missing_nice: list[MissingSkill] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    experience_gap_years: Decimal | None = Field(default=None, ge=-60, le=60)
    verdict: str | None = None
    application_angle: str | None = None


class MatchDetail(ReadModel):
    """Full explanation of one match: the score plus why it is that score."""

    id: UUID
    profile_id: UUID
    vacancy_id: UUID
    score: Decimal
    rule_score: Decimal
    semantic_score: Decimal | None
    llm_score: Decimal | None
    bucket: MatchBucket
    component_scores: MatchComponentScores
    matched_skills: list[MatchedSkill]
    missing_required: list[MissingSkill]
    missing_nice: list[MissingSkill]
    red_flags: list[str]
    experience_gap_years: Decimal | None
    verdict: str | None
    application_angle: str | None
    scored_at: datetime
