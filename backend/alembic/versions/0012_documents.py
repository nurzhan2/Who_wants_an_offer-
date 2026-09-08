"""Keep the resume's jobs, and every document generated from them.

Two tables and one column, added together because each is useless without
the others.

``profile_experience`` stores what the extraction has always produced and this
schema has always thrown away. ``ProfileExtraction.work_periods`` — company,
title, dates, the stack attributed to each job — was read out of every resume,
used to compute ``total_years`` and per-skill years, and then dropped. That cost
nothing while the only things built from a profile were a number and a cover
letter. A tailored CV *is* the list of jobs, so generating one from a schema
that does not hold them would mean asking a model to remember them from
``raw_text``, which is precisely how a generated CV acquires a job the candidate
never had.

``candidate_profile.education`` closes the same gap for degrees, and is a JSONB
column rather than a table for the same reason ``languages`` next to it is: a
short list of flat records, read whole, never queried by field. It is not
cosmetic — this project's own ATS audit raises ``MISSING_SECTIONS`` when a
resume has no education heading, so a CV generated without the data would be
marked down by the auditor in the same repository, and correctly.

``generated_document`` stores the result, versioned. The brief is explicit that
regeneration must not overwrite: the owner edits the rules, regenerates, and has
to see what changed. So ``version`` counts up per ``(profile, vacancy, kind)``
under a unique constraint, and nothing in the application updates a row that
already exists.

Neither table is backfilled, and the difference between them is worth stating.
``generated_document`` has nothing to backfill — no document existed before this
migration. ``profile_experience`` does have a source, and it is deliberately not
used: the jobs could be re-read out of ``candidate_profile.raw_text``, but only
by running the extraction again, which is an LLM call per profile inside a
migration. A profile parsed before this migration therefore has no experience
rows until it is re-parsed, and ``app/documents`` reports that as a reason it
cannot write a CV rather than writing one with the experience section missing.

Revision ID: 0012_documents
Revises: 0011_workshop
Create Date: 2026-09-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_documents"
down_revision: str | None = "0011_workshop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Both enums are created here and dropped on downgrade. Autogenerate does
#: neither for native enum types — see ``app/db/enums.py`` — so the columns
#: below declare ``create_type=False`` and this does it explicitly.
DOCUMENT_KIND = postgresql.ENUM("cv", "cover_letter", name="document_kind", create_type=False)
DOCUMENT_SOURCE = postgresql.ENUM("model", "fallback", name="document_source", create_type=False)


def upgrade() -> None:
    """Create both tables, the education column, and the two enum types."""
    DOCUMENT_KIND.create(op.get_bind(), checkfirst=True)
    DOCUMENT_SOURCE.create(op.get_bind(), checkfirst=True)

    # Filled in with an empty list rather than left NULL: "the resume named no
    # degrees" and "this profile predates the column" are both "nothing to
    # render", and giving them one representation keeps every reader from
    # having to decide which it is holding. The default is dropped immediately
    # afterwards so the model's Python-side default is the only one, matching
    # every other JSONB column in this schema (see migration 0002).
    op.add_column(
        "candidate_profile",
        sa.Column(
            "education",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.alter_column("candidate_profile", "education", server_default=None)

    op.create_table(
        "profile_experience",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("company", sa.String(length=300), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        # "YYYY-MM" as text, not a Date: a resume that names only a year does
        # not state a month, and a Date column would have to invent one.
        sa.Column("start", sa.String(length=7), nullable=True),
        sa.Column("end", sa.String(length=7), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("stack", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("domains", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["candidate_profile.id"],
            name="fk_profile_experience_profile_id_candidate_profile",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_profile_experience"),
        sa.UniqueConstraint(
            "profile_id", "position", name="uq_profile_experience_profile_id_position"
        ),
    )
    op.create_index(
        "ix_profile_experience_profile_id", "profile_experience", ["profile_id"], unique=False
    )

    op.create_table(
        "generated_document",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vacancy_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", DOCUMENT_KIND, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("file_format", sa.String(length=10), nullable=False),
        sa.Column("ats_report", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rules_version", sa.String(length=64), nullable=False),
        sa.Column("source", DOCUMENT_SOURCE, nullable=False),
        sa.Column("problems", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["candidate_profile.id"],
            name="fk_generated_document_profile_id_candidate_profile",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["vacancy_id"],
            ["vacancy.id"],
            name="fk_generated_document_vacancy_id_vacancy",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_generated_document"),
        # What makes "a regeneration is a new version" a property of the
        # database rather than of the code that happens to write it.
        sa.UniqueConstraint(
            "profile_id",
            "vacancy_id",
            "kind",
            "version",
            name="uq_generated_document_profile_id_vacancy_id_kind_version",
        ),
    )
    op.create_index(
        "ix_generated_document_profile_id", "generated_document", ["profile_id"], unique=False
    )
    op.create_index(
        "ix_generated_document_vacancy_id", "generated_document", ["vacancy_id"], unique=False
    )


def downgrade() -> None:
    """Drop both tables and both enum types.

    Losing ``generated_document`` loses the version history, which is the one
    thing in here that cannot be recomputed — every document can be generated
    again, but what an earlier rule set produced cannot. Stated rather than
    guarded against: a downgrade past this revision is a decision to discard it.
    """
    op.drop_index("ix_generated_document_vacancy_id", table_name="generated_document")
    op.drop_index("ix_generated_document_profile_id", table_name="generated_document")
    op.drop_table("generated_document")

    op.drop_index("ix_profile_experience_profile_id", table_name="profile_experience")
    op.drop_table("profile_experience")

    op.drop_column("candidate_profile", "education")

    DOCUMENT_SOURCE.drop(op.get_bind(), checkfirst=True)
    DOCUMENT_KIND.drop(op.get_bind(), checkfirst=True)
