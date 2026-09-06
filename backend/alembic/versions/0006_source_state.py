"""Where a source got to last time, so a corpus larger than one run can be walked.

Every connector before this one fetches a bounded feed: remotive's eighteen
postings, arbeitnow's few hundred, a page of JSearch results. Each run starts at
the beginning, finishes, and needs to remember nothing.

hh is not that. Its sitemap lists roughly fourteen thousand vacancies per city
across ten files, each entry dated with a ``lastmod``, and one run can neither
fetch them all politely nor learn anything from the run before it without
somewhere to write the position down. Nothing already in this schema can hold
it: ``pipeline_run`` records what a run did rather than where inside a source it
stopped, and ``source_quota`` counts requests. Deriving the position from run
timestamps fails in the case that matters — a run that died halfway would move
the mark past pages it never fetched, and those postings would never be
collected at all, silently.

The table is deliberately opaque. The key is a string the connector invents and
the value is JSONB it writes and reads back, so adding a second such source
costs no migration; hh keys one row per sitemap file, because a sitemap file is
what its ``lastmod`` values are grouped by.

Revision ID: 0006_source_state
Revises: 0005_source_framework
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_source_state"
down_revision: str | None = "0005_source_framework"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the per-source crawl-position store."""
    op.create_table(
        "source_state",
        sa.Column("source_slug", sa.String(length=50), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column(
            "value",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("source_slug", "key", name=op.f("pk_source_state")),
    )
    # The ORM always supplies a value, and no other JSONB column here keeps a
    # server default; it exists only so the column can be NOT NULL from the
    # first statement. Dropped for the reason 0005 gives: a default the model
    # does not declare is permanent drift under `alembic check`.
    op.alter_column("source_state", "value", server_default=None)


def downgrade() -> None:
    """Drop it. Losing the positions means the next crawl starts from the top."""
    op.drop_table("source_state")
