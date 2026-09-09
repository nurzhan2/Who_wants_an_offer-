"""Record where each vacancy requirement came from.

Revision ID: 0014_requirement_source
Revises: 0013_letter_rules
Create Date: 2026-09-09

``vacancy_skill`` held three facts about a requirement — its name, whether it is
hard, and what it weighs — and not the fourth: who says it is a requirement at
all. That was invisible while there was only one answer. Every row in the table
came from hh's ``keySkills``, a field the employer filled in themselves.

From this revision requirements are also read out of the description text, which
is where 832 of the corpus's 1958 vacancies keep them because they left the
structured field empty. Those rows are a *reading* of somebody's prose, not a
list somebody typed, and presenting the two as one kind of row would put an
extraction under an employer's name on the vacancy card, in the match
explanation and in the ATS report. The column exists so that they cannot be
confused, and so that a report can count them apart.

Existing rows are backfilled to ``employer_field``, which is not a guess: the
only writer before this revision was ``skill_names`` over
``raw._derived.key_skills``.

Reversible in the full sense. ``downgrade`` drops the column and the type and
loses nothing that cannot be recomputed: the derivation is a pure function of
payloads and descriptions that stay in the database, so
``scripts/backfill_skills.py`` rebuilds every row either way.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_requirement_source"
down_revision: str | None = "0013_letter_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_VALUES: tuple[str, ...] = ("employer_field", "description_text")


def upgrade() -> None:
    """Add the type and the column, defaulting existing rows to the field."""
    values = ", ".join(f"'{value}'" for value in SOURCE_VALUES)
    op.execute(f"CREATE TYPE requirement_source AS ENUM ({values})")

    source = postgresql.ENUM(*SOURCE_VALUES, name="requirement_source", create_type=False)
    op.add_column(
        "vacancy_skill",
        sa.Column("source", source, nullable=False, server_default="employer_field"),
    )
    # Same reasoning as 0003: the ORM always sets the column, and a server
    # default left behind is permanent drift in ``alembic check``. The default
    # is only here to fill the rows that already exist.
    op.alter_column("vacancy_skill", "source", server_default=None)


def downgrade() -> None:
    """Drop the column and the type.

    Rows found in descriptions are left in place, unmarked, which is the one
    thing worth knowing about this direction: after a downgrade the table again
    says "the employer asked for this" about every row, including the ones it
    never asked for. Re-running the backfill on the older code rewrites them
    from ``key_skills`` alone, because skills are replaced, not merged.
    """
    op.drop_column("vacancy_skill", "source")
    op.execute("DROP TYPE IF EXISTS requirement_source")
