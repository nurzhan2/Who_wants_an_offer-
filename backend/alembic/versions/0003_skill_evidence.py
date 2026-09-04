"""Separate how well a skill is known from how well that is evidenced.

``profile_skill.level`` was carrying both meanings. A skill named only in a
resume's sidebar, with no per-job technology list to date it, was stored as
``basic`` — which the coverage score multiplies by 0.7. Plenty of strong
candidates simply do not write a stack per job, so that was a 30% penalty for a
formatting habit rather than a fact about the person.

``level`` now answers "how well", defaulting to ``working`` when nothing dates
the skill, and ``evidence`` answers "how do we know". Whether ``stated`` should
be discounted at all is a scoring decision for phase 5, made on labelled data.

Existing rows are backfilled from what is already known: a skill with computed
years was tied to dated jobs, so it is ``corroborated``; one without is
``stated``, and its ``basic`` level is lifted to ``working`` because that level
was the artefact this migration exists to remove.

Revision ID: 0003_skill_evidence
Revises: 0002_parse_status
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_skill_evidence"
down_revision: str | None = "0002_parse_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVIDENCE_VALUES: tuple[str, ...] = ("corroborated", "stated")


def upgrade() -> None:
    """Add the evidence column and backfill it from the existing rows."""
    values = ", ".join(f"'{value}'" for value in EVIDENCE_VALUES)
    op.execute(f"CREATE TYPE skill_evidence AS ENUM ({values})")

    evidence = postgresql.ENUM(*EVIDENCE_VALUES, name="skill_evidence", create_type=False)
    op.add_column(
        "profile_skill",
        sa.Column("evidence", evidence, nullable=False, server_default="stated"),
    )
    # Dropped rather than kept: the ORM always sets the column, no other enum
    # column in this schema carries a server default, and leaving one behind
    # makes `alembic check` report a permanent drift against the model.
    op.alter_column("profile_skill", "evidence", server_default=None)

    op.execute("UPDATE profile_skill SET evidence = 'corroborated' WHERE years IS NOT NULL")
    # The level these rows carry is the artefact, not a measurement.
    op.execute("UPDATE profile_skill SET level = 'working' WHERE years IS NULL AND level = 'basic'")


def downgrade() -> None:
    """Drop the column and the type. The lifted levels are not put back."""
    op.drop_column("profile_skill", "evidence")
    op.execute("DROP TYPE IF EXISTS skill_evidence")
