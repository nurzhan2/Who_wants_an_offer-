"""What a connector can say about where its walk of a corpus has got to.

Its own module, and not ``app/schemas/source.py``, because ``app/sources/base``
has to name this type in a method signature while ``schemas/source`` imports
*from* ``sources/base``. One import edge in each direction is a cycle; a leaf
module both sides may import is not.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SavedState(BaseModel):
    """One row a connector wrote to ``source_state``, with its date.

    ``value`` is ``Any``-valued for the reason CLAUDE.md asks for: the shape
    belongs to the connector that wrote it and to nothing else. The repository
    does not validate it, this model does not interpret it, and the connector
    parses it back with its own model — which is what keeps the key scheme an
    implementation detail of ``sources/``.

    ``updated_at`` travels with it because it is the only record of *when* the
    position was written, and a position with no date cannot be told from one
    that has not moved in a month.
    """

    key: str
    value: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime


class CrawlPosition(BaseModel):
    """Where one source's walk of one part of a corpus has got to.

    Assembled by the connector out of its own saved state, because only the
    connector knows how it keys that state. CLAUDE.md rule 5 puts the cost of
    adding a source inside ``sources/``, and an overview endpoint that parsed
    ``source_state`` keys itself would move that cost back out.

    The two counts are measurements taken while the last run read the file, and
    neither is derived from the other. ``None`` means *not measured* — a
    position written before this connector started counting says how far the
    walk got and nothing about what is left, and rendering that as zero would
    report a finished backfill that never happened.
    """

    #: The part of the world this row is about: for hh, one regional host.
    scope: str
    #: The part of that corpus: for hh, one sitemap file.
    label: str
    #: A person's name for the scope, when the source has one. hh has cities.
    title: str | None = None
    #: Entries the file held when it was last read.
    total: int | None = None
    #: How many of them this walk had still not covered at that moment.
    outstanding: int | None = None
    #: Finished stretches. More than one means a run was interrupted.
    stretches: int = 0
    #: The newest and the oldest entry the finished stretches cover.
    newest: datetime | None = None
    oldest: datetime | None = None
    #: When the connector last wrote anything about this file.
    updated_at: datetime | None = None
