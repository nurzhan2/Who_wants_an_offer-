"""What the sources page and the run endpoint speak.

One rule shapes the whole module: a credential is answered for by name and by
presence, never by value. ``GET /sources`` says *which key is missing*, so the
person reading it knows what to add, and says nothing else — not a prefix, not
a length, not a masked form. All three narrow a key for anyone who can read the
response or a screenshot of it.
"""

from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from app.schemas.crawl import SearchPreview
from app.schemas.pipeline import PipelineRunRead
from app.sources.base import AccessMode, Unavailable


class SourceStatus(BaseModel):
    """One connector, as the dashboard sees it."""

    slug: str
    name: str
    regions: tuple[str, ...] = ()
    access_mode: AccessMode
    #: Where the terms live. Present for every API-mode source, because the
    #: registry refuses to register one without it.
    terms_url: str | None = None
    #: Credit line the vendor requires beside a posting from this source.
    attribution: str | None = None
    requests_per_second: float
    #: Metered allowance per UTC day, when the source is metered.
    daily_quota: int | None = None
    daily_used: int = 0
    min_interval: timedelta = timedelta(0)
    #: True when nothing stands in the way of running it right now.
    enabled: bool
    #: Why it will not run. None when it will.
    inactive: Unavailable | None = None
    last_run: PipelineRunRead | None = None


class SourcesResponse(BaseModel):
    """The sources page in one request."""

    sources: list[SourceStatus] = Field(default_factory=list)
    #: Connector modules that failed to import, by module name. A typo in one
    #: file must not disable the pipeline, but it must not be invisible either.
    import_errors: dict[str, str] = Field(default_factory=dict)


class SourceSearchPreview(BaseModel):
    """One source's part of the search plan."""

    slug: str
    name: str
    enabled: bool
    #: Why it will not run. None when it will.
    inactive: Unavailable | None = None
    preview: SearchPreview


class SearchPlanResponse(BaseModel):
    """What the next run will look for, and where.

    Answers "what did saving my job titles change". Built without a request to
    any source, from the active profile and what the sources stored last time.
    """

    #: The titles the plan was built from. Empty means skill keywords were used.
    target_titles: list[str] = Field(default_factory=list)
    #: Where the plan's words came from: ``titles``, ``skills`` or ``headline``.
    basis: str
    #: The line sources rank by, when there is one.
    intent: str | None = None
    queries: int = 0
    #: Searches the per-run cap cut.
    dropped: int = 0
    sources: list[SourceSearchPreview] = Field(default_factory=list)


class SourceRunSummary(BaseModel):
    """What one source did during a run."""

    slug: str
    found: int = 0
    new: int = 0
    updated: int = 0
    #: Postings that collapsed into a vacancy another posting had already
    #: created — the cross-publisher duplicate rate, worth seeing.
    duplicates: int = 0
    #: HTTP requests sent, which is what a metered source is billed for.
    requests: int = 0
    duration_seconds: float = 0.0
    errors: list[dict[str, object]] = Field(default_factory=list)
    skipped: Unavailable | None = None


class EmbeddingSummary(BaseModel):
    """What the embedding step did."""

    considered: int = 0
    #: Rows seen again whose text had not moved, so the model was not called.
    unchanged: int = 0
    embedded: int = 0
    #: Present when the model runtime is not installed. Not an error: the
    #: vectors are computed on a later run.
    skipped_reason: str | None = None


class PlanSummary(BaseModel):
    """The search plan, without the searches.

    The queries themselves are absent on purpose: they are built from the
    candidate's own skills, and this response is the one part of the pipeline a
    browser renders.
    """

    queries: int = 0
    groups: tuple[str, ...] = ()
    placements: int = 0
    collapsed: int = 0
    dropped: int = 0
    limit: int = 0


class RunResponse(BaseModel):
    """The result of one crawl."""

    dry_run: bool = False
    started_at: datetime
    duration_seconds: float = 0.0
    plan: PlanSummary
    sources: list[SourceRunSummary] = Field(default_factory=list)
    embedding: EmbeddingSummary | None = None
    found: int = 0
    new: int = 0
    duplicates: int = 0
