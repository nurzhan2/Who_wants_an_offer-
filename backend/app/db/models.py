"""SQLAlchemy 2.0 ORM models. Schema of record: docs/ARCHITECTURE.md.

Conventions applied throughout:

* Money is ``Numeric(12, 2)``. Never a float — binary floats cannot represent
  a salary exactly, and the error compounds through currency conversion.
* Every score lives on a single 0-100 scale as ``Numeric(5, 2)``, including the
  component scores that docs/MATCHING.md expresses as 0..1 fractions; they are
  normalised on write. One scale beats remembering which column uses which.
* Open-ended vocabularies (currency, country, language) are fixed-width strings
  validated by Pydantic, not native enums: ``ALTER TYPE`` in PostgreSQL is a
  migration hazard and these lists keep growing.
* Collections always needed with their parent use ``lazy="selectin"``.
  Collections that must not load implicitly use ``lazy="raise"`` — an explicit
  error beats an N+1 discovered in production.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CHAR,
    Boolean,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.enums import (
    ApplicationStatus,
    EmploymentType,
    MatchBucket,
    ParseStatus,
    PipelineRunStatus,
    RemoteType,
    SalaryPeriod,
    Seniority,
    SkillEvidence,
    SkillLevel,
    VacancyCompleteness,
    pg_enum,
)

#: Salaries, normalised salaries and any other monetary amount.
Money = Numeric(12, 2)
#: Any score, always on the 0-100 scale.
Score = Numeric(5, 2)
#: Years of experience, one decimal place.
Years = Numeric(4, 1)


class CandidateProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Structured resume: what the candidate is and what they want."""

    __tablename__ = "candidate_profile"

    name: Mapped[str | None] = mapped_column(String(200))
    headline: Mapped[str | None] = mapped_column(String(300))
    seniority: Mapped[Seniority | None] = mapped_column(pg_enum(Seniority, "seniority"))
    total_years: Mapped[Decimal | None] = mapped_column(Years)
    summary: Mapped[str | None] = mapped_column(Text)

    locations: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    relocation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    remote_pref: Mapped[RemoteType | None] = mapped_column(pg_enum(RemoteType, "remote_type"))

    salary_min: Mapped[Decimal | None] = mapped_column(Money)
    salary_currency: Mapped[str | None] = mapped_column(CHAR(3))
    #: [{"code": "en", "level": "C1"}, ...]
    languages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)

    raw_text: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dim))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Extraction runs in the background; the client polls these.
    parse_status: Mapped[ParseStatus] = mapped_column(
        pg_enum(ParseStatus, "parse_status"),
        default=ParseStatus.PENDING,
        nullable=False,
    )
    #: Why extraction failed, in a form a human can act on. Never contains
    #: resume text: this reaches the API and the logs.
    parse_error: Mapped[str | None] = mapped_column(Text)
    parse_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Whether a machine can read the uploaded file, as
    #: :class:`app.schemas.ats.ATSReport`. Computed once at upload — the file
    #: itself is deleted when parsing ends, so it cannot be recomputed later.
    #: NULL means no audit was recorded, which is not the same as a clean one.
    ats_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # What the user uploaded, so the dashboard can say which resume is live.
    resume_filename: Mapped[str | None] = mapped_column(String(255))
    resume_size_bytes: Mapped[int | None] = mapped_column(Integer)
    resume_format: Mapped[str | None] = mapped_column(String(10))

    skills: Mapped[list["ProfileSkill"]] = relationship(
        back_populates="profile",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    matches: Mapped[list["Match"]] = relationship(
        back_populates="profile",
        cascade="all, delete-orphan",
        lazy="raise",
    )


class ProfileSkill(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One canonicalised skill of the candidate, with depth and recency."""

    __tablename__ = "profile_skill"
    __table_args__ = (UniqueConstraint("profile_id", "canonical_name"),)

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    canonical_name: Mapped[str] = mapped_column(String(100), nullable=False)
    #: Every spelling the resume used for this skill. A list rather than one
    #: string because canonicalisation collapses variants — "Python" and
    #: "Python 3" both become ``python`` — and losing the originals would make
    #: the skill dictionary impossible to debug.
    raw_names: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    years: Mapped[Decimal | None] = mapped_column(Years)
    level: Mapped[SkillLevel] = mapped_column(
        pg_enum(SkillLevel, "skill_level"),
        default=SkillLevel.WORKING,
        nullable=False,
    )
    #: How well is one question; how we know is another. See SkillEvidence.
    evidence: Mapped[SkillEvidence] = mapped_column(
        pg_enum(SkillEvidence, "skill_evidence"),
        default=SkillEvidence.STATED,
        nullable=False,
    )
    last_used_year: Mapped[int | None] = mapped_column(Integer)

    profile: Mapped[CandidateProfile] = relationship(back_populates="skills")


class Vacancy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A job posting, deduplicated across every source that carries it."""

    __tablename__ = "vacancy"

    #: sha1(normalised company, title, city); the deduplication key.
    fingerprint: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    company: Mapped[str | None] = mapped_column(String(200))
    company_url: Mapped[str | None] = mapped_column(String(500))
    description_raw: Mapped[str | None] = mapped_column(Text)
    description_md: Mapped[str | None] = mapped_column(Text)

    seniority: Mapped[Seniority | None] = mapped_column(pg_enum(Seniority, "seniority"))
    min_years: Mapped[Decimal | None] = mapped_column(Years)

    city: Mapped[str | None] = mapped_column(String(120))
    #: ISO 3166-1 alpha-2.
    country: Mapped[str | None] = mapped_column(CHAR(2))
    remote: Mapped[RemoteType] = mapped_column(
        pg_enum(RemoteType, "remote_type"),
        default=RemoteType.NO,
        nullable=False,
    )

    # Salary exactly as advertised — this is what the UI shows.
    salary_min: Mapped[Decimal | None] = mapped_column(Money)
    salary_max: Mapped[Decimal | None] = mapped_column(Money)
    #: ISO 4217, validated by Pydantic rather than a native enum.
    currency: Mapped[str | None] = mapped_column(CHAR(3))
    is_gross: Mapped[bool | None] = mapped_column(Boolean)
    period: Mapped[SalaryPeriod | None] = mapped_column(pg_enum(SalaryPeriod, "salary_period"))

    # Monthly USD equivalent — this is what sorting and filtering use. Comparing
    # raw amounts across currencies ranks 500000 KZT above 4000 USD. Populated by
    # the normalisation phase; the columns exist now so landing that does not
    # cost a second migration.
    salary_min_normalized: Mapped[Decimal | None] = mapped_column(Money)
    salary_max_normalized: Mapped[Decimal | None] = mapped_column(Money)
    #: When the conversion ran; rates go stale and the value must be recomputed.
    salary_normalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    employment_type: Mapped[EmploymentType | None] = mapped_column(
        pg_enum(EmploymentType, "employment_type")
    )
    #: ISO 639-1 language of the posting text.
    language: Mapped[str | None] = mapped_column(CHAR(2))

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    #: Which fingerprint algorithm produced :attr:`fingerprint`. Travels with
    #: it, always written by app.normalize.fingerprint, so a re-crawl can only
    #: ever write the version it actually used. Phase 4 will improve company and
    #: city normalisation, which changes the fingerprint and collapses rows that
    #: are distinct today — its recompute script bumps this and merges them.
    fingerprint_version: Mapped[int] = mapped_column(
        SmallInteger, default=1, server_default="1", nullable=False
    )
    #: How much of the posting we hold. Matching reads it to decide what it is
    #: allowed to conclude from a row with no description.
    completeness: Mapped[VacancyCompleteness] = mapped_column(
        pg_enum(VacancyCompleteness, "vacancy_completeness"),
        default=VacancyCompleteness.FULL,
        nullable=False,
    )
    #: Who may actually take this job: {mode, allowed_countries, sponsorship,
    #: evidence}. Populated in phase 4. For a candidate outside the US or the
    #: EU this is the most valuable filter in the system — half of a global
    #: search is remote-in-name-only and closed by right-to-work.
    work_authorization: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: Resume farms, placement-programme sellers and scraped filler. Roughly a
    #: third of a global aggregator feed.
    is_spam: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    #: What made it look like spam, so the verdict can be argued with.
    spam_signals: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: When phase 4 last enriched this row. NULL means never.
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dim))
    #: sha256 of the exact text :attr:`embedding` was computed from. Kept out of
    #: REFRESHABLE_COLUMNS on purpose: a re-crawl that rewrites description_raw
    #: must leave this stale, because staleness is precisely the signal that the
    #: vector needs recomputing.
    embedding_text_hash: Mapped[str | None] = mapped_column(CHAR(64))
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Full-text search column. The configuration is hardcoded to 'simple' on
    # purpose: postings mix Russian and English, and to_tsvector(regconfig, text)
    # is not IMMUTABLE, so a per-row configuration cannot appear in a generated
    # column — the migration would simply refuse to apply. Language-aware
    # stemming, if it is ever needed, has to be a separate expression index.
    # Do not "fix" this into a per-row configuration.
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', coalesce(title, '') || ' ' || "
            "coalesce(company, '') || ' ' || coalesce(description_raw, ''))",
            persisted=True,
        ),
        nullable=False,
    )

    sources: Mapped[list["VacancySource"]] = relationship(
        back_populates="vacancy",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    skills: Mapped[list["VacancySkill"]] = relationship(
        back_populates="vacancy",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    matches: Mapped[list["Match"]] = relationship(
        back_populates="vacancy",
        cascade="all, delete-orphan",
        lazy="raise",
    )
    applications: Mapped[list["Application"]] = relationship(
        back_populates="vacancy",
        cascade="all, delete-orphan",
        lazy="raise",
    )


class VacancySource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Where one vacancy was seen. A cross-posted job has several of these."""

    __tablename__ = "vacancy_source"
    __table_args__ = (UniqueConstraint("source_slug", "external_id"),)

    vacancy_id: Mapped[UUID] = mapped_column(
        ForeignKey("vacancy.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_slug: Mapped[str] = mapped_column(String(50), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    #: Untouched source payload, so the normaliser can re-run without refetching.
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    vacancy: Mapped[Vacancy] = relationship(back_populates="sources")


class VacancySkill(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A skill the vacancy asks for, with how hard the requirement is."""

    __tablename__ = "vacancy_skill"
    __table_args__ = (UniqueConstraint("vacancy_id", "canonical_name"),)

    vacancy_id: Mapped[UUID] = mapped_column(
        ForeignKey("vacancy.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    canonical_name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: 1.00 in the requirements block or the title, 0.60 for a passing mention.
    weight: Mapped[Decimal] = mapped_column(Numeric(3, 2), default=Decimal("1.00"), nullable=False)

    vacancy: Mapped[Vacancy] = relationship(back_populates="skills")


class Match(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Explained fit between one profile and one vacancy."""

    __tablename__ = "match"
    __table_args__ = (UniqueConstraint("profile_id", "vacancy_id"),)

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_profile.id", ondelete="CASCADE"),
        nullable=False,
    )
    vacancy_id: Mapped[UUID] = mapped_column(
        ForeignKey("vacancy.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    score: Mapped[Decimal] = mapped_column(Score, nullable=False)
    rule_score: Mapped[Decimal] = mapped_column(Score, nullable=False)
    semantic_score: Mapped[Decimal | None] = mapped_column(Score)
    llm_score: Mapped[Decimal | None] = mapped_column(Score)
    bucket: Mapped[MatchBucket] = mapped_column(
        pg_enum(MatchBucket, "match_bucket"), nullable=False
    )

    #: Per-component breakdown, all on the same 0-100 scale.
    component_scores: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    matched_skills: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    missing_required: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    missing_nice: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    red_flags: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)

    experience_gap_years: Mapped[Decimal | None] = mapped_column(Years)
    verdict: Mapped[str | None] = mapped_column(Text)
    application_angle: Mapped[str | None] = mapped_column(Text)
    scored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    profile: Mapped[CandidateProfile] = relationship(back_populates="matches")
    vacancy: Mapped[Vacancy] = relationship(back_populates="matches")


class Application(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Personal tracker entry for a vacancy the candidate acted on."""

    __tablename__ = "application"

    vacancy_id: Mapped[UUID] = mapped_column(
        ForeignKey("vacancy.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[ApplicationStatus] = mapped_column(
        pg_enum(ApplicationStatus, "application_status"),
        default=ApplicationStatus.SAVED,
        nullable=False,
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    cover_letter: Mapped[str | None] = mapped_column(Text)

    vacancy: Mapped[Vacancy] = relationship(back_populates="applications")


class PipelineRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One source run inside a pipeline execution, successful or not."""

    __tablename__ = "pipeline_run"

    source_slug: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    status: Mapped[PipelineRunStatus] = mapped_column(
        pg_enum(PipelineRunStatus, "pipeline_run_status"),
        default=PipelineRunStatus.RUNNING,
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: One entry per failure; a broken source must not abort the whole run.
    errors: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)


class SourceQuota(Base):
    """How many metered requests a source has spent today.

    Its own table because nothing else can answer the question. ``PipelineRun``
    counts runs, and a metered API charges per *page*: a run that died halfway
    through pagination spent real credits and left no record of them, so
    deriving the number from run history undercounts exactly when it matters.

    Keyed on the day rather than on a rolling window because that is how the
    vendors bill — JSearch's Basic tier is 100 requests a day, reset at
    midnight UTC — and because ``ON CONFLICT (source_slug, day) DO UPDATE SET
    used = used + 1`` is then a single atomic statement with no read-modify-
    write race between concurrent connectors.

    No surrogate id: the natural key is the whole row's identity, and a UUID
    would only invite a second row for the same source and day.
    """

    __tablename__ = "source_quota"

    source_slug: Mapped[str] = mapped_column(String(50), primary_key=True)
    #: UTC calendar day, matching the vendor's own reset boundary.
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    #: Incremented when a request is SENT, never when one succeeds. A 500 has
    #: already cost a credit; counting successes walks straight past the limit
    #: and into a 429 that looks unexplainable.
    used: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SourceState(Base):
    """Where a source got to last time, under a key the source chooses.

    A crawler that walks a large corpus in slices has to remember its position
    across runs, and nothing else here can hold that. ``PipelineRun`` records
    what a run did, not where inside a source it stopped; ``SourceQuota``
    counts requests. Deriving a position from run timestamps is wrong in the
    one case that matters — a run that died halfway would advance a watermark
    past pages it never fetched, and those postings would never be collected.

    Deliberately opaque to the framework. The key is a string the connector
    invents (hh uses one per sitemap file, because a sitemap file is what its
    ``lastmod`` values are grouped by), and the value is JSONB the connector
    writes and reads back. Making this table know about sitemaps would put a
    connector's business in the schema, which is the thing CLAUDE.md rule 5
    exists to prevent.

    No surrogate id, for the same reason ``SourceQuota`` has none: the natural
    key is the row's identity, and a UUID would invite a second row for it.
    """

    __tablename__ = "source_state"

    source_slug: Mapped[str] = mapped_column(String(50), primary_key=True)
    #: Namespaced by the connector. Never a user identifier and never a URL with
    #: a credential in it — this table is dumped in support conversations.
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
