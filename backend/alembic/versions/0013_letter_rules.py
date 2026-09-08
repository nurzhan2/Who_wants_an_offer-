"""Record which version of the letter rules wrote each letter.

Revision ID: 0013_letter_rules
Revises: 0012_documents
Create Date: 2026-09-08

``application.cover_letter`` holds a letter; nothing held the rules that judged
it. That was invisible while the only reader was a terminal printing the letter
it had just generated. The documents screen reads letters written weeks apart,
and without this column it could only show the rules in force *now* and let a
reader assume those produced everything on the list — which is exactly wrong at
the moment a rule changes, because the letters written before it are the ones
worth looking at.

Nullable, and staying nullable. NULL means "written before this was recorded",
which is neither "version zero" nor "passes today's rules"; the screen says *not
recorded* and recomputes today's verdict separately, so the two facts stay
apart. There is no backfill for the same reason migration 0009 refused one it
could not justify: nothing knows which rules judged a letter written last week,
and writing today's number over it would manufacture the provenance the column
exists to establish.

``String(64)``: the same fingerprint ``generated_document.rules_version`` holds,
which is what a rules version is in this project after phases 08 and 10 met.
A counter was the original design and it named only the built-in half of the
checks — two letters written either side of an edit in the workshop would have
reported the same version, which is the one question this column exists to
answer. See ``app/documents/rules.py``.
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_letter_rules"
down_revision = "0012_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the column. Existing rows keep NULL — see the module docstring."""
    op.add_column(
        "application",
        sa.Column("letter_rules_version", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """Drop it. Only the documents screen read it, and it tolerates NULL."""
    op.drop_column("application", "letter_rules_version")
