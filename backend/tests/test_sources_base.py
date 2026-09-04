"""The plugin contract every connector is held to, tested without a connector.

``base.py`` is the one file in ``sources/`` that no source may edit, so a
regression here is not a bug in one feed — it is a bug in all of them at once,
and it surfaces somewhere else entirely. That shape decides what is worth
asserting below.

**The two decisions the module docstring calls load-bearing.** ``SearchQuery``
is hashable and ``search`` is not ``async def``. Both look like mistakes to a
reader who skims, both are one keystroke from being "tidied up", and neither
fails at the point of the edit: a list-valued ``keywords`` field raises only
when the planner tries to collapse duplicates, and an ``async def search``
compiles fine and breaks every caller. So each has a test that fails the moment
the decision is reversed.

**The lengths are database columns wearing a Pydantic hat.** A source that emits
a 400-character identifier has to fail at its own boundary, naming the field.
Let through, the same value fails inside ``bulk_upsert`` as an asyncpg
truncation error, taking a whole hundred-posting batch down with it and pointing
at the writer instead of the source that produced it.

**A missing credential is a configuration answer, not an incident.** It has to
be computable with no request and no database, and the reason it produces gets
rendered into an API response and a log line — which is why one assertion below
is about what the reason does *not* contain.

Nothing here touches PostgreSQL or the network: every input is built in the
test, and the only source is a double defined in this file.
"""

import inspect
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import settings
from app.sources.base import BaseSource, RawPosting, SearchQuery, SourceUnavailable

pytestmark = pytest.mark.unit

#: Stands in for a real key. Distinctive enough that finding it anywhere in a
#: rendered reason is unambiguous evidence of a leak.
SECRET_VALUE = "rapidapi-live-9c1f4e7b0a2d"


def posting(external_id: str, *, source_slug: str = "fake") -> RawPosting:
    """One posting carrying only the fields the identity tests read."""
    return RawPosting(
        source_slug=source_slug,
        external_id=external_id,
        url=f"https://example.invalid/{external_id}",
        title="Data Engineer",
    )


class FakeSource(BaseSource):
    """A source that serves scripted pages and counts how often it was asked.

    Deliberately not decorated with ``@register_source``: the registry is the
    production wiring, and a double has no business appearing in it.
    """

    slug: ClassVar[str] = "fake"
    name: ClassVar[str] = "Fake source"

    def __init__(self, *pages: Sequence[RawPosting]) -> None:
        super().__init__()
        self.pages: tuple[Sequence[RawPosting], ...] = pages
        self.calls: list[SearchQuery] = []

    async def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]:
        """Yield every scripted page, recording that this query really ran."""
        self.calls.append(query)
        for page in self.pages:
            for item in page:
                yield item


class CredentialedSource(FakeSource):
    """A source that declares two keys and nothing else about authentication."""

    slug: ClassVar[str] = "credentialed"
    name: ClassVar[str] = "Credentialed source"
    requires_auth: ClassVar[bool] = True
    required_credentials: ClassVar[tuple[str, ...]] = (
        "credentialed.api_key",
        "credentialed.api_secret",
    )


class MeteredSource(FakeSource):
    """A source whose published terms permit one run every six hours."""

    slug: ClassVar[str] = "metered"
    name: ClassVar[str] = "Metered source"
    min_interval: ClassVar[timedelta] = timedelta(hours=6)


# ── the query the planner collapses ───────────────────────────────────


def test_the_same_query_written_two_ways_collapses_to_one() -> None:
    """The planner crosses skill groups with placements and repeats itself.

    Collapsing those repeats is what stops a run paying twice for the same
    request, and it is done with a set — so equality alone is not enough, the
    two spellings must also hash alike. A caller passing a list must land on the
    same object as a caller passing a tuple, because both spellings occur in
    ``query_planner`` and neither is wrong.
    """
    from_list = SearchQuery(keywords=["Python", "SQL"])
    from_tuple = SearchQuery(keywords=("Python", "SQL"))

    assert from_list == from_tuple
    assert hash(from_list) == hash(from_tuple)
    assert len({from_list, from_tuple}) == 1


def test_a_query_can_be_hashed_at_all() -> None:
    """A ``list`` field would make this raise, under ``frozen=True`` or not.

    Asserted on its own because the failure is remote from its cause: the
    annotation is changed in ``base.py`` and the ``TypeError`` surfaces inside
    ``search_batch``'s ``dict.fromkeys`` on a production run.
    """
    query = SearchQuery(keywords=["python"], area="Almaty", limit=50)

    assert isinstance(hash(query), int)
    assert query.keywords == ("python",)


def test_keywords_keep_the_order_the_source_will_rank_by() -> None:
    """These words become a query string, and relevance is not symmetric.

    Sorting them — the obvious way to make duplicate collapsing easier — would
    quietly reorder what every source ranks against, so a resume whose strongest
    skill is Python starts getting results ranked for Airflow.
    """
    query = SearchQuery(keywords=["Python", "Airflow", "dbt"])

    assert query.keywords == ("Python", "Airflow", "dbt")


def test_blank_and_repeated_keywords_are_dropped() -> None:
    """A profile yields "Python" and "python" from two sections, and empty cells.

    Left in, each repeat becomes another paid request returning the same
    postings; a blank becomes a query for everything the source holds.
    """
    query = SearchQuery(keywords=["Python", "  ", "python", "PYTHON", "", "SQL", " sql "])

    assert query.keywords == ("Python", "SQL")


def test_a_query_cannot_be_edited_after_the_plan_is_built() -> None:
    """A mutation would change the hash of an object already sitting in a set.

    The entry becomes unreachable, the dedup silently stops working, and the
    run issues the duplicate requests the planner went to the trouble of
    removing.
    """
    query = SearchQuery(keywords=["python"])

    with pytest.raises(ValidationError):
        query.area = "Almaty"  # type: ignore[misc]

    assert query.area is None


# ── the posting, and the columns behind it ────────────────────────────


@pytest.mark.parametrize(
    ("field", "limit"),
    [("external_id", 200), ("url", 1000), ("title", 300)],
)
def test_a_value_one_character_over_its_column_is_refused(field: str, limit: int) -> None:
    """Exactly at the column length is a posting; one over is a lost batch.

    The limits mirror ``vacancy_source`` columns, so the boundary is asserted on
    both sides: too strict and legitimate postings are dropped for nothing, too
    loose and the value reaches ``bulk_upsert``, where asyncpg raises a
    truncation error that fails all hundred postings written with it and names
    the writer rather than the source at fault.
    """
    fields: dict[str, str] = {
        "source_slug": "fake",
        "external_id": "42",
        "url": "https://example.invalid/42",
        "title": "Data Engineer",
    }

    assert RawPosting.model_validate({**fields, field: "x" * limit})

    with pytest.raises(ValidationError) as raised:
        RawPosting.model_validate({**fields, field: "x" * (limit + 1)})

    assert [error["loc"] for error in raised.value.errors()] == [(field,)]


def test_a_posting_with_no_url_is_refused() -> None:
    """The url is the only thing the dashboard can send a person to.

    An empty string passes a ``str`` annotation and reaches the database as a
    NOT NULL column that is technically satisfied, so the posting is stored,
    scored, shown — and the "Apply" link goes nowhere.
    """
    with pytest.raises(ValidationError) as raised:
        RawPosting.model_validate(
            {
                "source_slug": "fake",
                "external_id": "42",
                "url": "",
                "title": "Data Engineer",
            }
        )

    assert [error["loc"] for error in raised.value.errors()] == [("url",)]


def test_fetched_at_is_timezone_aware() -> None:
    """A naive timestamp becomes a silent hours-wide error, not a crash.

    It is written to a ``timestamptz`` column, which will assume some zone for
    it; the freshness filter and ``max_vacancy_age_days`` then work off a time
    that is off by the deployment's offset, and postings age wrong rather than
    visibly break.
    """
    fetched_at = posting("42").fetched_at

    assert fetched_at.tzinfo is not None
    assert fetched_at.utcoffset() is not None


def test_the_key_is_the_pair_the_upsert_matches_on() -> None:
    """``(source, external_id)`` is the natural key idempotency rests on.

    Widen or reorder it and a repeated run stops recognising what it already
    holds, so every run inserts the whole feed again instead of updating it.
    """
    assert posting("42", source_slug="remotive").key == ("remotive", "42")


def test_two_sources_may_use_the_same_external_id() -> None:
    """Boards number their postings independently, and "1" is a popular number.

    Deduplicating on the identifier alone would let whichever source ran first
    suppress the other's posting entirely.
    """
    assert posting("1", source_slug="remotive").key != posting("1", source_slug="arbeitnow").key


def test_a_posting_is_never_hashed_so_dedup_goes_through_the_key() -> None:
    """``raw`` is a dict, so the frozen model is still unhashable.

    Recorded as a test because the obvious way to write ``search_batch`` is a
    set of postings, and that ``TypeError`` would only appear once a real feed
    was being paginated.
    """
    with pytest.raises(TypeError):
        hash(posting("42"))


def test_with_detail_returns_a_new_posting_and_leaves_the_listing_one_alone() -> None:
    """The listing posting is often still in the caller's page buffer.

    Mutating in place would edit an object another loop is mid-iteration over,
    and — worse — a detail fetch that fails halfway would leave half the page
    carrying descriptions belonging to postings that were never confirmed.
    """
    listed = posting("42")
    detailed = listed.with_detail(description="Full text", raw={"body": "html"})

    assert detailed is not listed
    assert detailed.description == "Full text"
    assert detailed.raw == {"body": "html"}
    assert listed.description is None
    assert listed.raw == {}
    # Identity has to survive the round trip, or the detail lands on nothing.
    assert detailed.key == listed.key


def test_with_detail_keeps_the_original_payload_when_none_is_offered() -> None:
    """Most sources return only the body from a detail page.

    Overwriting ``raw`` with an empty dict would throw away the listing payload
    that normalisation is meant to be able to re-run against without refetching.
    """
    listed = RawPosting(
        source_slug="fake",
        external_id="42",
        url="https://example.invalid/42",
        title="Data Engineer",
        raw={"from": "listing"},
    )

    assert listed.with_detail(description="Full text").raw == {"from": "listing"}


# ── availability ──────────────────────────────────────────────────────


def test_a_source_missing_a_credential_names_the_key_and_not_its_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason is rendered into an API response and a log line.

    Both are read by more people than the ``.env`` is, so the reason may carry
    the *names* of the keys that are absent and nothing about the one that is
    present. A value, a prefix or even a length narrows a live key for whoever
    is reading, and this is the only place in the framework that handles both at
    once.
    """
    monkeypatch.setattr(
        settings,
        "source_credentials",
        {"credentialed.api_key": SecretStr(SECRET_VALUE)},
    )
    source = CredentialedSource()

    assert source.is_configured() is False
    assert source.missing_credentials() == ("credentialed.api_secret",)

    reason = source.unavailable()
    assert reason is not None
    assert reason.code is SourceUnavailable.MISSING_CREDENTIALS
    assert reason.missing_credentials == ("credentialed.api_secret",)
    assert "credentialed.api_secret" in reason.detail

    rendered = reason.model_dump_json()
    assert SECRET_VALUE not in rendered
    assert SECRET_VALUE[:8] not in rendered
    assert str(len(SECRET_VALUE)) not in rendered


def test_a_fully_configured_source_reports_nothing_wrong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: without it the test above could pass on a source that is
    always unavailable, and the whole registry would be dark for the same
    reason while every assertion stayed green."""
    monkeypatch.setattr(
        settings,
        "source_credentials",
        {
            "credentialed.api_key": SecretStr(SECRET_VALUE),
            "credentialed.api_secret": SecretStr(SECRET_VALUE),
        },
    )
    source = CredentialedSource()

    assert source.missing_credentials() == ()
    assert source.is_configured() is True
    assert source.unavailable() is None


def test_a_source_declaring_no_credentials_needs_no_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Free feeds are the majority, and they must run on an empty settings file.

    Requiring configuration by default would mean a fresh checkout collects
    nothing at all until someone finds out which keys the framework wanted.
    """
    monkeypatch.setattr(settings, "source_credentials", {})
    source = FakeSource()

    assert source.is_configured() is True
    assert source.unavailable() is None


# ── scheduling ────────────────────────────────────────────────────────


def test_a_source_asked_again_too_soon_is_not_due() -> None:
    """``min_interval`` comes from a source's published terms, not from taste.

    Ignoring it is how an account gets rate-limited or closed, and the pipeline
    has no other brake: the scheduler asks every source on every tick.
    """
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    source = MeteredSource()

    assert source.is_due(now - timedelta(hours=5, minutes=59), now=now) is False


def test_a_source_is_due_the_moment_the_interval_has_elapsed() -> None:
    """The boundary is inclusive, so a six-hourly source really runs four times
    a day. Exclusive, each run drifts by the length of the tick and the source
    quietly loses a run somewhere near midnight."""
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    source = MeteredSource()

    assert source.is_due(now - timedelta(hours=6), now=now) is True
    assert source.is_due(now - timedelta(days=1), now=now) is True


def test_a_source_that_has_never_run_is_due() -> None:
    """A new connector has no last attempt, and None is not "infinitely recent".

    Read the wrong way round it would never run at all, and the failure is
    invisible: the source is registered, configured and silent.
    """
    assert MeteredSource().is_due(None) is True


def test_the_cooldown_is_the_last_attempt_plus_the_interval() -> None:
    """The dashboard shows this timestamp, so it has to be the real one.

    Computed from the last *attempt*: a failed run still spent the request, and
    keying off successes would retry a failing metered source at full speed —
    exactly what a 429 is asking us not to do.
    """
    last = datetime(2026, 9, 5, 6, 0, tzinfo=UTC)

    assert MeteredSource().cooldown_until(last) == last + timedelta(hours=6)
    assert MeteredSource().cooldown_until(None) is None
    # A source with no published interval is never in cooldown at all.
    assert FakeSource().cooldown_until(last) is None
    assert FakeSource().is_due(last) is True


# ── running several queries ───────────────────────────────────────────


async def test_a_repeated_query_reaches_the_source_only_once() -> None:
    """The planner emits the same query from two different skills.

    Every duplicate that gets through is a paid request returning results the
    run already has, and on a metered source it is spent quota.
    """
    source = FakeSource([posting("1")])
    query = SearchQuery(keywords=["python", "sql"])
    # The second spelling is the one the planner produces from another skill:
    # an equal query built separately, not the same object passed twice.
    same = SearchQuery(keywords=("python", "sql"))

    found = [item async for item in source.search_batch([query, same, query])]

    assert len(source.calls) == 1
    assert [item.external_id for item in found] == ["1"]


async def test_a_posting_served_on_two_pages_is_yielded_once() -> None:
    """Paginated feeds overlap: arbeitnow's pages two and three shared 17 of 175.

    A repeat that escapes here is normalised, embedded and scored a second time,
    and the dashboard shows the same job twice — the one defect a user notices
    immediately and cannot explain.
    """
    repeated = posting("17")
    source = FakeSource([posting("1"), repeated], [repeated, posting("2")])

    found = [item async for item in source.search_batch([SearchQuery()])]

    assert [item.external_id for item in found] == ["1", "17", "2"]


async def test_distinct_queries_all_run() -> None:
    """The control for the collapsing above: two different queries are two runs.

    An over-eager dedup would silently drop half the plan, and the run would
    look healthy while collecting a fraction of what it was asked for.
    """
    source = FakeSource([posting("1")])

    found = [
        item
        async for item in source.search_batch(
            [SearchQuery(keywords=["python"]), SearchQuery(keywords=["sql"])]
        )
    ]

    assert len(source.calls) == 2
    # The posting itself is still yielded once, across queries.
    assert [item.external_id for item in found] == ["1"]


async def test_search_is_iterated_directly_rather_than_awaited() -> None:
    """``search`` is not ``async def``, and this is the assertion that says so.

    Declared ``async def``, the abstract method types every implementation's
    call as a coroutine, and each of the callers — ``search_batch`` here, the
    runner, every future connector's tests — has to write
    ``async for posting in await source.search(query)``. It is a one-word edit
    that looks like a correction, so it needs a test that fails on it rather
    than only a comment asking not to.
    """
    source = FakeSource([posting("1"), posting("2")])

    assert not inspect.iscoroutinefunction(BaseSource.search)

    stream = source.search(SearchQuery())
    assert not inspect.iscoroutine(stream)
    assert inspect.isasyncgen(stream)

    found = [item async for item in stream]
    assert [item.external_id for item in found] == ["1", "2"]

    # And the spelling every connector's caller actually uses.
    async for item in source.search(SearchQuery()):
        assert item.source_slug == "fake"
