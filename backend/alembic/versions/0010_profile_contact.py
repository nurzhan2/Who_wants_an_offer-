"""Give the profile a contact block that survives re-parsing the resume.

The contact block of a CV — name, phone, email, city and a handful of links —
is the one part that does not change per vacancy, and until now it existed only
inside ``candidate_profile.raw_text``. Text is not a field: it cannot be
corrected when the parser misreads a phone number, and it cannot be substituted
into a document generated for the owner.

Two tables rather than columns on ``candidate_profile``; the reasoning is in
the docstring of :class:`app.db.models.ProfileContact` and comes down to
keeping personal data off the row that the matcher, the embedder and every
dashboard response already carry.

``profile_contact_link`` is rows rather than columns because the set of places
a person keeps a profile is open and changes faster than migrations should.
``kind`` is a plain string, not an enum: adding a member to a PostgreSQL enum
is ``ALTER TYPE``, which this schema avoids everywhere for the same reason.

The ``*_edited`` flags are the reason the whole thing is safe to prefill. They
say which values a human has settled, and prefill writes only where they are
false. Defaulting them to false is right for every row this migration creates,
because there are none: no contact has ever been edited by hand at the moment
it runs.

Nothing is backfilled. Contacts could in principle be re-extracted from the
resume text still stored on old profiles, but a migration is the wrong place
for it: the extraction is a heuristic that will be tuned, and a backfill would
freeze whatever version of it shipped today into rows nobody can tell apart
from hand-checked ones. Uploading a resume fills the block in; that path runs
the current code and records where each value came from.

Revision ID: 0010_profile_contact
Revises: 0009_application_profile
Create Date: 2026-09-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_profile_contact"
down_revision: str | None = "0009_application_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the contact block and its links."""
    op.create_table(
        "profile_contact",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("profile_id", sa.UUID(), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=True),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("full_name_edited", sa.Boolean(), nullable=False),
        sa.Column("phone_edited", sa.Boolean(), nullable=False),
        sa.Column("email_edited", sa.Boolean(), nullable=False),
        sa.Column("city_edited", sa.Boolean(), nullable=False),
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
            name=op.f("fk_profile_contact_profile_id_candidate_profile"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profile_contact")),
        # One block per profile. A second row would silently split the same
        # person's contacts in two, and whichever one a query happened to read
        # would look complete.
        sa.UniqueConstraint("profile_id", name=op.f("uq_profile_contact_profile_id")),
    )
    op.create_index(
        op.f("ix_profile_contact_profile_id"), "profile_contact", ["profile_id"], unique=False
    )

    op.create_table(
        "profile_contact_link",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("label", sa.String(length=60), nullable=True),
        sa.Column("is_manual", sa.Boolean(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
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
            ["contact_id"],
            ["profile_contact.id"],
            name=op.f("fk_profile_contact_link_contact_id_profile_contact"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profile_contact_link")),
        # The same address twice is never meaningful, and deduplicating in the
        # database is what lets prefill re-add extracted links without checking
        # first whether the owner has already typed one of them.
        sa.UniqueConstraint(
            "contact_id", "url", name=op.f("uq_profile_contact_link_contact_id_url")
        ),
    )
    op.create_index(
        op.f("ix_profile_contact_link_contact_id"),
        "profile_contact_link",
        ["contact_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop both tables.

    Links go first: the FK is ``ON DELETE CASCADE`` at the row level, which
    says nothing about the order the tables themselves may be dropped in.
    """
    op.drop_index(op.f("ix_profile_contact_link_contact_id"), table_name="profile_contact_link")
    op.drop_table("profile_contact_link")
    op.drop_index(op.f("ix_profile_contact_profile_id"), table_name="profile_contact")
    op.drop_table("profile_contact")
