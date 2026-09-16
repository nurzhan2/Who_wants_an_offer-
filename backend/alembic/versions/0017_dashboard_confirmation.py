"""Record the owner's confirmation of an application, given in the dashboard.

Revision ID: 0017_dashboard_confirmation
Revises: 0016_target_titles
Create Date: 2026-09-16

Until now an application could be confirmed only in a terminal, by typing a word
under a printed card. The dashboard now shows the same card in a modal, and the
owner's "yes" there has to survive until the local agent picks it up — which is
a different process, minutes or hours later. So it is stored, and it is stored
as what was agreed to rather than as a flag:

* ``confirmed_at`` — when;
* ``confirmed_letter_digest`` — SHA-256 of the letter read, so a regeneration
  voids the confirmation;
* ``confirmed_card_digest`` — SHA-256 of the whole card read, which the agent
  binds its mandate to, exactly as the terminal confirmation does.

All nullable, no backfill: no existing row was confirmed this way.

**Not in this migration: the ``alembic check`` drift on ``generation_rule`` and
``reference_document``.** Those server defaults were created by
``0011_workshop`` and have been in every database since; only the model forgot
to declare them. The fix is in ``app/db/models.py``, and a migration re-creating
defaults that already exist would be a no-op pretending to be a change.

Reversible: dropping the columns forgets pending confirmations, which the
agent then treats as never given.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_dashboard_confirmation"
down_revision: str | None = "0016_target_titles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the three confirmation columns."""
    op.add_column(
        "application", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "application", sa.Column("confirmed_letter_digest", sa.CHAR(64), nullable=True)
    )
    op.add_column("application", sa.Column("confirmed_card_digest", sa.CHAR(64), nullable=True))


def downgrade() -> None:
    """Drop them; a pending confirmation is then simply not given."""
    op.drop_column("application", "confirmed_card_digest")
    op.drop_column("application", "confirmed_letter_digest")
    op.drop_column("application", "confirmed_at")
