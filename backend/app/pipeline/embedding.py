"""Vectors for crawled postings: computed in batches, committed as they go.

Four rules, and each of them exists because the obvious alternative either turns
a run into hours or throws away the hours it already spent.

**Batched, never per posting.** ``encode_texts`` chunks by
``settings.embedding_batch_size``; calling it once per vacancy makes every call
a batch of one and defeats the batching entirely.

**After deduplication, never during the crawl.** A job cross-posted to four
boards is one row, and encoding it while each connector yields it would pay for
the same vector four times. The step runs when the writing is done and reads the
deduplicated rows back.

**Never for an unchanged description.** Every re-crawl rewrites ``updated_at``
whether or not the text moved, so "the row was touched" is not the question.
The vector's own text is hashed and stored beside it, and a row whose text
hashes the same is skipped without the model ever seeing it.

**Committed batch by batch, never once at the end.** This is the rule the first
version of this module got wrong, and getting it wrong cost the entire backlog.
Measured on the owner's machine, bge-m3 on CPU costs about seven seconds a
posting: 466 rows is 54 minutes, and the corpus behind a single city is over
thirteen thousand, which is more than a day. The old shape computed every
outstanding vector in one ``encode_texts`` call and wrote them in one statement
at the very end, so anything that ended the process first — a keyboard
interrupt, an HTTP timeout, a source raising past ``asyncio.gather`` — discarded
every vector it had just computed. What that looks like from outside is a
database holding 466 vacancies and no vectors at all while
``.cache/embeddings`` holds 466 of them, which is precisely what was observed.
Now each batch is written and committed before the next is encoded, so an
interrupted run keeps everything it earned and the next one resumes from there.

Committing the caller's session is therefore deliberate rather than a leak of
concern: durability per batch *is* the behaviour, and a caller that wanted one
all-or-nothing transaction would be asking for the bug back.

**The remainder is counted, never inferred.** A run that stops on a budget has
to say how much is left, and the only number a windowed selection can offer is
what was left of the current window — which on the shipped defaults is zero,
because the row cap (2000) is a whole number of windows (200). Reporting that as
the backlog would tell an operator with thirteen thousand postings outstanding
that nothing was. So the step ends with one unlimited ``COUNT`` over the same
predicate the selection uses.

The step is also allowed to do nothing. ``sentence-transformers`` is an optional
extra that CI does not install, and a run that crawled successfully must not be
reported as failed because it could not embed — the vectors are recomputed on
the next run, and every non-semantic part of the product still works.
"""

import hashlib
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.repositories.vacancy import EmbeddedVacancy, EmbeddingCandidate, VacancyRepository
from app.matching.embeddings import EmbeddingError, encode_texts, vacancy_text

logger = get_logger(__name__)

#: Wall clock, injectable for the same reason ``BaseSource.is_due`` takes its
#: own: a budget that can only be tested against the real clock can only be
#: tested by sleeping through it.
type Clock = Callable[[], float]

#: Why the step stopped. Reported rather than inferred, because "embedded 0" has
#: four completely different meanings and only one of them is fine.
type StopReason = Literal["drained", "budget", "starved", "unavailable"]

#: Rows one selection asks for. Deliberately larger than a model batch: the SQL
#: narrowing returns rows that only *may* be stale, and the ones whose text
#: turns out not to have moved are dropped here, so a window sized to the model
#: batch would routinely come back with nothing to do. Bounded because every row
#: carries its description and a window is held in memory whole.
SELECT_WINDOW = 200


@dataclass(frozen=True, slots=True)
class EmbeddingOutcome:
    """What the embedding step did, for the run report."""

    #: Rows the cheap SQL narrowing offered, each counted once.
    considered: int
    #: Rows whose text had not actually changed, so the model was not called.
    unchanged: int
    #: Vectors computed and written.
    embedded: int
    #: Why nothing was computed, when that is the answer. Not an error.
    skipped_reason: str | None = None
    #: Commits. Each one is work that survives the process dying after it, which
    #: is the difference between this step and the one it replaced.
    batches: int = 0
    #: Rows still outstanding when the step stopped, counted in the database
    #: rather than inferred from the last window: everything the selection still
    #: flags, less the rows this pass hashed and proved unchanged.
    #:
    #: It is what the next pass starts from. It is a CEILING on the work left,
    #: not a promise about model calls and not a count that tracks the selection:
    #: rows nobody has hashed yet will turn out unchanged too, so this can be
    #: non-zero while the very next selection has nothing to do — an earlier
    #: version of this comment claimed it "reaches zero exactly when the
    #: selection is empty", and the step's own tests show both directions of
    #: that being false. When it matters whether real work is outstanding, the
    #: question is ``count_never_embedded``, which is what ``stopped`` uses to
    #: decide between a drained run and a starved one.
    backlog: int = 0
    #: Why the loop ended.
    stopped: StopReason = "drained"


@dataclass(frozen=True, slots=True)
class _Pending:
    """One posting whose text has moved since its vector was computed."""

    id: UUID
    text: str
    digest: str


@dataclass(slots=True)
class _Progress:
    """Running totals while the loop works through the backlog."""

    considered: int = 0
    unchanged: int = 0
    embedded: int = 0
    batches: int = 0


def text_hash(text: str) -> str:
    """The hash stored beside a vector, over the exact text it was built from."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def embed_pending(
    session: AsyncSession,
    *,
    limit: int | None = None,
    time_budget: float | None = None,
    clock: Clock = time.monotonic,
) -> EmbeddingOutcome:
    """Work through the outstanding vectors, committing every batch as it lands.

    ``limit`` caps the vectors computed and ``time_budget`` the wall clock;
    both default to the configured values, and both are checked *between*
    batches so a batch is never abandoned half-computed. Two budgets rather than
    one because neither bounds the other: a row served from the disk cache costs
    microseconds and a cold one costs seconds, so a count says nothing about the
    time and a deadline says nothing about how much got done.

    Returns when the backlog is empty, when a budget is spent, or when the
    selection stops offering rows this call has not already examined. Every one
    of those endings pays for one count query so the outcome can say what is
    actually left rather than what was left of the last window.
    """
    vacancies = VacancyRepository(session)
    max_vectors = settings.embedding_max_per_run if limit is None else limit
    seconds = settings.embedding_time_budget_seconds if time_budget is None else time_budget
    deadline = clock() + seconds
    batch_size = settings.embedding_batch_size

    progress = _Progress()
    # Ids already looked at during this call. The selection is a *predicate*,
    # not a queue: a row whose text had not moved is left untouched on purpose,
    # so it is offered again by the very next window. Without this set the loop
    # would re-examine it forever.
    examined: set[UUID] = set()

    while True:
        window = await vacancies.needs_embedding(limit=SELECT_WINDOW)
        if not window:
            return await _finish(vacancies, progress, "drained")
        fresh = [row for row in window if row.id not in examined]
        if not fresh:
            return await _exhausted(vacancies, progress, window=len(window))

        examined.update(row.id for row in fresh)
        progress.considered += len(fresh)
        pending = _changed(fresh, progress)

        written_here = 0
        for batch in _batches(pending, batch_size):
            if progress.embedded >= max_vectors or clock() >= deadline:
                return await _finish(vacancies, progress, "budget")
            try:
                # One call per batch, not one per run. The provider still sees
                # batches of exactly ``embedding_batch_size``, so nothing about
                # the batching is lost — only the point at which the work
                # becomes durable moves, from the end of the run to here.
                vectors = await encode_texts([item.text for item in batch])
            except EmbeddingError as exc:
                # The runtime is optional and its absence is a known state, not
                # a failure of the crawl that just succeeded.
                logger.warning("pipeline.embedding.unavailable", error=str(exc))
                return await _finish(vacancies, progress, "unavailable", skipped_reason=str(exc))

            # strict=True because encode_texts promises index alignment, and a
            # silent length mismatch here would attach vectors to the wrong
            # postings.
            progress.embedded += await vacancies.set_embeddings(
                [
                    EmbeddedVacancy(id=item.id, vector=vector, text_hash=item.digest)
                    for item, vector in zip(batch, vectors, strict=True)
                ]
            )
            await session.commit()
            progress.batches += 1
            written_here += len(batch)
            # Logged per batch, not per run. At seven seconds a posting a run is
            # an hour long, and a step that says nothing until it finishes is
            # indistinguishable from one that has hung.
            logger.info(
                "pipeline.embedding.batch",
                embedded=progress.embedded,
                batches=progress.batches,
                remaining_in_window=len(pending) - written_here,
            )

        if progress.embedded >= max_vectors or clock() >= deadline:
            # The awkward one. With the shipped defaults the row cap is a whole
            # number of windows (2000 = 10 x 200), so a large backlog *always*
            # stops here rather than inside the batch loop — which is why the
            # remainder has to come from a count and not from what is left of
            # the current window, of which there is nothing.
            return await _finish(vacancies, progress, "budget")


async def embed_pending_titles(
    session: AsyncSession,
    *,
    limit: int | None = None,
    time_budget: float | None = None,
    clock: Clock = time.monotonic,
) -> EmbeddingOutcome:
    """Vectors for titles, the same way :func:`embed_pending` does descriptions.

    Simpler than its sibling in one respect: the selection is exact
    (``_needs_title_embedding`` hashes the title in SQL), so a row leaves it the
    moment its vector is written and a window never holds rows with nothing to
    do. Committed batch by batch for the reason the module docstring gives.
    The text embedded is the stored title exactly, because that is what the
    selection hashes.
    """
    vacancies = VacancyRepository(session)
    max_vectors = settings.embedding_max_per_run if limit is None else limit
    seconds = settings.embedding_time_budget_seconds if time_budget is None else time_budget
    deadline = clock() + seconds
    progress = _Progress()

    while True:
        if progress.embedded >= max_vectors or clock() >= deadline:
            return await _finish_titles(vacancies, progress, "budget")
        size = min(settings.embedding_batch_size, max_vectors - progress.embedded)
        batch = await vacancies.needs_title_embedding(limit=size)
        if not batch:
            return await _finish_titles(vacancies, progress, "drained")
        progress.considered += len(batch)
        try:
            vectors = await encode_texts([item.title for item in batch])
        except EmbeddingError as exc:
            logger.warning("pipeline.title_embedding.unavailable", error=str(exc))
            return await _finish_titles(vacancies, progress, "unavailable", skipped_reason=str(exc))
        progress.embedded += await vacancies.set_title_embeddings(
            [
                EmbeddedVacancy(id=item.id, vector=vector, text_hash=text_hash(item.title))
                for item, vector in zip(batch, vectors, strict=True)
            ]
        )
        await session.commit()
        progress.batches += 1
        logger.info(
            "pipeline.title_embedding.batch", embedded=progress.embedded, batches=progress.batches
        )


async def _finish_titles(
    vacancies: VacancyRepository,
    progress: _Progress,
    reason: StopReason,
    *,
    skipped_reason: str | None = None,
) -> EmbeddingOutcome:
    """End a title pass with the counted remainder, which is exact here."""
    return _stop(
        progress,
        reason,
        backlog=await vacancies.count_needing_title_embedding(),
        skipped_reason=skipped_reason,
    )


def _changed(candidates: Sequence[EmbeddingCandidate], progress: _Progress) -> list[_Pending]:
    """Split a window into the rows whose text actually moved, counting the rest."""
    pending: list[_Pending] = []
    for candidate in candidates:
        text = vacancy_text(
            title=candidate.title,
            company=candidate.company,
            city=candidate.city,
            description=candidate.description,
        )
        digest = text_hash(text)
        if candidate.stored_hash == digest:
            # Seen again and rewritten by the upsert, but the words are the same.
            progress.unchanged += 1
            continue
        pending.append(_Pending(id=candidate.id, text=text, digest=digest))
    return pending


def _batches(pending: Sequence[_Pending], size: int) -> Iterator[list[_Pending]]:
    """Split the outstanding rows into units of one commit."""
    for start in range(0, len(pending), size):
        yield list(pending[start : start + size])


async def _outstanding(vacancies: VacancyRepository, progress: _Progress) -> int:
    """Rows still waiting, counted rather than guessed at from the last window.

    One unlimited count against the same predicate the selection uses, minus the
    rows this pass hashed and cleared. Those stay flagged forever — nothing is
    written for a row whose text has not moved, so ``updated_at > embedded_at``
    keeps holding — and counting them as outstanding would leave an operator
    watching a number that can never reach zero.

    One query per run, not per batch: the loop already spends seconds a row.
    """
    flagged = await vacancies.count_needing_embedding()
    # Clamped because another process may embed a row between the pass hashing
    # it and this count; a negative remainder would be nonsense on a terminal.
    return max(0, flagged - progress.unchanged)


async def _exhausted(
    vacancies: VacancyRepository, progress: _Progress, *, window: int
) -> EmbeddingOutcome:
    """End a run whose selection keeps offering rows that need no vector.

    Whether that is fine depends on what is behind the window, which the window
    itself cannot say — a full window of rows that need nothing is the ordinary
    end of a quiet run when the outstanding set happens to be exactly that size,
    and a starved run when it is not. Guessing from ``len(window)`` alone cries
    wolf on the first of those.

    The wide count does not settle it either, and an earlier version of this
    function believed it did. ``needs_embedding``'s predicate flags every row
    written since its vector was computed, and a re-crawl bumps ``updated_at``
    on rows whose description never moved — so a quiet re-crawl of five thousand
    postings leaves five thousand rows flagged and nothing whatsoever to do. Read
    that remainder as evidence of starvation and the operator is warned that a
    backlog is unreachable when there is no backlog.

    ``count_never_embedded`` is the question that actually distinguishes them: a
    row with no vector at all needs one whatever its text hash says. So a full
    window of already-current rows plus rows that have never been embedded means
    the selection really is starving them — it orders by ``last_seen_at`` and
    applies its limit before anything knows whether a row's text moved, so the
    churn crowds the window and the un-embedded rows behind it are unreachable.
    That is a defect in the selection, not here, and it earns a warning. A full
    window of already-current rows with nothing un-embedded behind them is just
    a drained run with some churn in the count, and it earns silence.
    """
    backlog = await _outstanding(vacancies, progress)
    if backlog == 0:
        return _stop(progress, "drained", backlog=0)
    without_a_vector = await vacancies.count_never_embedded()
    if without_a_vector == 0:
        # Everything flagged already has a vector; the remainder is re-crawl
        # churn that the next pass will hash and clear. Nothing is stuck.
        return _stop(progress, "drained", backlog=backlog)
    logger.warning(
        "pipeline.embedding.starved",
        window=window,
        backlog=backlog,
        without_a_vector=without_a_vector,
        detail="every row on offer was already up to date, and rows with no vector sit behind them",
    )
    return _stop(progress, "starved", backlog=backlog)


async def _finish(
    vacancies: VacancyRepository,
    progress: _Progress,
    reason: StopReason,
    *,
    skipped_reason: str | None = None,
) -> EmbeddingOutcome:
    """End the run, asking the database how much of the backlog is left."""
    return _stop(
        progress,
        reason,
        backlog=await _outstanding(vacancies, progress),
        skipped_reason=skipped_reason,
    )


def _stop(
    progress: _Progress,
    reason: StopReason,
    *,
    backlog: int,
    skipped_reason: str | None = None,
) -> EmbeddingOutcome:
    """Freeze the running totals into the outcome the report reads."""
    logger.info(
        "pipeline.embedding.done",
        stopped=reason,
        considered=progress.considered,
        unchanged=progress.unchanged,
        embedded=progress.embedded,
        batches=progress.batches,
        backlog=backlog,
    )
    return EmbeddingOutcome(
        considered=progress.considered,
        unchanged=progress.unchanged,
        embedded=progress.embedded,
        skipped_reason=skipped_reason,
        batches=progress.batches,
        backlog=backlog,
        stopped=reason,
    )
