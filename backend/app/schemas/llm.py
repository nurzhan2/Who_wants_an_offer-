"""The contract the LLM must fill in when it reads a resume.

Kept deliberately flat. Structured outputs constrain generation against this
schema, and deeply nested optionals are where that constraint starts to
misbehave; a list of flat records is both easier for the model and easier to
validate.

One field name matters more than the rest: ``stated_total_years``. The resume's
own claim about total experience is captured but never used — people work in
parallel (job plus freelance plus side projects) and summing durations gives a
third-year student twelve years of experience. The real number is computed in
``app.resume.dates`` by merging overlapping intervals.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Where a skill was found. A skill listed only in a "skills" block carries less
#: evidence than one the candidate describes doing at a named employer.
SkillMention = Literal["skills_block", "work_description", "project", "education", "other"]

#: Self-assessed depth, when the resume says so explicitly. Otherwise null and
#: the enricher infers it from years and mention sites.
StatedLevel = Literal["basic", "working", "strong", "expert"]

RemotePreference = Literal["no", "hybrid", "full"]


class StrictModel(BaseModel):
    """Base for the extraction contract: no fields the schema did not declare."""

    model_config = ConfigDict(extra="forbid")


class WorkPeriod(StrictModel):
    """One job, with the dates the experience calculation needs."""

    company: str
    title: str
    #: ISO "YYYY-MM". The prompt normalises every date format to this.
    start: str | None = Field(default=None, description="YYYY-MM, or null if absent.")
    #: Null while the job is current; ``is_current`` says which it is.
    end: str | None = Field(default=None, description="YYYY-MM, or null if current.")
    is_current: bool = False
    #: Technologies the resume attributes to this job. Feeds per-skill years.
    stack: list[str] = Field(default_factory=list)
    #: fintech, e-commerce, edtech, gamedev...
    domains: list[str] = Field(default_factory=list)


class ExtractedSkill(StrictModel):
    """A skill as the resume spells it, before canonicalisation."""

    name: str
    level: StatedLevel | None = None
    mentioned_in: SkillMention = "other"
    #: Companies from ``work_periods`` where this skill was used, by name. The
    #: per-skill year count is the union of those jobs' intervals.
    companies: list[str] = Field(default_factory=list)


class ExtractedLanguage(StrictModel):
    """A spoken language and its level."""

    #: ISO 639-1 where the model can tell; the enricher normalises the rest.
    code: str
    #: CEFR (A1..C2) or "native".
    level: str | None = None


class Education(StrictModel):
    """One degree or programme."""

    institution: str
    degree: str | None = None
    field: str | None = None
    end_year: int | None = None


class ProfileExtraction(StrictModel):
    """Everything the model is asked to read out of a resume."""

    full_name: str | None = None
    headline: str | None = None
    summary: str | None = None
    city: str | None = None
    #: ISO 3166-1 alpha-2 where the model can tell.
    country: str | None = None
    relocation: bool | None = None
    remote_pref: RemotePreference | None = None
    salary_expectation: float | None = None
    salary_currency: str | None = None

    work_periods: list[WorkPeriod] = Field(default_factory=list)
    skills: list[ExtractedSkill] = Field(default_factory=list)
    languages: list[ExtractedLanguage] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)

    #: What the resume CLAIMS about total experience. Recorded for comparison
    #: and never written to the profile — see the module docstring.
    stated_total_years: float | None = None
