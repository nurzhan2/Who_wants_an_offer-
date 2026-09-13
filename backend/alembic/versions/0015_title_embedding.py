"""Embed a vacancy's title apart from its description, and a profile's headline.

Revision ID: 0015_title_embedding
Revises: 0014_requirement_source
Create Date: 2026-09-13

The six-component formula barely used the title. It was one line inside the
text behind ``vacancy.embedding``, dissolved into up to four thousand characters
of description, and on 9 Sep 2026 five networking and telecom postings topped
the queue at 87.5-88.6 against a «Python Developer — Backend» profile on one
matched skill each. The title alone would have turned every one of them down.

Scoring now reads the title as its own signal, so it needs its own vector. The
profile's side of that comparison is the headline, which is short and dense in
the same way; embedding it against the long profile text would compare a title
with a paragraph.

``title_embedding_hash`` is the same device as ``embedding_text_hash``: the
sha256 of the exact text the vector was computed from, so a re-crawl that
changes a title is noticed and one that does not is not paid for twice.

No index. The scorer computes the distance over chunks of known ids, not a
nearest-neighbour search, so an HNSW index would be maintained and never read.

Reversible: the vectors are recomputable from ``title`` and ``headline``, which
stay.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0015_title_embedding"
down_revision: str | None = "0014_requirement_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The width of every vector column in this schema. A literal, like the one in
#: ``0001_initial_schema``: a migration must not change meaning when a setting
#: does.
EMBEDDING_DIM = 1024


def upgrade() -> None:
    """Add the title vector with its hash, and the headline vector."""
    op.add_column("vacancy", sa.Column("title_embedding", Vector(EMBEDDING_DIM), nullable=True))
    op.add_column("vacancy", sa.Column("title_embedding_hash", sa.CHAR(64), nullable=True))
    op.add_column(
        "candidate_profile",
        sa.Column("headline_embedding", Vector(EMBEDDING_DIM), nullable=True),
    )


def downgrade() -> None:
    """Drop the three columns; nothing in them is not recomputable."""
    op.drop_column("candidate_profile", "headline_embedding")
    op.drop_column("vacancy", "title_embedding_hash")
    op.drop_column("vacancy", "title_embedding")
