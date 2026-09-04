"""Store the ATS readability report on the profile.

The audit reads a PDF's word geometry to decide whether an employer's applicant
tracking system will parse the file or mangle it. That is pure CPU work on the
uploaded bytes, and the bytes are deleted once parsing finishes — so the answer
has to be computed at upload and kept, not recomputed on request.

JSONB rather than columns: the finding list is a nested, evolving shape that is
read whole and never queried by field. ``app.schemas.ats.ATSReport`` is the
contract; a change there must keep validating what is already stored.

Nullable on purpose. Profiles uploaded before this migration have no report,
and an audit that fails must not fail the upload — both cases are "no report",
which is different from "a report saying the file is fine".

Revision ID: 0004_ats_report
Revises: 0003_skill_evidence
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_ats_report"
down_revision: str | None = "0003_skill_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the report column."""
    op.add_column(
        "candidate_profile",
        sa.Column("ats_report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Drop it. The reports are not preserved; they are derived data."""
    op.drop_column("candidate_profile", "ats_report")
