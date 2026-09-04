"""Columns the crawler needs, and the credit ledger it cannot do without.

Three groups of change, landed together so phase 4 does not cost a second
migration over the same table.

**Fingerprint versioning.** ``vacancy.fingerprint`` is sha1 over a normalised
company, title and city, and phase 4 will normalise those better — company
forms, quotes, transliteration, city aliases. That moves the key, and postings
that are distinct rows today will collide once it moves. Recording which
algorithm produced each row is what lets that recompute merge them deliberately
instead of failing on the unique constraint.

**Fields phase 4 fills, created now.** ``work_authorization`` and ``is_spam``
are empty until normalisation lands, but the columns are cheap and a second
ALTER over the same table is not. ``work_authorization`` earns its place from
measurement rather than theory: in a sample of ten global remote postings, five
required existing US work rights and excluded sponsorship, while carrying
``remote = true`` and a location of "Anywhere". Every hard filter in the system
would have passed them, and the stack matched perfectly, so they would have
scored near the top of a list for a candidate who cannot apply to any of them.

**The credit ledger.** ``source_quota`` exists because nothing else can answer
"how many metered requests has this source spent today". ``pipeline_run``
counts runs, and a metered API charges per page: a run that died halfway
through pagination spent real credits and left no record, so a number derived
from run history undercounts exactly when the limit is about to be hit.

Revision ID: 0005_source_framework
Revises: 0004_ats_report
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_source_framework"
down_revision: str | None = "0004_ats_report"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Declared best-first, and the order is load-bearing rather than cosmetic:
#: PostgreSQL orders an enum by declaration, so ``LEAST(completeness,
#: excluded.completeness)`` in the upsert means "the more complete of the two".
#: Reordering these values would silently let a headline-only source overwrite
#: a posting we already hold in full.
COMPLETENESS_VALUES: tuple[str, ...] = ("full", "snippet", "stub")


def upgrade() -> None:
    """Add the crawler columns and the per-day credit ledger."""
    values = ", ".join(f"'{value}'" for value in COMPLETENESS_VALUES)
    op.execute(f"CREATE TYPE vacancy_completeness AS ENUM ({values})")
    completeness = postgresql.ENUM(
        *COMPLETENESS_VALUES, name="vacancy_completeness", create_type=False
    )

    op.add_column(
        "vacancy",
        sa.Column("fingerprint_version", sa.SmallInteger(), nullable=False, server_default="1"),
    )
    op.add_column(
        "vacancy",
        sa.Column("completeness", completeness, nullable=False, server_default="full"),
    )
    # Dropped immediately, as in 0003: the ORM always supplies the value, no
    # other enum column in this schema keeps a server default, and leaving one
    # behind makes `alembic check` report a permanent drift against the model.
    # The default exists only to fill the rows that are already there.
    op.alter_column("vacancy", "completeness", server_default=None)

    op.add_column(
        "vacancy",
        sa.Column("work_authorization", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "vacancy",
        sa.Column("is_spam", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "vacancy",
        sa.Column("spam_signals", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("vacancy", sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("vacancy", sa.Column("embedding_text_hash", sa.CHAR(length=64), nullable=True))
    op.add_column("vacancy", sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "source_quota",
        sa.Column("source_slug", sa.String(length=50), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("source_slug", "day", name=op.f("pk_source_quota")),
    )

    # Partial index over the rows the embedding step actually looks for. The
    # alternative is a sequential scan of the whole vacancy table on every run,
    # which grows with the corpus while the answer stays small.
    op.execute(
        "CREATE INDEX ix_pg_vacancy_needs_embedding ON vacancy (last_seen_at DESC) "
        "WHERE embedding IS NULL OR embedded_at IS NULL"
    )


def downgrade() -> None:
    """Drop them again. The enum type goes with its only column."""
    op.execute("DROP INDEX IF EXISTS ix_pg_vacancy_needs_embedding")
    op.drop_table("source_quota")
    for column in (
        "embedded_at",
        "embedding_text_hash",
        "enriched_at",
        "spam_signals",
        "is_spam",
        "work_authorization",
        "completeness",
        "fingerprint_version",
    ):
        op.drop_column("vacancy", column)
    op.execute("DROP TYPE IF EXISTS vacancy_completeness")
