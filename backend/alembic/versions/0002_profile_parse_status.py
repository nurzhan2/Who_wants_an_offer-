"""Resume parse status, uploaded-file metadata, and skill raw-name lists.

Three changes, one migration:

* ``parse_status`` / ``parse_error`` / ``parse_started_at`` — extraction runs in
  a background task, so the upload endpoint answers before it finishes and the
  client polls this.
* ``resume_filename`` / ``resume_size_bytes`` / ``resume_format`` — the
  dashboard has to be able to say which resume the live profile came from.
* ``profile_skill.raw_name`` becomes ``raw_names``, a JSONB list.
  Canonicalisation collapses variants ("Python" and "Python 3" both become
  ``python``), and one column cannot hold both originals. Existing values are
  migrated into single-element lists rather than dropped.

The new enum type is created and dropped explicitly, for the same reason as in
0001: Alembic autogenerate does neither, and the omission only shows up on a
clean database.

Revision ID: 0002_parse_status
Revises: 0001_initial
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_parse_status"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PARSE_STATUS_VALUES: tuple[str, ...] = ("pending", "ready", "failed")


def upgrade() -> None:
    """Add the parse-status columns, the resume metadata and raw_names."""
    values = ", ".join(f"'{value}'" for value in PARSE_STATUS_VALUES)
    op.execute(f"CREATE TYPE parse_status AS ENUM ({values})")

    parse_status = postgresql.ENUM(*PARSE_STATUS_VALUES, name="parse_status", create_type=False)

    # Rows that already exist were created before background parsing, so they
    # are complete by definition. Defaulting them to 'pending' would strand them
    # behind the staleness timeout.
    #
    # The server default is then dropped rather than changed to 'pending'. It
    # exists only to backfill this one ALTER: the ORM always sets the column
    # explicitly, no other enum column in this schema carries one, and leaving
    # it behind makes `alembic check` report a permanent drift against the model.
    op.add_column(
        "candidate_profile",
        sa.Column("parse_status", parse_status, nullable=False, server_default="ready"),
    )
    op.alter_column("candidate_profile", "parse_status", server_default=None)

    op.add_column("candidate_profile", sa.Column("parse_error", sa.Text(), nullable=True))
    op.add_column(
        "candidate_profile",
        sa.Column("parse_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "candidate_profile", sa.Column("resume_filename", sa.String(length=255), nullable=True)
    )
    op.add_column("candidate_profile", sa.Column("resume_size_bytes", sa.Integer(), nullable=True))
    op.add_column(
        "candidate_profile", sa.Column("resume_format", sa.String(length=10), nullable=True)
    )

    # Only unparsed profiles are worth polling; the dashboard filters on this.
    op.execute(
        "CREATE INDEX ix_pg_candidate_profile_parse_pending ON candidate_profile "
        "(parse_started_at) WHERE parse_status = 'pending'"
    )

    op.add_column(
        "profile_skill",
        sa.Column(
            "raw_names",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    # Carry the old single value over instead of dropping it.
    op.execute(
        "UPDATE profile_skill SET raw_names = jsonb_build_array(raw_name) "
        "WHERE raw_name IS NOT NULL"
    )
    op.drop_column("profile_skill", "raw_name")
    op.alter_column("profile_skill", "raw_names", server_default=None)


def downgrade() -> None:
    """Reverse everything, including the enum type."""
    op.add_column("profile_skill", sa.Column("raw_name", sa.String(length=200), nullable=True))
    # Keep the first spelling; the rest cannot fit in a scalar column.
    op.execute(
        "UPDATE profile_skill SET raw_name = raw_names ->> 0 "
        "WHERE jsonb_array_length(raw_names) > 0"
    )
    op.drop_column("profile_skill", "raw_names")

    op.execute("DROP INDEX IF EXISTS ix_pg_candidate_profile_parse_pending")
    for column in (
        "resume_format",
        "resume_size_bytes",
        "resume_filename",
        "parse_started_at",
        "parse_error",
        "parse_status",
    ):
        op.drop_column("candidate_profile", column)

    op.execute("DROP TYPE IF EXISTS parse_status")
