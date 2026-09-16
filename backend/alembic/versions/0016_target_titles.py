"""Store the job titles the owner is looking for, apart from the resume.

Revision ID: 0016_target_titles
Revises: 0015_title_embedding
Create Date: 2026-09-16

What the owner searches for used to be implied by the resume: keywords came out
of 66 extracted skills and were matched against ``hh_roles.yaml``. The resume
lists Java, Go, JavaScript and C beside Python, so Go and PHP catalogue pages
entered the crawl and Golang postings topped the list. The only way to narrow it
was to edit the yaml by hand.

``target_titles`` is the answer to "what do I want to be hired as", typed by the
owner. It is a short ordered list read whole, so it is JSONB on the profile for
the same reasons ``locations`` is. NOT NULL with an empty default: an empty list
is the previous behaviour, and every existing profile keeps it.

Reversible: dropping the column returns the planner to skill keywords, which is
what an empty list already does.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0016_target_titles"
down_revision: str | None = "0015_title_embedding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the list, empty for every existing profile."""
    op.add_column(
        "candidate_profile",
        sa.Column(
            "target_titles",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    """Drop the list; an empty one already meant "search by skills"."""
    op.drop_column("candidate_profile", "target_titles")
