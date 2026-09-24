"""What a connector can say about where its walk of a corpus has got to.

Its own module, and not ``app/schemas/source.py``, because ``app/sources/base``
has to name this type in a method signature while ``schemas/source`` imports
*from* ``sources/base``. One import edge in each direction is a cycle; a leaf
module both sides may import is not.
"""

from datetime import datetime
from enum import StrEnum
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


class SearchUse(StrEnum):
    """How a source turns the owner's search into requests."""

    #: The terms are sent upstream as search queries, verbatim.
    QUERY = "query"
    #: The whole feed is downloaded and filtered here by the terms.
    FILTER = "filter"
    #: The terms choose which catalogue pages are opened first.
    CATALOG = "catalog"


class SearchPreview(BaseModel):
    """What one source will be asked for on its next run, in order.

    Built by the connector, for the reason :class:`CrawlPosition` is: only the
    connector knows how its input becomes requests. Pure — no request is made
    to build it — so a person can change their search and see the effect
    without spending anything.
    """

    use: SearchUse
    #: The requests or catalogue pages, first to be spent first.
    terms: list[str] = Field(default_factory=list)
    #: How many more there are beyond :attr:`terms`.
    more: int = Field(default=0, ge=0)
    #: One sentence a person can act on: a budget, a staleness, a caveat.
    note: str | None = None


class CrawlStop(StrEnum):
    """Why a run ended. Four different decisions for whoever reads it.

    Stored as a token and rendered in Russian where it is shown, rather than
    stored as the sentence: the value survives in ``source_state`` for as long
    as the row does, and a wording change should not mean two spellings of the
    same fact in one table.
    """

    #: Nothing left outstanding on any site the run walked. The good ending.
    CORPUS = "corpus"
    #: The wall-clock budget ran out. Expected, on a night run, and not a
    #: problem: it says the night was the limit, which is what it was for.
    TIME = "time"
    #: The page budget ran out. On a timed run this usually means the two
    #: settings disagree — see ``sources.hh.page_budget_binds``.
    PAGES = "pages"
    #: A check for robots ended it. The one ending that asks the owner to decide
    #: something: crawl that host more slowly, or less often, or not tonight.
    CHALLENGE = "challenge"
    #: Something else ended it — the network went, the laptop slept, a module
    #: broke. Its own value rather than folded into ``PAGES``, because a run
    #: that spent its budget and a run that fell over look identical in the
    #: counters and mean opposite things at breakfast.
    INTERRUPTED = "interrupted"


class CrawlChallenge(BaseModel):
    """One check for robots, and what the run did about it."""

    #: The host that answered with it.
    scope: str
    title: str | None = None
    at: datetime
    #: Pages the run had fetched by then. The number that says whether the rate
    #: is the problem: 50 and 172 are the two this connector was built around.
    after_pages: int = 0
    #: Whether the run waited it out and came back, rather than ending there.
    resumed: bool = False


class CrawlCityRun(BaseModel):
    """What one run did in one city."""

    scope: str
    title: str | None = None
    fetched: int = 0
    stored: int = 0
    #: Postings carrying one of the professions the profile asked for. Against
    #: ``fetched`` this is relevant postings per request spent.
    role_hits: int = 0
    #: Entries still not covered when the run read this city's sitemaps.
    outstanding: int = 0
    #: Whether the run got through this city's share or was cut short in it.
    finished: bool = False


class CrawlRunSummary(BaseModel):
    """One run, in the shape a person reads over coffee.

    Written by the connector into its own ``source_state`` and read back by it,
    for the same reason :class:`CrawlPosition` is assembled there: the keys and
    the meaning are the connector's, and rule 5 keeps both inside ``sources/``.

    It is deliberately a *run* summary and not a corpus one. Where the walk has
    got to is already answered, per file, by :class:`CrawlPosition`; what this
    adds is the part that is gone by morning — how long the run had, what it
    spent, and whether anything stopped it.
    """

    started_at: datetime
    finished_at: datetime
    #: Requests the run was allowed, and minutes, as configured.
    pages: int
    minutes: float | None = None
    #: Pages actually fetched, across every city.
    fetched: int = 0
    stored: int = 0
    stopped_by: CrawlStop = CrawlStop.CORPUS
    cities: list[CrawlCityRun] = Field(default_factory=list)
    challenges: list[CrawlChallenge] = Field(default_factory=list)
    #: Seconds spent waiting out a challenge rather than crawling.
    paused_seconds: float = 0.0

    @property
    def outstanding(self) -> int:
        """Entries the run left uncovered across the cities it walked."""
        return sum(city.outstanding for city in self.cities)
