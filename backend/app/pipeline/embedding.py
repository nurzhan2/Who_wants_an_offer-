"""Vectors for crawled postings, computed once at the end of a run.

Three rules, and each of them exists because the obvious alternative turns a
run into hours.

**Batched, never per posting.** ``encode_texts`` already chunks by
``settings.embedding_batch_size``; calling it once per vacancy makes every call
a batch of one and defeats the batching entirely. So the whole run makes one
call.

**After deduplication, never during the crawl.** A job cross-posted to four
boards is one row, and encoding it while each connector yields it would pay for
the same vector four times. The step runs when the writing is done and reads the
deduplicated rows back.

**Never for an unchanged description.** Every re-crawl rewrites ``updated_at``
whether or not the text moved, so "the row was touched" is not the question.
The vector's own text is hashed and stored beside it, and a row whose text
hashes the same is skipped without the model ever seeing it.

The step is also allowed to do nothing. ``sentence-transformers`` is an optional
extra that CI does not install, and a run that crawled successfully must not be
reported as failed because it could not embed — the vectors are recomputed on
the next run, and every non-semantic part of the product still works.
"""

import hashlib
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.repositories.vacancy import EmbeddedVacancy, VacancyRepository
from app.matching.embeddings import EmbeddingError, encode_texts, vacancy_text

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class EmbeddingOutcome:
    """What the embedding step did, for the run report."""

    #: Rows the cheap SQL narrowing flagged as possibly stale.
    considered: int
    #: Rows whose text had not actually changed, so the model was not called.
    unchanged: int
    #: Vectors computed and written.
    embedded: int
    #: Why nothing was computed, when that is the answer. Not an error.
    skipped_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _Pending:
    """One posting whose text has moved since its vector was computed."""

    id: UUID
    text: str
    digest: str


def text_hash(text: str) -> str:
    """The hash stored beside a vector, over the exact text it was built from."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def embed_pending(session: AsyncSession, *, limit: int = 500) -> EmbeddingOutcome:
    """Compute and store the vectors this run left outstanding."""
    vacancies = VacancyRepository(session)
    candidates = await vacancies.needs_embedding(limit=limit)
    if not candidates:
        return EmbeddingOutcome(considered=0, unchanged=0, embedded=0)

    pending: list[_Pending] = []
    unchanged = 0
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
            unchanged += 1
            continue
        pending.append(_Pending(id=candidate.id, text=text, digest=digest))

    if not pending:
        logger.info("pipeline.embedding.nothing_changed", considered=len(candidates))
        return EmbeddingOutcome(considered=len(candidates), unchanged=unchanged, embedded=0)

    try:
        # One call. It chunks internally and reads the on-disk cache, so a
        # posting whose text we have embedded before costs nothing here either.
        vectors = await encode_texts([item.text for item in pending])
    except EmbeddingError as exc:
        # The runtime is optional and its absence is a known state, not a
        # failure of the crawl that just succeeded.
        logger.warning("pipeline.embedding.unavailable", error=str(exc))
        return EmbeddingOutcome(
            considered=len(candidates),
            unchanged=unchanged,
            embedded=0,
            skipped_reason=str(exc),
        )

    # strict=True because encode_texts promises index alignment, and a silent
    # length mismatch here would attach vectors to the wrong postings.
    written = await vacancies.set_embeddings(
        [
            EmbeddedVacancy(id=item.id, vector=vector, text_hash=item.digest)
            for item, vector in zip(pending, vectors, strict=True)
        ]
    )
    logger.info(
        "pipeline.embedding.done",
        considered=len(candidates),
        unchanged=unchanged,
        embedded=written,
    )
    return EmbeddingOutcome(considered=len(candidates), unchanged=unchanged, embedded=written)
