"""Domain enumerations.

These live in their own module because schemas, repositories and services all
import them; pulling in ``models.py`` for an enum would drag the whole ORM
graph along with it.

Every enum is stored as a native PostgreSQL type. Two consequences worth
remembering:

* Alembic autogenerate neither creates nor drops enum types, so the migration
  issues ``CREATE TYPE`` / ``DROP TYPE`` explicitly and the columns declare
  ``create_type=False``.
* Adding a member later needs ``ALTER TYPE ... ADD VALUE``, which is why
  open-ended vocabularies (currency, country, language) are plain strings
  instead.
"""

from enum import StrEnum

from sqlalchemy.dialects.postgresql import ENUM


class SkillLevel(StrEnum):
    """How well the candidate knows a skill; multiplies its coverage weight."""

    BASIC = "basic"
    WORKING = "working"
    STRONG = "strong"
    EXPERT = "expert"


class SkillEvidence(StrEnum):
    """How the claim about a skill is supported.

    Kept apart from ``SkillLevel`` because they answer different questions and
    conflating them punished the wrong thing. A skill named only in a sidebar
    used to be scored ``basic``, cutting its contribution by 30% — but plenty of
    strong candidates never write a per-job technology list, so that was a
    measurement artefact of resume formatting, not a fact about the person.

    ``level`` now says how well; this says how sure. Whether ``stated`` should
    be discounted at all is a scoring decision for phase 5, taken on labelled
    data rather than assumed here.
    """

    #: Tied to dated jobs, so its years are computed rather than assumed.
    CORROBORATED = "corroborated"
    #: Listed, with nothing dating it.
    STATED = "stated"


class Seniority(StrEnum):
    """Grade, as advertised by the vacancy or inferred from the resume."""

    JUNIOR = "junior"
    MIDDLE = "middle"
    SENIOR = "senior"
    LEAD = "lead"


class VacancyCompleteness(StrEnum):
    """How much of the posting we actually hold.

    Not every source gives a description. A subscription-email connector may
    yield a title, a company and a link and nothing else, and scoring such a
    row against a full one would compare a paragraph with an advertisement.
    Matching reads this to decide what it is allowed to conclude.
    """

    #: Description present and complete enough to score semantically.
    FULL = "full"
    #: A teaser: the first lines, or a truncated body.
    SNIPPET = "snippet"
    #: Title, company and link. No body at all.
    STUB = "stub"


class RemoteType(StrEnum):
    """Work format offered by a vacancy or preferred by the candidate."""

    NO = "no"
    HYBRID = "hybrid"
    FULL = "full"


class EmploymentType(StrEnum):
    """Contract shape."""

    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    FREELANCE = "freelance"


class SalaryPeriod(StrEnum):
    """Period the advertised salary refers to."""

    HOUR = "hour"
    DAY = "day"
    MONTH = "month"
    YEAR = "year"


class MatchBucket(StrEnum):
    """Score bucket shown in the dashboard. Ranges live in docs/MATCHING.md."""

    APPLY_NOW = "apply_now"
    STRONG = "strong"
    STRETCH = "stretch"
    SKIP = "skip"
    FILTERED = "filtered"


class ApplicationStatus(StrEnum):
    """Position of an application in the personal kanban."""

    SAVED = "saved"
    APPLIED = "applied"
    SCREENING = "screening"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"


class DocumentKind(StrEnum):
    """Which of the two documents a generated row holds.

    Both are written for one (profile, vacancy) pair and both are versioned the
    same way, so they share a table rather than getting one each. What differs
    is only what is inside them, and that is the row's payload.
    """

    CV = "cv"
    COVER_LETTER = "cover_letter"


class DocumentSource(StrEnum):
    """Where a generated document's arrangement came from.

    Recorded because the two are not the same thing to a person deciding
    whether to send it: ``model`` means a model chose the order and the
    selection, ``fallback`` means the rule-based arrangement did. Neither
    invents facts — that is enforced elsewhere — but one of them was tailored
    to the vacancy and the other was not.
    """

    MODEL = "model"
    FALLBACK = "fallback"


class ParseStatus(StrEnum):
    """Where a resume is in the extraction pipeline.

    Parsing runs in the background, so the API answers before it finishes and
    the client polls this. A ``pending`` row older than the configured timeout
    is reported as ``failed``: background tasks do not survive a restart, and a
    profile stuck in ``pending`` forever is worse than an honest failure.
    """

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class PipelineRunStatus(StrEnum):
    """Outcome of one source run inside a pipeline execution."""

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


#: Type name in PostgreSQL for every enum above, in creation order.
ENUM_TYPE_NAMES: dict[str, type[StrEnum]] = {
    "skill_level": SkillLevel,
    "skill_evidence": SkillEvidence,
    "seniority": Seniority,
    "remote_type": RemoteType,
    "employment_type": EmploymentType,
    "salary_period": SalaryPeriod,
    "match_bucket": MatchBucket,
    "application_status": ApplicationStatus,
    "parse_status": ParseStatus,
    "document_kind": DocumentKind,
    "document_source": DocumentSource,
    "pipeline_run_status": PipelineRunStatus,
}


def pg_enum[E: StrEnum](enum_cls: type[E], name: str) -> ENUM:
    """Build a native PostgreSQL enum column type.

    ``values_callable`` matters: by default SQLAlchemy persists the member
    *name* (``STRONG``), not its value (``strong``). We want the lowercase
    values, because that is what the API and the fixtures speak.
    """
    return ENUM(
        enum_cls,
        name=name,
        create_type=False,
        values_callable=lambda enum: [member.value for member in enum],
    )
