"""The embedding step: does it drain, and does the work survive being stopped?

This file exists because of a failure that left no trace anywhere it would be
looked for. The database held 466 vacancies and zero vectors while
``.cache/embeddings`` held 466 vectors — every one of them computed, at about
seven seconds each, and then thrown away. The old step encoded the whole backlog
in one call and wrote it in one statement at the very end, so 54 minutes of work
became nothing the moment anything ended the process first. The run reported
itself as a success, because the crawl had succeeded.

So the properties under test here are not "does it embed", which is easy and was
never broken. They are:

* every batch is durable before the next one starts;
* a backlog bigger than one batch, and bigger than one selection window, is
  actually worked through;
* the loop terminates even though its selection is a predicate that keeps
  re-offering rows nobody needs to embed;
* a run that stops early says how much is really left — counted in the
  database, not read off the last window, which on the shipped settings is
  empty exactly when the backlog is largest;
* the script that drives all of this from a terminal draws the right conclusion
  from those numbers, in characters a cp1251 console can print.

Most of it needs no database and no model: the repository is a double that
reproduces the real *predicate* semantics — a row leaves the window only when a
vector is written for it — and the provider is the deterministic fake.

The last section does use the database, because two of the claims above are
claims about SQL and a double cannot test them: that ``count_needing_embedding``
counts the same rows ``needs_embedding`` offers, and that a batch committed
before an interrupted run really is still there afterwards.
"""

import argparse
import importlib.util
import itertools
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import replace
from datetime import timedelta
from types import ModuleType
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from structlog.testing import capture_logs

from app.core.config import settings
from app.db.models import Vacancy
from app.db.repositories.vacancy import EmbeddedVacancy, EmbeddingCandidate, VacancyRepository
from app.matching import embeddings
from app.matching.embeddings import (
    EmbeddingsUnavailableError,
    FakeEmbeddingProvider,
    get_provider,
    vacancy_text,
)
from app.pipeline import embedding as step
from app.pipeline.embedding import Clock, EmbeddingOutcome, embed_pending, text_hash
from factories import make_vacancy
from helpers import REPO_ROOT

# Marked per test rather than for the module: everything above the database
# section is a pure unit test, and the section at the bottom deliberately is
# not. Following test_llm_usage.py, which is split the same way.


# ── doubles ───────────────────────────────────────────────────────────


class FakeVacancies:
    """A stand-in for ``VacancyRepository`` that keeps the real selection semantics.

    The subtlety worth reproducing is that ``needs_embedding`` is a *predicate*,
    not a queue. A row leaves it only once a vector has been written for that
    row, so a row the step skips because its text had not moved is offered again
    by the very next call — forever. A double backed by a list that pops would
    make the loop look terminating when it is not.
    """

    def __init__(self, rows: Sequence[EmbeddingCandidate]) -> None:
        self._rows = list(rows)
        self.written: dict[UUID, EmbeddedVacancy] = {}
        #: One entry per selection, so a test can count round trips.
        self.selections: list[int] = []
        #: How many times the step asked for the unlimited count.
        self.counts = 0
        #: And how many times for the narrow one.
        self.never_embedded_counts = 0

    def outstanding(self) -> list[EmbeddingCandidate]:
        """Every row the selection still flags, with no window on it.

        The ground truth a test compares ``EmbeddingOutcome.backlog`` against.
        """
        return [row for row in self._rows if row.id not in self.written]

    async def needs_embedding(self, *, limit: int) -> list[EmbeddingCandidate]:
        """Rows with no vector yet, newest first, capped at ``limit``."""
        self.selections.append(limit)
        return self.outstanding()[:limit]

    async def count_needing_embedding(self) -> int:
        """The same predicate, counted rather than windowed.

        Counts unchanged rows too, exactly as the SQL does: nothing is ever
        written for them, so they stay flagged and the real ``COUNT`` keeps
        returning them. A double that quietly excluded them would let the step's
        subtraction of ``unchanged`` look right while being untested.
        """
        self.counts += 1
        return len(self.outstanding())

    async def count_never_embedded(self) -> int:
        """Rows with no vector at all, the narrow half of the predicate.

        ``stored_hash`` is the faithful stand-in for it: a row only has one
        because a vector was written for it, so a row without one is exactly the
        SQL's ``embedding IS NULL OR embedded_at IS NULL``. This is the count
        that separates a starved run from a re-crawl's churn, so a double that
        returned the wide count here would hide the difference the step exists
        to draw.
        """
        self.never_embedded_counts += 1
        return sum(1 for row in self.outstanding() if row.stored_hash is None)

    async def set_embeddings(self, items: Sequence[EmbeddedVacancy]) -> int:
        """Store the batch and report how many rows it touched."""
        for item in items:
            self.written[item.id] = item
        return len(items)


class FakeSession:
    """Only the one method the step uses, counting and logging every call."""

    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.commits = 0

    async def commit(self) -> None:
        """Record a commit."""
        self.commits += 1
        self._log.append("commit")


class CountingProvider:
    """Deterministic vectors, with a record of every batch it was handed."""

    name: str = "counting"

    def __init__(
        self, log: list[str], *, fail_on: int | None = None, error: Exception | None = None
    ) -> None:
        self._log = log
        self._fail_on = fail_on
        self._error = error or RuntimeError("the model fell over")
        #: Size of each batch the provider saw, in order.
        self.calls: list[int] = []

    async def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """Encode, or break on the nth call so a test can stop the run midway."""
        self.calls.append(len(texts))
        self._log.append("encode")
        if self._fail_on is not None and len(self.calls) == self._fail_on:
            raise self._error
        return [FakeEmbeddingProvider.vector_for(text) for text in texts]


def candidate(index: int, *, unchanged: bool = False) -> EmbeddingCandidate:
    """One selectable row. ``unchanged`` gives it the hash of its own text."""
    row = EmbeddingCandidate(
        id=uuid4(),
        title=f"Backend Engineer {index}",
        company="Acme",
        city=None,
        description=f"Body of posting {index}.",
        stored_hash=None,
    )
    if not unchanged:
        return row
    digest = text_hash(
        vacancy_text(
            title=row.title, company=row.company, city=row.city, description=row.description
        )
    )
    return replace(row, stored_hash=digest)


def ticking(step_seconds: float) -> Clock:
    """A clock that advances by a fixed amount on every read.

    Deterministic on purpose: a budget tested against the wall clock either
    sleeps through it or passes by luck.
    """
    counter = itertools.count(0, 1)
    return lambda: next(counter) * step_seconds


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_disk_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caching off.

    The default is ``.cache/embeddings`` relative to the working directory, so
    without this the suite reads and writes the developer's real cache — which
    on this project is a live directory a pipeline run may be filling right now.
    """
    monkeypatch.setattr(settings, "embedding_cache_dir", None)


@pytest.fixture(autouse=True)
def _isolated_provider() -> Iterator[None]:
    """Empty ``get_provider``'s lru_cache around every test."""
    get_provider.cache_clear()
    yield
    get_provider.cache_clear()


@pytest.fixture
def log() -> list[str]:
    """One ordered record of encodes and commits, so interleaving is testable."""
    return []


@pytest.fixture
def session(log: list[str]) -> FakeSession:
    """The session the step commits."""
    return FakeSession(log)


def wire(
    monkeypatch: pytest.MonkeyPatch,
    rows: Sequence[EmbeddingCandidate],
    provider: CountingProvider,
) -> FakeVacancies:
    """Point the step at the doubles and hand back the repository."""
    vacancies = FakeVacancies(rows)
    monkeypatch.setattr(step, "VacancyRepository", lambda _session: vacancies)
    monkeypatch.setattr(embeddings, "get_provider", lambda: provider)
    return vacancies


def as_session(fake: FakeSession) -> AsyncSession:
    """The double, typed as what the step's signature asks for."""
    return cast("AsyncSession", fake)


# ── draining ──────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_a_backlog_larger_than_one_batch_is_worked_through(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession, log: list[str]
) -> None:
    """Fifty rows against a batch of eight is seven batches, not one and a cap.

    The reported symptom was "500 records per run against a corpus of 13 557",
    and the fix has to be that one call keeps going rather than that the number
    500 gets bigger.
    """
    monkeypatch.setattr(settings, "embedding_batch_size", 8)
    provider = CountingProvider(log)
    vacancies = wire(monkeypatch, [candidate(n) for n in range(50)], provider)

    outcome = await embed_pending(as_session(session))

    assert outcome.embedded == 50
    assert outcome.batches == 7
    assert outcome.stopped == "drained"
    assert outcome.backlog == 0
    assert len(vacancies.written) == 50


@pytest.mark.unit
async def test_a_backlog_larger_than_one_selection_window_is_worked_through(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession, log: list[str]
) -> None:
    """The window is a page over the backlog, not the whole of what a run may do.

    Rows leave the predicate as their vectors are committed, so the next
    selection returns the next page. If the step stopped after one window the
    corpus behind a city could never be reached at all.
    """
    monkeypatch.setattr(step, "SELECT_WINDOW", 4)
    monkeypatch.setattr(settings, "embedding_batch_size", 4)
    provider = CountingProvider(log)
    vacancies = wire(monkeypatch, [candidate(n) for n in range(12)], provider)

    outcome = await embed_pending(as_session(session))

    assert outcome.embedded == 12
    assert outcome.stopped == "drained"
    assert provider.calls == [4, 4, 4]
    # Three windows of work, then one that comes back empty and ends the loop.
    assert vacancies.selections == [4, 4, 4, 4]


@pytest.mark.unit
async def test_nothing_outstanding_never_reaches_the_model(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession, log: list[str]
) -> None:
    """An empty selection is the cheap common case and must cost one query."""
    provider = CountingProvider(log)
    wire(monkeypatch, [], provider)

    outcome = await embed_pending(as_session(session))

    assert outcome == step.EmbeddingOutcome(considered=0, unchanged=0, embedded=0)
    assert provider.calls == []
    assert session.commits == 0


# ── durability ────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_every_batch_is_committed_before_the_next_is_encoded(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession, log: list[str]
) -> None:
    """The whole point of the rewrite, stated as an interleaving.

    Encode, commit, encode, commit. Anything that batches the writes to the end
    reintroduces the bug: at seven seconds a posting, the window in which a
    process can die holding uncommitted work is the entire run.
    """
    monkeypatch.setattr(settings, "embedding_batch_size", 4)
    provider = CountingProvider(log)
    wire(monkeypatch, [candidate(n) for n in range(12)], provider)

    await embed_pending(as_session(session))

    assert log == ["encode", "commit"] * 3


@pytest.mark.unit
async def test_vectors_already_committed_survive_a_failure_later_in_the_run(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession, log: list[str]
) -> None:
    """A model that dies on the third batch must not cost the first two.

    This is the exact shape of what happened: everything computed, nothing
    written. The error is still allowed to propagate — swallowing it would hide
    a broken model — but the eight vectors that were paid for are in the
    database when it does.
    """
    monkeypatch.setattr(settings, "embedding_batch_size", 4)
    provider = CountingProvider(log, fail_on=3)
    vacancies = wire(monkeypatch, [candidate(n) for n in range(20)], provider)

    with pytest.raises(RuntimeError, match="fell over"):
        await embed_pending(as_session(session))

    assert len(vacancies.written) == 8
    assert session.commits == 2


@pytest.mark.unit
async def test_an_unavailable_model_keeps_what_it_managed_to_write(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession, log: list[str]
) -> None:
    """The graceful stop keeps its partial progress too, and reports the rest.

    "Kept" means committed. Counting the rows handed to the writer proves only
    that the step called it — which was equally true of the version that lost
    the whole backlog, because it called the writer too and then died before
    anything reached disk. So the commits are what is asserted here, and the
    interleaving is asserted with them: two encodes, each followed by its own
    commit, before the third encode fails.
    """
    monkeypatch.setattr(settings, "embedding_batch_size", 4)
    provider = CountingProvider(
        log, fail_on=3, error=EmbeddingsUnavailableError("runtime not installed")
    )
    vacancies = wire(monkeypatch, [candidate(n) for n in range(20)], provider)

    outcome = await embed_pending(as_session(session))

    assert outcome.stopped == "unavailable"
    assert outcome.skipped_reason
    assert outcome.embedded == 8
    assert outcome.backlog == 12
    assert len(vacancies.written) == 8
    assert session.commits == 2, "the eight vectors are on disk, not in a pending transaction"
    assert log == ["encode", "commit", "encode", "commit", "encode"]
    assert outcome.batches == session.commits


@pytest.mark.unit
async def test_a_missing_runtime_is_reported_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession, log: list[str]
) -> None:
    """A crawl that succeeded must not be reported as failed for want of an extra."""
    provider = CountingProvider(log, fail_on=1, error=EmbeddingsUnavailableError("no extra"))
    wire(monkeypatch, [candidate(n) for n in range(4)], provider)

    outcome = await embed_pending(as_session(session))

    assert outcome.embedded == 0
    assert outcome.skipped_reason
    assert outcome.stopped == "unavailable"


# ── budgets ───────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_the_row_cap_stops_the_run_and_never_abandons_a_batch(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession, log: list[str]
) -> None:
    """The cap is checked between batches, so a run may overshoot by one batch.

    Deliberate. Stopping inside a batch would mean discarding vectors that had
    already been computed, which is the failure this module was rewritten to
    stop doing. The cap bounds the run; the batch bounds the loss.
    """
    monkeypatch.setattr(settings, "embedding_batch_size", 4)
    provider = CountingProvider(log)
    wire(monkeypatch, [candidate(n) for n in range(20)], provider)

    outcome = await embed_pending(as_session(session), limit=10)

    assert outcome.embedded == 12
    assert outcome.embedded % 4 == 0
    assert outcome.stopped == "budget"


@pytest.mark.unit
async def test_the_clock_budget_stops_between_batches(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession, log: list[str]
) -> None:
    """A count cannot bound the time: a cached row is microseconds, a cold one seconds.

    The deadline is read once at the start and compared before each batch, so
    the run stops on a batch boundary with everything before it committed.
    """
    monkeypatch.setattr(settings, "embedding_batch_size", 4)
    provider = CountingProvider(log)
    wire(monkeypatch, [candidate(n) for n in range(20)], provider)

    outcome = await embed_pending(as_session(session), time_budget=25.0, clock=ticking(10.0))

    assert outcome.embedded == 8
    assert outcome.stopped == "budget"
    assert log == ["encode", "commit", "encode", "commit"]


@pytest.mark.unit
async def test_the_budgets_default_to_the_configured_ones(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession, log: list[str]
) -> None:
    """The cap is configuration, not a constant in the function.

    It used to be a hardcoded 500, which is why raising it meant editing code.
    """
    monkeypatch.setattr(settings, "embedding_batch_size", 2)
    monkeypatch.setattr(settings, "embedding_max_per_run", 4)
    provider = CountingProvider(log)
    wire(monkeypatch, [candidate(n) for n in range(10)], provider)

    outcome = await embed_pending(as_session(session))

    assert outcome.embedded == 4
    assert outcome.stopped == "budget"


# ── the rows that need nothing ────────────────────────────────────────


@pytest.mark.unit
async def test_an_unchanged_description_never_reaches_the_model(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession, log: list[str]
) -> None:
    """Every re-crawl rewrites updated_at, so "touched" cannot be the question.

    Without the hash check a nightly run re-encodes the whole corpus, which at
    seven seconds a posting is not a run anybody finishes.
    """
    monkeypatch.setattr(settings, "embedding_batch_size", 8)
    rows = [candidate(n, unchanged=True) for n in range(4)] + [candidate(n) for n in range(4, 8)]
    provider = CountingProvider(log)
    wire(monkeypatch, rows, provider)

    outcome = await embed_pending(as_session(session))

    assert provider.calls == [4], "only the four rows whose text moved"
    assert outcome.unchanged == 4
    assert outcome.considered == 8
    assert outcome.embedded == 4
    assert outcome.stopped == "drained"


@pytest.mark.unit
async def test_a_window_of_rows_that_need_nothing_does_not_loop_forever(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession, log: list[str]
) -> None:
    """The termination hazard, and the starvation it exposes.

    A row skipped as unchanged is never written, so it never leaves the
    selection and the next window returns it again. ``needs_embedding`` orders
    by ``last_seen_at`` and applies its limit before anything knows whether a
    row's text moved, so a crawl that re-touches more rows than one window holds
    fills every window with rows that need nothing — and the un-embedded
    backlog behind them is unreachable.

    This asserts the step notices and stops rather than spinning. It does not
    assert the backlog gets embedded, because from here it cannot be: fixing
    that means ordering un-embedded rows first, which lives in the repository.
    But it does assert the four hidden rows are *counted*, because an operator
    who is told a starved run left nothing behind has been told the same lie the
    old step told.
    """
    monkeypatch.setattr(step, "SELECT_WINDOW", 4)
    monkeypatch.setattr(settings, "embedding_batch_size", 4)
    rows = [candidate(n, unchanged=True) for n in range(4)] + [candidate(n) for n in range(4, 8)]
    provider = CountingProvider(log)
    vacancies = wire(monkeypatch, rows, provider)

    outcome = await embed_pending(as_session(session))

    assert outcome.stopped == "starved"
    assert outcome.embedded == 0
    assert outcome.backlog == 4, "the four rows the window will never reach"
    assert len(vacancies.outstanding()) - outcome.unchanged == outcome.backlog
    assert provider.calls == []


@pytest.mark.unit
async def test_a_short_window_of_rows_that_need_nothing_is_simply_drained(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession, log: list[str]
) -> None:
    """The same shape without a full window is the ordinary end of a quiet run.

    Nothing is hidden behind a window that did not fill, so this is "everything
    is up to date", not "we cannot see past the noise", and it must not raise a
    warning that would cry wolf on every nightly run.
    """
    monkeypatch.setattr(step, "SELECT_WINDOW", 8)
    provider = CountingProvider(log)
    wire(monkeypatch, [candidate(n, unchanged=True) for n in range(4)], provider)

    outcome = await embed_pending(as_session(session))

    assert outcome.stopped == "drained"
    assert outcome.unchanged == 4
    assert outcome.backlog == 0


@pytest.mark.unit
async def test_a_full_window_of_rows_that_need_nothing_is_not_called_starved(
    monkeypatch: pytest.MonkeyPatch,
    session: FakeSession,
    log: list[str],
) -> None:
    """Exactly ``SELECT_WINDOW`` rows outstanding, all of them up to date.

    ``len(window) >= SELECT_WINDOW`` cannot tell this apart from a window with
    a hidden backlog behind it, and used to call both starved — telling an
    operator "the backlog behind them is unreachable" when there was no backlog
    behind them at all. The count can tell them apart, so this must come out as
    an ordinary drained run with no warning.
    """
    monkeypatch.setattr(step, "SELECT_WINDOW", 4)
    provider = CountingProvider(log)
    vacancies = wire(monkeypatch, [candidate(n, unchanged=True) for n in range(4)], provider)

    with capture_logs() as logs:
        outcome = await embed_pending(as_session(session))

    assert outcome.stopped == "drained"
    assert outcome.unchanged == 4
    assert outcome.backlog == 0
    assert len(vacancies.outstanding()) == 4, "the rows are still flagged; none of them need work"
    assert not [entry for entry in logs if entry["event"].endswith("starved")]


@pytest.mark.unit
async def test_a_re_crawls_churn_behind_a_full_window_is_not_called_starved(
    monkeypatch: pytest.MonkeyPatch,
    session: FakeSession,
    log: list[str],
) -> None:
    """A full window of up-to-date rows with more up-to-date rows behind it.

    This is what an ordinary re-crawl produces and it is the common case, not an
    exotic one: every re-crawl bumps ``updated_at`` whether or not the
    description moved, so a quiet pass over five thousand postings leaves five
    thousand rows flagged and nothing at all to do.

    The wide count cannot see that — it reports a non-zero remainder either way —
    so a version of this step that decided on the wide count alone warned that
    "the backlog behind them is unreachable" on every nightly run over a corpus
    larger than one window. The narrow count settles it: nothing here has ever
    gone un-embedded, so there is nothing behind the window and nothing to say.
    """
    monkeypatch.setattr(step, "SELECT_WINDOW", 4)
    monkeypatch.setattr(settings, "embedding_batch_size", 4)
    provider = CountingProvider(log)
    vacancies = wire(monkeypatch, [candidate(n, unchanged=True) for n in range(8)], provider)

    with capture_logs() as logs:
        outcome = await embed_pending(as_session(session))

    assert outcome.stopped == "drained", "churn is not starvation"
    assert not [entry for entry in logs if entry["event"].endswith("starved")]
    # The remainder is still reported honestly — it is a ceiling on the work
    # left, and four rows really are still flagged — but it is not an alarm.
    assert outcome.backlog == 4
    assert await vacancies.count_never_embedded() == 0
    assert provider.calls == []


@pytest.mark.unit
async def test_rows_with_no_vector_behind_a_full_window_are_still_called_starved(
    monkeypatch: pytest.MonkeyPatch,
    session: FakeSession,
    log: list[str],
) -> None:
    """The other side of the same decision, so the narrowing cannot silence it.

    Same shape as the churn case, except the rows behind the window have never
    been embedded. That is the real defect in the selection's ordering and it
    must still be reported, or narrowing the diagnosis would have turned a true
    alarm off along with the false one.
    """
    monkeypatch.setattr(step, "SELECT_WINDOW", 4)
    monkeypatch.setattr(settings, "embedding_batch_size", 4)
    rows = [candidate(n, unchanged=True) for n in range(4)] + [candidate(n) for n in range(4, 8)]
    provider = CountingProvider(log)
    vacancies = wire(monkeypatch, rows, provider)

    with capture_logs() as logs:
        outcome = await embed_pending(as_session(session))

    assert outcome.stopped == "starved"
    assert [entry for entry in logs if entry["event"].endswith("starved")]
    assert await vacancies.count_never_embedded() == 4


# ── the report ────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_the_outcome_says_how_much_is_left(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession, log: list[str]
) -> None:
    """Without this number a stopped run and a finished run look identical.

    That is how zero embeddings against 466 vacancies went unnoticed: the run
    reported found, new and duplicates, all of them healthy.
    """
    monkeypatch.setattr(settings, "embedding_batch_size", 4)
    provider = CountingProvider(log)
    wire(monkeypatch, [candidate(n) for n in range(20)], provider)

    outcome = await embed_pending(as_session(session), limit=4)

    assert outcome.embedded == 4
    assert outcome.backlog == 16


@pytest.mark.unit
async def test_a_remainder_behind_a_full_window_is_the_real_remainder(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession, log: list[str]
) -> None:
    """A full window used to mean the number was a floor. Now it is the count.

    The window is a page, and what is on the page says nothing about the size of
    the book. The step asks for a count instead, so 36 outstanding rows are
    reported as 36 whether or not they fit in one selection.
    """
    monkeypatch.setattr(step, "SELECT_WINDOW", 8)
    monkeypatch.setattr(settings, "embedding_batch_size", 4)
    provider = CountingProvider(log)
    vacancies = wire(monkeypatch, [candidate(n) for n in range(40)], provider)

    outcome = await embed_pending(as_session(session), limit=4)

    assert outcome.embedded == 4
    assert outcome.backlog == 36
    assert outcome.backlog == len(vacancies.outstanding())
    assert vacancies.counts == 1, "one count for the whole run, not one per batch"


@pytest.mark.unit
async def test_a_row_cap_landing_on_a_window_boundary_reports_the_whole_backlog(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession, log: list[str]
) -> None:
    """The path every large backlog takes on the shipped defaults.

    ``embedding_max_per_run`` is 2000 and ``SELECT_WINDOW`` is 200, so the row
    cap lands exactly on a window boundary: the loop finishes the window's last
    batch, falls out of the batch loop and stops there rather than inside it.
    That branch used to hardcode the remainder to zero and flag it "not known",
    which the script rendered as "осталось не менее 0" — for the one case the
    number exists to describe. Scaled down here to 8 rows a cap and 4 a window,
    with 40 outstanding.
    """
    monkeypatch.setattr(step, "SELECT_WINDOW", 4)
    monkeypatch.setattr(settings, "embedding_batch_size", 4)
    provider = CountingProvider(log)
    vacancies = wire(monkeypatch, [candidate(n) for n in range(40)], provider)

    outcome = await embed_pending(as_session(session), limit=8)

    assert outcome.stopped == "budget", "the cap stopped it after the second window"
    assert outcome.embedded == 8
    assert outcome.embedded % 4 == 0, "and it stopped on a window boundary, not inside one"
    assert len(vacancies.written) == 8
    assert len(vacancies.outstanding()) == 32, "what is really left"
    assert outcome.backlog == 32, "and what the run says is left"


@pytest.mark.unit
async def test_a_budget_spent_exactly_on_a_whole_window_still_stops(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession, log: list[str]
) -> None:
    """The awkward boundary: the cap lands on the last row of the window.

    Without a check after the batches the loop would go round, select again and
    call the result "drained" — reporting an empty backlog while twelve rows sit
    there with no vector. The stop has to be labelled by why it happened, not by
    what the next query happens to return.
    """
    monkeypatch.setattr(step, "SELECT_WINDOW", 8)
    monkeypatch.setattr(settings, "embedding_batch_size", 4)
    provider = CountingProvider(log)
    vacancies = wire(monkeypatch, [candidate(n) for n in range(20)], provider)

    outcome = await embed_pending(as_session(session), limit=8)

    assert outcome.embedded == 8
    assert outcome.stopped == "budget"
    assert outcome.backlog == 12
    assert len(vacancies.written) == 8, "twelve rows are still waiting"
    assert vacancies.selections == [8], "and it did not go back for another window"


@pytest.mark.unit
async def test_a_run_that_drains_the_backlog_on_the_row_cap_says_nothing_is_left(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession, log: list[str]
) -> None:
    """Finished and stopped at once: the cap bites on the very last row.

    ``stopped`` is "budget" because that is why the loop ended, and it must stay
    that way — but there is nothing behind it. Deciding "is there more to do"
    from the stop reason sends the operator round again for no rows at all, and
    the script's closing line is exactly that decision.
    """
    monkeypatch.setattr(step, "SELECT_WINDOW", 4)
    monkeypatch.setattr(settings, "embedding_batch_size", 4)
    provider = CountingProvider(log)
    vacancies = wire(monkeypatch, [candidate(n) for n in range(8)], provider)

    outcome = await embed_pending(as_session(session), limit=8)

    assert outcome.stopped == "budget"
    assert outcome.embedded == 8
    assert vacancies.outstanding() == [], "every row has a vector"
    assert outcome.backlog == 0, "so the run must not ask for another pass"


@pytest.mark.unit
async def test_the_batch_count_records_what_survived(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession, log: list[str]
) -> None:
    """Batches are commits. The number is the run's durable progress, exactly."""
    monkeypatch.setattr(settings, "embedding_batch_size", 5)
    provider = CountingProvider(log)
    wire(monkeypatch, [candidate(n) for n in range(15)], provider)

    outcome = await embed_pending(as_session(session))

    assert outcome.batches == 3
    assert outcome.batches == session.commits


# ── against the real database ─────────────────────────────────────────
#
# Everything above proves the loop's arithmetic. These prove the two claims that
# are claims about PostgreSQL, and that a double is structurally incapable of
# testing: the count and the selection agree about which rows are outstanding,
# and a committed batch is still there after the run that wrote it dies.


@pytest.fixture
def real_fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deterministic provider, at the width the pgvector column declares."""
    monkeypatch.setattr(settings, "embedding_provider", "fake")


async def store(vacancies: VacancyRepository, count: int) -> tuple[UUID, ...]:
    """Write ``count`` postings with no vectors, the way a crawl would."""
    result = await vacancies.bulk_upsert(
        [
            (
                make_vacancy(
                    seed=f"embed-{index}",
                    description_raw=f"Backend engineer. Posting number {index}.",
                ),
                "fixture_source",
                f"embed-{index}",
                f"https://example.test/{index}",
                {},
            )
            for index in range(count)
        ]
    )
    await vacancies.session.flush()
    return result.vacancy_ids


async def with_vectors(session: AsyncSession, ids: Sequence[UUID] | None = None) -> int:
    """Rows that actually hold a vector, read straight out of the table."""
    stmt = select(func.count()).select_from(Vacancy).where(Vacancy.embedding.is_not(None))
    if ids is not None:
        stmt = stmt.where(Vacancy.id.in_(ids))
    return int((await session.execute(stmt)).scalar_one())


@pytest.mark.db
async def test_the_count_and_the_selection_agree_about_what_is_outstanding(
    db_session: AsyncSession, vacancies: VacancyRepository, real_fake_provider: None
) -> None:
    """One predicate, two queries, and they must never drift apart.

    ``backlog`` is only meaningful if reaching zero means the selection is
    empty. Two hand-written copies of the same ``WHERE`` would eventually
    disagree, and the disagreement would read as a step that keeps reporting
    work while finding none.
    """
    await store(vacancies, 12)

    assert await vacancies.count_needing_embedding() == 12
    assert len(await vacancies.needs_embedding(limit=1000)) == 12

    # A window is a page over the same set: capping it must not change the count.
    assert len(await vacancies.needs_embedding(limit=5)) == 5
    assert await vacancies.count_needing_embedding() == 12

    outcome = await embed_pending(db_session, limit=1000)

    assert outcome.embedded == 12
    assert outcome.backlog == 0
    assert await vacancies.count_needing_embedding() == 0
    assert await vacancies.needs_embedding(limit=1000) == []


@pytest.mark.db
async def test_a_run_stopped_by_its_budget_reports_the_real_remainder(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    real_fake_provider: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The number the operator reads, checked against the table it describes.

    Not against the loop's own bookkeeping: the whole defect was that the loop's
    bookkeeping and the database disagreed, and only one of them is the truth.
    """
    monkeypatch.setattr(settings, "embedding_batch_size", 4)
    ids = await store(vacancies, 20)

    outcome = await embed_pending(db_session, limit=8)

    assert outcome.stopped == "budget"
    assert outcome.embedded == 8
    assert await with_vectors(db_session, ids) == 8
    assert outcome.backlog == 12
    assert outcome.backlog == await vacancies.count_needing_embedding()


@pytest.mark.db
async def test_a_committed_batch_survives_the_run_that_wrote_it(
    async_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, log: list[str]
) -> None:
    """The property the whole rewrite exists for, in real transactions.

    Every other test in this file runs inside the suite's rollback wrapper,
    where ``commit`` releases a savepoint. That is fine for arithmetic and
    useless here: the claim is that work already committed is still there after
    the run dies, and a savepoint that is about to be rolled back cannot show
    it. So this one owns its transactions — a writer session with real commits,
    a second connection to look with, and a delete at the end because nothing
    rolls it back for us.

    The model fails on the third batch, exactly as an interrupt or a timeout
    would. The eight vectors the first two batches paid for have to be visible
    from a connection that never saw the writer's transaction. Under the shape
    this replaced there would be none: everything was encoded first and written
    in one statement at the very end, so the process dying anywhere before that
    left the table exactly as it found it.
    """
    monkeypatch.setattr(settings, "embedding_batch_size", 4)
    provider = CountingProvider(log, fail_on=3)
    monkeypatch.setattr(embeddings, "get_provider", lambda: provider)

    ids: tuple[UUID, ...] = ()
    try:
        async with AsyncSession(bind=async_engine, expire_on_commit=False) as writer:
            ids = await store(VacancyRepository(writer), 20)
            await writer.commit()

            with pytest.raises(RuntimeError, match="fell over"):
                await embed_pending(writer)
            # Whatever the dead run left open, thrown away — the point is what
            # it had already committed, not what it was holding.
            await writer.rollback()

        async with AsyncSession(bind=async_engine) as observer:
            assert await with_vectors(observer, ids) == 8, "two batches, committed as they landed"
            assert provider.calls == [4, 4, 4], "and a third that was encoded but never written"
    finally:
        async with AsyncSession(bind=async_engine) as cleaner:
            await cleaner.execute(sa_delete(Vacancy).where(Vacancy.id.in_(ids)))
            await cleaner.commit()


@pytest.mark.db
async def test_an_unchanged_posting_stays_flagged_and_is_still_not_a_backlog(
    db_session: AsyncSession, vacancies: VacancyRepository, real_fake_provider: None
) -> None:
    """The subtraction that makes ``backlog`` mean anything, against real SQL.

    A row whose text has not moved is never written, so ``embedded_at`` stays
    where it is and the SQL predicate keeps flagging it — forever. Reporting the
    raw count would leave an operator watching a number that can never fall to
    zero, which is why the step subtracts the rows it hashed and cleared.
    """
    await store(vacancies, 6)
    await embed_pending(db_session)

    # Age the vectors so the "written since we embedded it" test can fire at
    # all: inside one transaction PostgreSQL's now() is constant, so updated_at
    # and embedded_at come out equal. Same trick as test_pipeline_end_to_end.
    await db_session.execute(
        sa_update(Vacancy.__table__).values(
            embedded_at=Vacancy.__table__.c.embedded_at - timedelta(hours=1)
        )
    )
    await db_session.flush()

    assert await vacancies.count_needing_embedding() == 6, "all six look suspect to SQL"

    outcome = await embed_pending(db_session)

    assert outcome.considered == 6
    assert outcome.unchanged == 6, "and none of them had actually changed"
    assert outcome.embedded == 0
    assert outcome.stopped == "drained"
    assert outcome.backlog == 0, "nothing to do, even though six rows are still flagged"


@pytest.mark.db
async def test_a_row_with_no_vector_is_offered_before_one_that_merely_might_be_stale(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """The ordering the whole starvation turned on, asserted on its own.

    ``last_seen_at`` says when a posting was last advertised. It says nothing
    about whether this row needs a vector, so ordering by it alone lets a
    re-crawl decide what the selection can reach: the crawl bumps the timestamp
    on every row it sees, including thousands whose description never moved, and
    those crowd the window ahead of rows that have no vector at all.

    Measured on the live corpus, 24 Sep 2026: a chain whose crawl brought in
    1236 new postings then computed **zero** description vectors and reported
    ``stopped="starved"``, with 1704 rows holding no vector. Scoring right after
    it read 1703 vacancies without one.

    So the sort key leads with the only fact that cannot be wrong: a row with no
    vector needs one whatever its text hash turns out to be.
    """
    older, newer = await store(vacancies, 2)
    await _embed_one(db_session, newer)
    # The crawl saw the already-embedded row most recently. Under the old
    # ordering that alone was enough to put it first.
    await db_session.execute(
        sa_update(Vacancy.__table__)
        .where(Vacancy.__table__.c.id == newer)
        .values(last_seen_at=func.now(), updated_at=func.now())
    )
    await db_session.execute(
        sa_update(Vacancy.__table__)
        .where(Vacancy.__table__.c.id == older)
        .values(last_seen_at=func.now() - timedelta(days=9))
    )
    await db_session.flush()

    window = await vacancies.needs_embedding(limit=2)

    assert [row.id for row in window] == [older, newer]


@pytest.mark.db
async def test_a_backlog_behind_a_window_of_churn_is_reached_rather_than_starved(
    db_session: AsyncSession,
    vacancies: VacancyRepository,
    real_fake_provider: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live failure of 24 Sep, in miniature and against real SQL.

    A window's worth of already-embedded rows whose text never moved, all seen
    more recently than the rows that have no vector at all. The step hashes the
    window, finds nothing to do, asks again, gets *the same window* — nothing
    retires an unchanged row from the predicate — and stops. Under the old
    ordering the backlog behind it was unreachable in that call and in every
    later one, because the query has no offset and the answer never changed.

    The fix is not a bigger window: the ratio of churn to real work is a
    property of the crawl, not of the window. It is that a row with no vector
    outranks one that merely might be stale.
    """
    monkeypatch.setattr(step, "SELECT_WINDOW", 4)
    churn = await store(vacancies, 4)
    await embed_pending(db_session)
    assert await with_vectors(db_session, churn) == 4

    # Age every vector so the "written since we embedded it" arm of the
    # predicate fires, then add the backlog and make the churn look fresher.
    await db_session.execute(
        sa_update(Vacancy.__table__).values(
            embedded_at=Vacancy.__table__.c.embedded_at - timedelta(hours=1)
        )
    )
    backlog = await _store_more(vacancies, 4)
    await db_session.execute(
        sa_update(Vacancy.__table__)
        .where(Vacancy.__table__.c.id.in_(backlog))
        .values(last_seen_at=func.now() - timedelta(days=9))
    )
    await db_session.flush()
    assert await vacancies.count_never_embedded() == 4

    outcome = await embed_pending(db_session)

    assert outcome.embedded == 4, "the rows with no vector were reached"
    assert outcome.stopped != "starved"
    assert await vacancies.count_never_embedded() == 0
    assert await with_vectors(db_session, backlog) == 4


async def _embed_one(session: AsyncSession, vacancy_id: UUID) -> None:
    """Give one row a vector and a hash, the way a finished pass leaves it.

    ``embedded_at`` is backdated an hour on purpose: inside one transaction
    PostgreSQL's ``now()`` is constant, so a vector written and a row touched in
    the same test would come out with equal timestamps and the "written since we
    embedded it" arm of the predicate could never fire. Same trick the churn
    tests above use, for the same reason.
    """
    await session.execute(
        sa_update(Vacancy.__table__)
        .where(Vacancy.__table__.c.id == vacancy_id)
        .values(
            embedding=[0.0] * settings.embedding_dim,
            embedded_at=func.now() - timedelta(hours=1),
            embedding_text_hash="x" * 64,
        )
    )
    await session.flush()


async def _store_more(vacancies: VacancyRepository, count: int) -> tuple[UUID, ...]:
    """A second batch of postings with no vectors, under their own ids."""
    result = await vacancies.bulk_upsert(
        [
            (
                make_vacancy(
                    seed=f"backlog-{index}",
                    description_raw=f"Platform engineer. Backlog posting {index}.",
                ),
                "fixture_source",
                f"backlog-{index}",
                f"https://example.test/backlog/{index}",
                {},
            )
            for index in range(count)
        ]
    )
    await vacancies.session.flush()
    return result.vacancy_ids


# ── the script that drives it ─────────────────────────────────────────
#
# scripts/embed_backlog.py is where a person actually meets this step, and it is
# the layer that turns an EmbeddingOutcome into "you are done" or "run it again".
# Getting that wrong is not cosmetic: the reason the missing vectors went
# unnoticed is that the tooling said everything was fine.
#
# The pass runner is injected, so none of this needs a database or a model.


def _load_embed_backlog() -> ModuleType:
    """Import ``scripts/embed_backlog.py`` by path.

    ``scripts/`` is not a package and is not on sys.path — the script is run as
    ``python scripts/embed_backlog.py`` — so loading it by file path keeps the
    test from inventing an import route that only exists under pytest.
    """
    path = REPO_ROOT / "scripts" / "embed_backlog.py"
    spec = importlib.util.spec_from_file_location("embed_backlog_script", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backlog_script = _load_embed_backlog()

#: What the script's loop calls for each pass. Declared here rather than
#: imported: the module is loaded by path at runtime, so nothing about it is
#: statically known and mypy would take the alias as ``Any``.
type PassRunner = Callable[[argparse.Namespace], Awaitable[EmbeddingOutcome]]


def options(*, until_drained: bool = False) -> argparse.Namespace:
    """The parsed command line the script's loop reads."""
    return argparse.Namespace(max=None, seconds=None, until_drained=until_drained)


def scripted(*outcomes: EmbeddingOutcome) -> tuple[PassRunner, list[int]]:
    """A pass runner returning each outcome in turn, then repeating the last."""
    calls: list[int] = []

    async def run_pass(_args: argparse.Namespace) -> EmbeddingOutcome:
        calls.append(len(calls) + 1)
        return outcomes[min(len(calls) - 1, len(outcomes) - 1)]

    return run_pass, calls


DRAINED = EmbeddingOutcome(considered=4, unchanged=0, embedded=4, batches=1, backlog=0)
CAPPED = EmbeddingOutcome(
    considered=8, unchanged=2, embedded=6, batches=2, backlog=40, stopped="budget"
)
STARVED = EmbeddingOutcome(considered=0, unchanged=0, embedded=0, backlog=13, stopped="starved")
MISSING_MODEL = EmbeddingOutcome(
    considered=8,
    unchanged=0,
    embedded=0,
    skipped_reason="runtime not installed",
    backlog=8,
    stopped="unavailable",
)


@pytest.mark.unit
async def test_the_script_says_it_is_done_when_nothing_is_left(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A drained pass ends with "done" and exit code 0."""
    run_pass, calls = scripted(DRAINED)

    code = await backlog_script.drain(options(), run_pass=run_pass)

    assert code == 0
    assert calls == [1]
    assert "Готово" in capsys.readouterr().out


@pytest.mark.unit
async def test_the_script_does_not_ask_for_another_pass_when_the_cap_drained_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stopped on the budget with nothing behind it is finished, not unfinished.

    A run that embeds its last row and hits the row cap in the same breath
    reports ``stopped="budget"``, which is true and must stay true. Reading the
    stop reason rather than the remainder made the script tell an operator with
    an empty backlog to run it again — and that message is indistinguishable
    from the real one, so it trains people to ignore both.
    """
    run_pass, calls = scripted(
        EmbeddingOutcome(
            considered=8, unchanged=0, embedded=8, batches=2, backlog=0, stopped="budget"
        )
    )

    code = await backlog_script.drain(options(until_drained=True), run_pass=run_pass)
    printed = capsys.readouterr().out

    assert code == 0
    assert calls == [1], "and it did not start a second pass for no rows"
    assert "Готово" in printed
    assert "ещё раз" not in printed


@pytest.mark.unit
async def test_until_drained_keeps_going_while_the_budget_stops_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The loop's reason to exist: many capped passes, one command."""
    calls: list[int] = []

    async def run_pass(_args: argparse.Namespace) -> EmbeddingOutcome:
        calls.append(len(calls) + 1)
        return CAPPED if len(calls) < 3 else DRAINED

    code = await backlog_script.drain(options(until_drained=True), run_pass=run_pass)
    printed = capsys.readouterr().out

    assert code == 0
    assert calls == [1, 2, 3]
    assert printed.count("ПРОХОД") == 3, "one report per pass, numbered"
    assert "Готово" in printed


@pytest.mark.unit
async def test_one_pass_is_the_default_and_says_how_much_is_left(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without ``--until-drained`` a capped pass reports and stops."""
    run_pass, calls = scripted(CAPPED)

    code = await backlog_script.drain(options(), run_pass=run_pass)
    printed = capsys.readouterr().out

    assert code == 0
    assert calls == [1]
    assert "40" in printed
    assert "ещё раз" in printed


@pytest.mark.unit
async def test_a_pass_that_writes_nothing_does_not_spin_forever(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--until-drained`` against a pass that makes no progress must terminate.

    A budget spent without a single vector written repeats identically, so
    looping on it is an infinite loop that looks like work.
    """
    run_pass, calls = scripted(
        EmbeddingOutcome(
            considered=8, unchanged=8, embedded=0, batches=0, backlog=40, stopped="budget"
        )
    )

    code = await backlog_script.drain(options(until_drained=True), run_pass=run_pass)

    assert code == 0
    assert calls == [1], "the zero-progress guard fires on the pass that made none"
    assert "Останавливаюсь" in capsys.readouterr().out


@pytest.mark.unit
async def test_a_missing_model_is_an_error_exit_with_the_command_to_fix_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-zero, because nothing was computed and nobody should think otherwise."""
    run_pass, calls = scripted(MISSING_MODEL)

    code = await backlog_script.drain(options(until_drained=True), run_pass=run_pass)
    printed = capsys.readouterr().out

    assert code == 1
    assert calls == [1], "no point starting another pass against a model that is not there"
    assert "uv sync --extra embeddings" in printed


@pytest.mark.unit
async def test_a_starved_run_does_not_tell_the_operator_to_try_again(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Repeating a starved pass reproduces it exactly; saying so is the fix."""
    run_pass, _ = scripted(STARVED)

    code = await backlog_script.drain(options(), run_pass=run_pass)
    printed = capsys.readouterr().out

    assert code == 0
    assert "13" in printed
    assert "ещё раз" not in printed
    assert "needs_embedding" in printed


@pytest.mark.unit
@pytest.mark.parametrize(
    "outcome",
    [DRAINED, CAPPED, STARVED, MISSING_MODEL],
    ids=["drained", "budget", "starved", "unavailable"],
)
async def test_every_line_the_script_prints_survives_a_cp1251_console(
    outcome: EmbeddingOutcome, capsys: pytest.CaptureFixture[str]
) -> None:
    """The console this is run on encodes cp1251, and one stray glyph is fatal.

    Not a style rule. A box-drawing character or an emoji raises
    UnicodeEncodeError at the ``print`` — after the work, which on a full pass
    is an hour of it, and after the vectors are safely committed but before
    anybody is told what happened. Checked over the per-pass report and the
    closing line, for every ending the script has.
    """
    await backlog_script.drain(options(), run_pass=scripted(outcome)[0])

    printed = capsys.readouterr().out
    assert printed.strip(), "there is a report to check"
    printed.encode("cp1251")
