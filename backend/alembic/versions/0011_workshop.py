"""The workshop: reference documents to imitate, and rules to obey.

Revision ID: 0011_workshop
Revises: 0010_profile_contact
Create Date: 2026-09-08

Two tables and four enum types, for the two levers the owner has over how their
documents are written without touching code:

``reference_document``
    an exemplary CV or cover letter, kept as extracted plain text. The file
    itself is not stored: it is read once, through the same extractor an
    uploaded resume goes through, and discarded. Nothing downstream can use
    anything but the text, and keeping somebody else's document indefinitely for
    no gain is a cost with no benefit attached to it.

``generation_rule``
    one checkable requirement — a count, a presence, an absence, a length —
    with a scope, a severity and a sentence for a person. Not free prose: a rule
    expressed as prose can only be asked of the model, and asking is not
    checking. ``params`` is JSONB because the shape differs per kind, is read
    whole, and is never queried by field; ``app.workshop.rules.RuleParams``
    validates it in both directions.

The two constraints that already existed — no links and no email addresses in a
cover letter, which is hh's spam filter and not a preference — are **not** rows
here. They are built into ``app.workshop.rules`` as undeletable entries whose
checking is ``app.letters.guard``'s, called rather than copied. Seeding them as
rows would make them deletable by anything holding a DELETE, and would put a
second copy of the definition somewhere it could drift from the first.

Both tables are new and empty, so the downgrade is a plain drop with nothing to
preserve. The enum types are created and dropped explicitly, per this project's
rule about ``ALTER TYPE`` and Alembic autogenerate; the member lists are frozen
copies rather than imports from ``app.db.enums``, because a migration must keep
applying the same way after the application's enums change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_workshop"
down_revision: str | None = "0010_profile_contact"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: name -> members, in creation order.
ENUM_TYPES: dict[str, tuple[str, ...]] = {
    "reference_kind": ("cv", "cover_letter"),
    "rule_scope": ("cv", "cover_letter", "both"),
    "rule_severity": ("hard", "soft"),
    "rule_kind": (
        "section_item_count",
        "required_section",
        "required_keyword",
        "date_format",
        "forbidden_phrase",
        "length",
        "no_links",
        "no_contact_handles",
    ),
}


def _enum(name: str) -> postgresql.ENUM:
    """Reference an already-created type; never create one implicitly."""
    return postgresql.ENUM(*ENUM_TYPES[name], name=name, create_type=False)


def upgrade() -> None:
    """Create the enum types, then the two tables."""
    for name, members in ENUM_TYPES.items():
        values = ", ".join(f"'{member}'" for member in members)
        op.execute(f"CREATE TYPE {name} AS ENUM ({values})")

    op.create_table(
        "reference_document",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kind", _enum("reference_kind"), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source_filename", sa.String(length=255), nullable=True),
        sa.Column("source_format", sa.String(length=10), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reference_document"),
    )
    op.create_index("ix_reference_document_kind", "reference_document", ["kind"])

    op.create_table(
        "generation_rule",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kind", _enum("rule_kind"), nullable=False),
        sa.Column("scope", _enum("rule_scope"), nullable=False),
        sa.Column(
            "severity", _enum("rule_severity"), nullable=False, server_default=sa.text("'hard'")
        ),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_generation_rule"),
    )
    op.create_index("ix_generation_rule_scope", "generation_rule", ["scope"])


def downgrade() -> None:
    """Drop both tables and the four types. Nothing else read them."""
    op.drop_index("ix_generation_rule_scope", table_name="generation_rule")
    op.drop_table("generation_rule")
    op.drop_index("ix_reference_document_kind", table_name="reference_document")
    op.drop_table("reference_document")
    for name in reversed(list(ENUM_TYPES)):
        op.execute(f"DROP TYPE {name}")
