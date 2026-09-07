"""Record which profile an application was written from.

Revision ID: 0009_application_profile
Revises: 0008_application_send_record
Create Date: 2026-09-08

``application`` records a vacancy and a status; it has never recorded whose
resume the letter was written from. That was harmless while nothing read a past
letter. Migration ``0008`` made letters into evidence — ``sent_letter`` is fed
back as a few-shot example of what got an answer — and at that moment the missing
link became a correctness problem rather than an untidiness.

``app/letters/store.py`` reached the profile through the ``match`` row, and said
in its own docstring that the join was "a proxy" and that "the honest fix is an
``application.profile_id`` column". The proxy does not hold: a ``match`` row
exists for every profile against every vacancy in the shared pool, so as soon as
the current profile is scored against a vacancy some earlier profile applied to,
that earlier profile's sent letter is served as an example of what to claim. The
model is then shown a letter written from a different resume and asked to write
like it, which is the shortest path to a letter claiming experience its candidate
does not have — the one thing the whole feature is forbidden to do.

It is fixed now rather than later because it is free now: there is exactly one
profile and zero rows with a ``sent_letter``, so the backfill has nothing to get
wrong. With two profiles and a year of applications it would be a guess.

The column is nullable and stays nullable. NULL means "written before this was
recorded, or typed into the tracker by hand" — both real, neither a profile — and
the reader treats NULL as not-this-profile, which is the conservative direction:
a letter whose provenance is unknown is not evidence about any resume.

``ondelete="SET NULL"``: deleting a resume must not delete the record that an
application was sent. The application happened; only the link to the resume that
produced it is lost.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_application_profile"
down_revision = "0008_application_send_record"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the column and point every existing row at the only profile there is.

    The backfill is safe *only* because there is one profile. It is written as a
    query rather than a constant so that it does nothing at all — rather than
    something wrong — on a database where that is not true: a database with two
    profiles cannot know which one wrote a letter from 2026, and inventing an
    answer there would be exactly the false provenance this column exists to
    prevent. Such a database gets NULLs, and NULLs are read as "not evidence".
    """
    op.add_column(
        "application",
        sa.Column("profile_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_application_profile_id_candidate_profile",
        "application",
        "candidate_profile",
        ["profile_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_application_profile_id", "application", ["profile_id"])

    op.execute(
        sa.text(
            """
            UPDATE application
               SET profile_id = (SELECT id FROM candidate_profile)
             WHERE (SELECT count(*) FROM candidate_profile) = 1
            """
        )
    )


def downgrade() -> None:
    """Drop it. Nothing else read it, so nothing else has to be undone."""
    op.drop_index("ix_application_profile_id", table_name="application")
    op.drop_constraint(
        "fk_application_profile_id_candidate_profile", "application", type_="foreignkey"
    )
    op.drop_column("application", "profile_id")
