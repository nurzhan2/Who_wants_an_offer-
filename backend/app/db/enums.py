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


class Seniority(StrEnum):
    """Grade, as advertised by the vacancy or inferred from the resume."""

    JUNIOR = "junior"
    MIDDLE = "middle"
    SENIOR = "senior"
    LEAD = "lead"


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


class PipelineRunStatus(StrEnum):
    """Outcome of one source run inside a pipeline execution."""

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


#: Type name in PostgreSQL for every enum above, in creation order.
ENUM_TYPE_NAMES: dict[str, type[StrEnum]] = {
    "skill_level": SkillLevel,
    "seniority": Seniority,
    "remote_type": RemoteType,
    "employment_type": EmploymentType,
    "salary_period": SalaryPeriod,
    "match_bucket": MatchBucket,
    "application_status": ApplicationStatus,
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
