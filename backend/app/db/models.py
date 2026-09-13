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
    DocumentKind,
    DocumentSource,
    EmploymentType,
    MatchBucket,
    ParseStatus,
    PipelineRunStatus,
    ReferenceKind,
    RemoteType,
    RequirementSource,
    RuleKind,
    RuleScope,
    RuleSeverity,
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
    #: Degrees and programmes, as ``app.schemas.llm.Education`` records them.
    #: JSONB and on this table for exactly the reasons ``languages`` is: a short
    #: list of flat records, read whole, never queried by field. The extraction
    #: has always produced it and nothing stored it, which cost nothing until a
    #: generated CV needed an education section — and a CV without one trips
    #: this project's own ATS audit (``MISSING_SECTIONS``), correctly.
    education: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)

    raw_text: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dim))
    #: The headline alone, compared with each vacancy's title. Kept apart from
    #: :attr:`embedding` for the reason ``0015_title_embedding`` gives: a title
    #: compared with a paragraph is not the same measurement as a title
    #: compared with a title.
    headline_embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dim))
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
    experience: Mapped[list["ProfileExperience"]] = relationship(
        back_populates="profile",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ProfileExperience.position",
    )
    matches: Mapped[list["Match"]] = relationship(
        back_populates="profile",
        cascade="all, delete-orphan",
        lazy="raise",
    )


class ProfileContact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """How to reach the candidate: the block printed at the top of every CV.

    **A table of its own rather than columns on ``candidate_profile``**, which
    was the choice to justify:

    * *Contacts must not travel with the profile.* The profile is loaded by the
      matcher, embedded, re-scored and serialised on every dashboard request.
      As columns, a phone number would ride along in every one of those reads
      and would be one ``from_attributes`` field away from an API response, an
      LLM prompt or a log line. As a table with no relationship pointing at it
      from :class:`CandidateProfile`, reaching a phone number takes a
      deliberate join — the privacy boundary this data needs is enforced by the
      schema instead of by everyone remembering.
    * *They change for different reasons.* Every field on ``candidate_profile``
      is derived from a document and is rewritten wholesale each time that
      document is re-parsed. These are facts about a person that the owner
      edits by hand and that must survive exactly such a re-parse; mixing the
      two in one row means one ``UPDATE ... SET`` away from losing them.
    * *They are written by a different path.* Nothing in the pipeline writes
      here except the prefill step, and the API endpoint that writes here
      touches nothing else. One row, one writer, no partial overlap.

    One row per profile, enforced by a unique constraint on ``profile_id``
    rather than by making it the primary key: the links table points at this
    row's own id, so a profile that is deleted and re-created cannot leave
    links attached to the wrong contact block.
    """

    __tablename__ = "profile_contact"
    __table_args__ = (UniqueConstraint("profile_id"),)

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    full_name: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(320))
    city: Mapped[str | None] = mapped_column(String(120))

    # One flag per field, not one flag for the row. Prefill fills the gaps a
    # person has not filled in themselves, so it has to know which gaps those
    # are: a correction to the phone number must not freeze the email address
    # the resume would have supplied.
    full_name_edited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    phone_edited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_edited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    city_edited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    links: Mapped[list["ProfileContactLink"]] = relationship(
        back_populates="contact",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ProfileContactLink.position",
    )


class ProfileContactLink(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One address the candidate can be looked up at.

    Rows rather than columns because the set is open. ``github``, ``telegram``
    and ``linkedin`` are what this market asks for today; the next resume adds
    a personal site, a portfolio, a package registry — and none of those should
    cost a migration. ``kind`` is a plain slug for the same reason the rest of
    the schema keeps open vocabularies out of PostgreSQL enums.

    ``position`` preserves the order the owner arranged, which is the order the
    generated document prints. It is not unique: reordering a list under a
    unique constraint means a delete-then-insert dance for no benefit.
    """

    __tablename__ = "profile_contact_link"
    __table_args__ = (UniqueConstraint("contact_id", "url"),)

    contact_id: Mapped[UUID] = mapped_column(
        ForeignKey("profile_contact.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    label: Mapped[str | None] = mapped_column(String(60))
    #: True when a person typed or kept this link. Re-parsing a resume replaces
    #: only the extracted ones; a link a human put here outlives every upload.
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)

    contact: Mapped[ProfileContact] = relationship(back_populates="links")


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


class ProfileExperience(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One job from the resume, kept because a tailored CV is built out of these.

    The extraction has always produced ``work_periods`` — company, title, dates,
    the stack attributed to that job — and until now nothing stored them: they
    were used to compute ``total_years`` and per-skill years and then dropped on
    the floor. That was enough while the only consumers were a number and a
    letter, and it stopped being enough the moment a CV had to be generated,
    because a CV *is* this list.

    The row is what makes the generator's central promise checkable. A tailored
    CV may reorder these entries and choose which of them to keep; it may not
    edit one. Company, title and dates are written into the document from these
    columns and are never taken from the model's answer — so "the generator does
    not change dates, company names or job titles" is a property of where the
    strings come from rather than a rule somebody remembered to check.

    Replaced wholesale on every parse, like ``profile_skill``: a re-parse of a
    corrected resume must not leave last week's jobs behind.
    """

    __tablename__ = "profile_experience"
    __table_args__ = (UniqueConstraint("profile_id", "position"),)

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Order in the resume, newest first as resumes are written. Kept so the
    #: default arrangement is the candidate's own, and so a generated CV can be
    #: compared against it: a reordering is only visible against an original.
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    company: Mapped[str] = mapped_column(String(300), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    #: "YYYY-MM" as the extraction normalises every date format to. Stored as
    #: text rather than a Date because a resume that gives only a year genuinely
    #: does not state a month, and inventing ``-01`` in the column would make
    #: the two cases indistinguishable to anything reading it back.
    start: Mapped[str | None] = mapped_column(String(7))
    #: NULL while the job is current; ``is_current`` says which of the two.
    end: Mapped[str | None] = mapped_column(String(7))
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Technologies the resume attributes to *this* job. A tailored CV may show
    #: a subset of it, chosen for the vacancy, and never anything outside it.
    stack: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    #: fintech, e-commerce, gamedev... as the extraction recorded them.
    domains: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)

    profile: Mapped[CandidateProfile] = relationship(back_populates="experience")


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
    #: The title alone. Matching's main signal since ``0015_title_embedding``:
    #: dissolved into :attr:`embedding` it hardly moved a score. Out of
    #: REFRESHABLE_COLUMNS for the same reason as the hash above.
    title_embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dim))
    #: sha256 of the exact title :attr:`title_embedding` was computed from.
    title_embedding_hash: Mapped[str | None] = mapped_column(CHAR(64))

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
    documents: Mapped[list["GeneratedDocument"]] = relationship(
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
    #: Who says this is a requirement — the employer's own field, or our reading
    #: of their description. Never mixed: see :class:`RequirementSource`.
    source: Mapped[RequirementSource] = mapped_column(
        pg_enum(RequirementSource, "requirement_source"),
        default=RequirementSource.EMPLOYER_FIELD,
        nullable=False,
    )

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
    """Personal tracker entry for a vacancy the candidate acted on.

    Three groups of columns, and the group a column belongs to decides who is
    allowed to write it.

    **The person's.** ``status``, ``applied_at``, ``notes``, ``cover_letter``.
    A kanban position, a date, free text, the letter as it currently stands.
    ``notes`` in particular is *theirs*: nothing in this codebase writes it.
    Until 2026-09-07 the apply agent's report was rendered into a marked block
    inside it, which the session that shipped that called the weakest part of
    the change in its own commit message — a text blob cannot be filtered,
    grouped or counted, and rewriting somebody's notes column on every result
    put machine output where a person's sentences live. Migration
    ``0008_application_send_record`` moved that block into the columns below
    and parsed the existing text forward.

    **What the agent recorded at send time** (``sent_*``, ``agent_*``,
    ``match_*``, ``vacancy_key_skills``). A snapshot, deliberately duplicating
    data that lives elsewhere in normalised form, because everything it
    duplicates is mutable: ``match`` rows are re-upserted by every scoring run,
    ``cover_letter`` is overwritten by every regeneration, and a re-crawl
    rewrites the posting. Without the snapshot, the question a feedback loop
    exists to answer — *what did the employer actually read, and what did we
    believe when we sent it* — has no answer a month later. This is the one
    place in the schema where a copy is the point rather than a smell.

    **What hh said** (``hh_*``). Quoted from somebody else's site, never
    parsed into a verdict of ours, and prefixed so that no reader mistakes
    hh's count of applications for one this project computed.

    NULL is never zero and never "no" in this table. ``sent_at IS NULL`` means
    the agent never reported a send for this row, which is what separates a
    tracker entry a person typed from an application that actually went out;
    ``match_score IS NULL`` means nothing scored this pairing, while ``0.00``
    would be a verdict; ``hh_negotiations_total IS NULL`` means nobody
    measured, while ``0`` is hh saying there are none. There are two sent
    applications on this account, and a dashboard that turns "not recorded"
    into a number would be inventing the only statistics it has.

    No index on any of them. This is one person's tracker — tens of rows, not
    millions — and what makes ``WHERE sent_at IS NOT NULL GROUP BY
    hh_last_state`` possible is that the values are columns at all, not that
    they are indexed.
    """

    __tablename__ = "application"

    vacancy_id: Mapped[UUID] = mapped_column(
        ForeignKey("vacancy.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Whose resume this was written from. Nullable because a row typed into the
    #: tracker by hand has no profile, and because rows predating migration 0009
    #: cannot be attributed. NULL is read as "not evidence about any resume":
    #: past letters are fed back as few-shot examples, and one written from a
    #: different resume would teach the model to claim experience this candidate
    #: does not have. SET NULL on delete — removing a resume must not remove the
    #: record that an application was sent.
    profile_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("candidate_profile.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[ApplicationStatus] = mapped_column(
        pg_enum(ApplicationStatus, "application_status"),
        default=ApplicationStatus.SAVED,
        nullable=False,
    )
    #: The person's own date on their own kanban, editable through the tracker
    #: API. :attr:`sent_at` is the machine's; the two are kept apart so that
    #: correcting a date by hand cannot move a measurement.
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: What a person typed. Nothing in this codebase writes it.
    notes: Mapped[str | None] = mapped_column(Text)
    #: The letter as it stands now — written and rewritten by
    #: ``app/letters/store.save_letter``. Not evidence of what was sent: a
    #: regeneration after a send replaces it in place. See :attr:`sent_letter`.
    cover_letter: Mapped[str | None] = mapped_column(Text)
    #: Which version of ``app/letters/guard`` judged :attr:`cover_letter`,
    #: written by the same call that wrote the letter. NULL means the letter
    #: predates the record — not that it was written under version zero, and not
    #: that it passes today's rules. A letter outlives the rules that wrote it,
    #: and without this the documents screen could only show the rules in force
    #: now and imply they produced everything on it.
    letter_rules_version: Mapped[str | None] = mapped_column(String(64))

    # ── the send, as the agent reported it ────────────────────────────
    #: When an application actually went out, from the report that said so.
    #: Written once and never by a person. NULL means this row was never sent
    #: by the agent, which includes every row a person created by hand.
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The exact text typed into hh's form, as the agent reported it — not a
    #: copy of :attr:`cover_letter`, which by then may have been regenerated,
    #: and which for a vacancy carrying two tracker rows may never have been
    #: the row this result landed on. Only the process that did the typing
    #: knows this string, so only its report may write it. NULL means the
    #: agent did not report one; ``""`` means it reported that nothing was
    #: typed, which hh allows.
    sent_letter: Mapped[str | None] = mapped_column(Text)
    #: Where the agent left this application: ``app.schemas.agent.AgentStatus``.
    #: A string rather than a native enum, unlike every other closed
    #: vocabulary here, because this set is not ours to freeze — it mirrors the
    #: state machine in ``agent/state.py``, a package this one may not import,
    #: and a state added there would need ``ALTER TYPE`` here before a result
    #: that has already happened in the world could be recorded. Validated by
    #: Pydantic at the boundary, which is where the module docstring's rule
    #: about open-ended vocabularies puts that job.
    agent_status: Mapped[str | None] = mapped_column(String(20))
    #: Why the agent ended where it did, in the words it wrote for a person.
    #: Ours, not hh's: "letter field not found on the page" is this program
    #: explaining itself.
    agent_reason: Mapped[str | None] = mapped_column(Text)

    # ── what this project believed when it sent ───────────────────────
    #: The match score as of the send, on the project's 0-100 scale. Copied
    #: out of ``match`` because that row is rewritten by the next scoring run,
    #: and correlating outcomes against a score that has since moved measures
    #: nothing. NULL means no match row existed for the active profile, not a
    #: score of zero.
    match_score: Mapped[Decimal | None] = mapped_column(Score)
    #: The bucket that went with it. A column of its own rather than a key
    #: inside :attr:`match_explanation` because it is the natural GROUP BY for
    #: "did the strong ones answer more often than the stretches".
    match_bucket: Mapped[MatchBucket | None] = mapped_column(pg_enum(MatchBucket, "match_bucket"))
    #: The whole explanation behind that score, shaped as
    #: ``app.schemas.agent.MatchExplanation``: matched skills, what was
    #: missing, red flags, the verdict sentence. JSONB because nothing filters
    #: on it — it is read whole, by a person or by a renderer. The Russian
    #: one-liner the confirmation card printed is not stored beside it; it is
    #: ``agent_queue.render_explanation`` of this value, and keeping one source
    #: is what stops the sentence and the structure drifting apart.
    match_explanation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: The requirements the posting listed at send time, canonical names, in
    #: the order the vacancy gave them. The posting is re-crawled and hh's
    #: ``keySkills`` change; this is what the employer was asking for on the
    #: day, which is the half of the feedback loop that says which missing
    #: skill actually costs an answer. NULL means not recorded; ``[]`` means
    #: recorded, and the posting named none.
    vacancy_key_skills: Mapped[list[str] | None] = mapped_column(JSONB)

    # ── hh's own words and hh's own numbers ───────────────────────────
    #: The soft line hh shows beside the response form — «Такой отклик может
    #: получить отказ», followed by the requirement it names. It is hh's
    #: analysis of this application against this vacancy, and it names one
    #: unmet requirement, which is more specific than any similarity this
    #: project computes. Text, verbatim, never parsed into a verdict of ours:
    #: a vacancy page is somebody else's site. NULL means hh did not show it.
    hh_warning: Mapped[str | None] = mapped_column(Text)
    #: hh's other line, historically the one that stopped a send — the
    #: resume-visibility demand. Since 2026-09-07 neither line blocks: hh
    #: accepts those applications, measured, and ``agent/state_page.py``
    #: carries the record. The two are still separate columns because they say
    #: different things: this one is about the account and is therefore true of
    #: every application sent while that setting stands, while
    #: :attr:`hh_warning` is about this vacancy alone. Merging them would lose
    #: exactly the distinction that makes either worth reading.
    hh_blocking_warning: Mapped[str | None] = mapped_column(Text)
    #: ``negotiations.total`` read off the page after the send: hh's own count
    #: of applications on this vacancy, and therefore the measured answer to
    #: "did this send actually happen once". NULL means unmeasured; ``0`` is
    #: hh saying there are none, and the two must not be added together.
    hh_negotiations_total: Mapped[int | None] = mapped_column(Integer)

    # ── the outcome, which arrives later and separately ───────────────
    #: ``topicList[].lastState`` as hh last reported it — ``DISCARD`` was
    #: observed live. The outcome of the application, in hh's vocabulary
    #: rather than a verdict of ours: that set is open and hh's, so
    #: enumerating it here would turn hh adding a state into a failure to
    #: record something that already happened. An outcome learned some other
    #: way — a phone call, an email — is not this column; it is
    #: :attr:`notes`, and it belongs to the person who heard it.
    hh_last_state: Mapped[str | None] = mapped_column(String(100))
    #: When that state was read off hh. Set together with it, so the pair is
    #: always "this outcome, seen on this date" and a dashboard can measure
    #: time-to-answer from :attr:`sent_at` without guessing.
    hh_last_state_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    vacancy: Mapped[Vacancy] = relationship(back_populates="applications")


class GeneratedDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One CV or one cover letter, written for one vacancy, kept for ever.

    **A regeneration adds a row; it never edits one.** That is the whole reason
    this table exists rather than two more columns on ``application``. The owner
    edits the rules a document is written under, regenerates, and has to be able
    to see what that changed — which is impossible if the previous answer was
    overwritten by the new one. So ``version`` counts up per
    ``(profile, vacancy, kind)`` and every version stays readable.

    **The file is not stored; the arrangement is.** ``payload`` holds the plan
    the generator produced — which experience entries in which order, which
    skills under which heading, the summary — and rendering it is deterministic,
    so the .docx can be rebuilt byte for byte from this row plus the profile it
    names. Keeping the bytes instead would double the storage and, worse, would
    let a stored file drift from the profile it claims to describe with nothing
    able to detect it. ``text`` is the same document as plain text: it is what
    the audit read, so it is what has to be kept to explain the audit's answer.

    **``rules_version`` is the point of comparison.** A document is written
    under a set of hard rules, and the answer to "why is this version different"
    is usually "because the rules changed". Storing the identity of the rule set
    in force makes that answerable from the row rather than from memory.

    Nothing here is sent anywhere. A document is generated, audited, stored and
    handed to the person; sending an application is ``agent/``'s, from a
    browser, under the user's own account, after a human has confirmed it.
    """

    __tablename__ = "generated_document"
    __table_args__ = (UniqueConstraint("profile_id", "vacancy_id", "kind", "version"),)

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vacancy_id: Mapped[UUID] = mapped_column(
        ForeignKey("vacancy.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[DocumentKind] = mapped_column(
        pg_enum(DocumentKind, "document_kind"),
        nullable=False,
    )
    #: 1 for the first document of this kind for this pair, then up. Assigned by
    #: the store under the unique constraint above, so two concurrent
    #: regenerations cannot both claim the same number.
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The generator's plan: orderings, selections, the summary. Rendering is a
    #: pure function of this and the profile.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    #: The same document as plain text — what the audit below actually read.
    text: Mapped[str] = mapped_column(Text, nullable=False)
    #: The file format the person downloads. See ``app.documents.render``.
    file_format: Mapped[str] = mapped_column(String(10), nullable=False)
    #: The audit of this document, as ``app.schemas.ats.ATSReport``. Not
    #: nullable: a document that could not be audited is not handed over, so a
    #: stored row always has the report that let it through.
    ats_report: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: Which rule set the document was written under, so two versions can be
    #: compared knowing whether the rules moved between them.
    rules_version: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Whether a model arranged this document or the rule-based fallback did.
    source: Mapped[DocumentSource] = mapped_column(
        pg_enum(DocumentSource, "document_source"),
        nullable=False,
    )
    #: What the checks caught on the way to this version, in order. Empty on a
    #: clean first answer, and kept even when a later attempt succeeded: "the
    #: model tried to add Kubernetes" is worth being able to read afterwards.
    problems: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)

    profile: Mapped[CandidateProfile] = relationship()
    vacancy: Mapped[Vacancy] = relationship(back_populates="documents")


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


class ReferenceDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A document the owner keeps as an example of how theirs should look.

    Two kinds, never mixed: an exemplary CV and an exemplary cover letter. What
    is stored is the extracted plain text, not the file — the file is read once,
    by the same extractor an uploaded resume goes through, and then discarded.
    Keeping the bytes would mean keeping somebody else's document indefinitely
    for no gain: nothing downstream can use anything but the text.

    **The text is untrusted input.** It is a document this project did not
    write, usually somebody else's, and it reaches the model quoted, fenced and
    labelled as data — see ``app/workshop/prompt.py``. The rule the reference
    exists to serve is a rule about *form*: structure, length, register, the
    order things are said in. Nothing factual may cross from it, and the prompt
    says so in as many words.
    """

    __tablename__ = "reference_document"

    kind: Mapped[ReferenceKind] = mapped_column(
        pg_enum(ReferenceKind, "reference_kind"), nullable=False, index=True
    )
    #: What the owner calls it in their own list.
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    #: "Чем именно хорош" — why this one is worth imitating. For the person, and
    #: for the prompt: "the opening names the product, not the company" is the
    #: sort of note that tells the model what to take from it.
    note: Mapped[str | None] = mapped_column(Text)
    #: The extracted plain text. The whole of what is kept.
    text: Mapped[str] = mapped_column(Text, nullable=False)
    #: Off means "keep it, do not show it". Deleting is also available; this is
    #: for trying a reference out and putting it back.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    #: Provenance of the upload, so a list of five references is legible. NULL
    #: for a reference pasted as text, which is a real and ordinary case.
    source_filename: Mapped[str | None] = mapped_column(String(255))
    source_format: Mapped[str | None] = mapped_column(String(10))
    size_bytes: Mapped[int | None] = mapped_column(Integer)


class GenerationRule(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One checkable requirement the owner placed on their own documents.

    The shape is deliberately narrow: a closed vocabulary of kinds
    (:class:`app.db.enums.RuleKind`), a JSONB blob of that kind's parameters,
    and a sentence for a human. What it is *not* is a free-text instruction, and
    that is the load-bearing decision in this table.

    A rule expressed as prose can only be asked of the model. Asking is not
    checking, and a rule that is only asked is a rule that is silently broken.
    Every kind here is instead something a function decides by reading the
    finished document, which is what makes ``severity = hard`` mean anything.

    The second consequence is a boundary rather than a mechanism. Because a rule
    is structure and not prose, the only way it can put a *claim* into a
    document is by naming one — a required keyword, a required section heading —
    and that is a small enough surface to check at the moment the rule is saved.
    ``app/workshop/truth.py`` does the checking, and refuses a rule that would
    make the system assert something the profile does not support.

    ``params`` is JSONB because it is a different shape per kind, read whole and
    never queried by field; ``app.workshop.rules.RuleParams`` is the contract,
    validated on the way in and on the way out.
    """

    __tablename__ = "generation_rule"

    kind: Mapped[RuleKind] = mapped_column(pg_enum(RuleKind, "rule_kind"), nullable=False)
    scope: Mapped[RuleScope] = mapped_column(
        pg_enum(RuleScope, "rule_scope"), nullable=False, index=True
    )
    severity: Mapped[RuleSeverity] = mapped_column(
        pg_enum(RuleSeverity, "rule_severity"),
        default=RuleSeverity.HARD,
        nullable=False,
    )
    #: This kind's parameters, as ``app.workshop.rules.RuleParams`` dumps them.
    #: Carries ``kind`` itself, so a row is self-describing and a mismatch
    #: between the column and the payload fails validation rather than being
    #: resolved by whichever the reader happened to trust.
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    #: What a person is shown when this rule is broken. Russian, theirs, and
    #: **never sent to the model** — the block the model is shown is rendered
    #: from ``params`` by code, so a sentence typed here cannot become an
    #: instruction. See ``app/workshop/prompt.py``.
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
