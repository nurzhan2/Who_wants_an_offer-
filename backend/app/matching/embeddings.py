"""Dense embeddings: providers, batching, and the on-disk vector cache.

Three things here are less obvious than they look.

* **The model is never loaded at import time.** ``sentence-transformers`` is an
  optional extra (torch and friends, ~1.5 GB, plus 2.3 GB of bge-m3 weights).
  Importing this module on a laptop or in CI that never installed the extra has
  to work, so the import happens inside the method that first needs a vector,
  behind a lazily-initialised singleton.
* **Inference never runs on the event loop.** ``SentenceTransformer.encode`` is
  blocking CPU work measured in hundreds of milliseconds; called directly it
  would freeze every other request for that time, so it runs in a worker thread.
* **Vectors are cached on disk.** A pipeline run re-encodes thousands of
  vacancies whose text has not changed since yesterday; at ~50 ms each that is
  minutes of pure repetition per run.
"""

import asyncio
import hashlib
import importlib
import importlib.util
import math
import os
import struct
from collections.abc import Iterator, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from app.core.config import settings
from app.core.exceptions import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Import name of the optional extra backing :class:`BGEM3Provider`.
SENTENCE_TRANSFORMERS = "sentence_transformers"

#: Cached vectors are little-endian float32 — the precision the model itself
#: produces, and a fixed byte order so a cache directory stays readable when it
#: is copied between machines.
_FLOAT_BYTES = 4
_CACHE_SUFFIX = ".vec"


class EmbeddingError(AppError):
    """A vector could not be produced, or came back the wrong shape."""

    title = "Embedding failed"
    problem_type = "embedding-error"


class EmbeddingsUnavailableError(EmbeddingError):
    """Semantic scoring was asked for on an install without the model runtime."""

    title = "Embeddings are not available"
    problem_type = "embeddings-unavailable"


class EmbeddingProvider(Protocol):
    """Anything that can turn texts into vectors of ``settings.embedding_dim``."""

    name: str

    async def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per input text, in the same order."""
        ...


class FakeEmbeddingProvider:
    """Deterministic pseudo-embeddings derived from a digest of the text.

    Not an approximation of the real model — nothing about the geometry is
    meaningful. It exists so that tests, and anyone who does not want four
    gigabytes on disk, can exercise every code path that handles vectors:
    the same text always yields the same unit vector, different texts yield
    different ones, and the width matches the pgvector column.
    """

    name: str = "fake"

    async def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one deterministic unit vector per text."""
        return [self.vector_for(text) for text in texts]

    @staticmethod
    def vector_for(text: str) -> list[float]:
        """Expand sha256(text) into a unit vector of the configured width."""
        dim = settings.embedding_dim
        raw = bytearray()
        seed = text.encode("utf-8")
        counter = 0
        while len(raw) < dim * _FLOAT_BYTES:
            raw += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            counter += 1

        # Map each word onto [-1, 1) rather than [0, 1): vectors confined to the
        # positive orthant all point roughly the same way, so every pair of
        # unrelated texts would score as highly similar.
        values = [
            int.from_bytes(raw[i * _FLOAT_BYTES : (i + 1) * _FLOAT_BYTES], "big") / 2**31 - 1.0
            for i in range(dim)
        ]
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0.0:  # pragma: no cover - needs a sha256 collision with zero
            return [1.0] + [0.0] * (dim - 1)
        return [value / norm for value in values]


class BGEM3Provider:
    """The real model, loaded once per process and used from a worker thread."""

    name: str = "bge-m3"

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name or settings.embedding_model
        # ``Any`` because sentence-transformers is an optional extra: its types
        # cannot be imported here, since on most installs there is nothing for
        # the type checker to resolve.
        self._model: Any = None
        self._lock = asyncio.Lock()

    async def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """Encode texts, loading the model on first use."""
        if not texts:
            return []
        model = await self._model_once()
        return await asyncio.to_thread(self._encode_blocking, model, list(texts))

    async def _model_once(self) -> Any:
        """The loaded model, loading it at most once across concurrent callers.

        The lock is the point: without it two requests arriving together both
        see ``None`` and both pull 2.3 GB of weights into memory.
        """
        if self._model is not None:
            return self._model
        async with self._lock:
            # Re-checked inside the lock: the caller that held it may have
            # finished loading while this one was waiting.
            if self._model is None:
                logger.info("embeddings.model_loading", model=self._model_name)
                self._model = await asyncio.to_thread(self._load_blocking)
                logger.info("embeddings.model_loaded", model=self._model_name)
        return self._model

    def _load_blocking(self) -> Any:
        """Import the extra and construct the model. Downloads weights on a cold run."""
        return load_cached_first(importlib.import_module(SENTENCE_TRANSFORMERS), self._model_name)

    @staticmethod
    def _encode_blocking(model: Any, texts: list[str]) -> list[list[float]]:
        """Run inference. Called only through :func:`asyncio.to_thread`.

        Vectors come back normalised so that cosine similarity is a plain dot
        product, which is what the rest of the matching code and pgvector's
        ``<=>`` operator assume.
        """
        encoded = model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
        return [[float(value) for value in row] for row in encoded]


def load_cached_first(module: Any, model_name: str) -> Any:
    """The model from the local cache, and from the network only if it is not there.

    Measured 2026-09-17, in the dashboard walkthrough: with the weights already
    in the Hugging Face cache, ``SentenceTransformer(name)`` still asked
    huggingface.co for file metadata, the connection dropped, and the resume
    upload failed as a whole — for a model that was on disk the entire time.
    Asking the cache first makes a warm machine independent of the hub; a cold
    one still downloads, as before.

    ``Any`` for the module and the model: ``sentence_transformers`` is an
    optional extra with no stubs, imported only here.
    """
    try:
        return module.SentenceTransformer(model_name, local_files_only=True)
    except (OSError, ValueError) as error:
        # huggingface_hub reports "not in the cache" as LocalEntryNotFoundError,
        # which is both an OSError and a ValueError across its versions.
        logger.info("embeddings.model_not_cached", model=model_name, reason=type(error).__name__)
        return module.SentenceTransformer(model_name)


class UnavailableEmbeddingProvider:
    """Placeholder returned when the configured model runtime is not installed.

    Constructing it must never raise: the API has to boot, answer health checks
    and serve every non-semantic endpoint on a machine without the extra. Only
    code that actually needs a vector fails, and it fails saying what to run.
    """

    name: str = "unavailable"

    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """Always fail, with the install command in the message."""
        raise EmbeddingsUnavailableError(
            f"{self._reason}. Install the model runtime with `uv sync --extra embeddings`, "
            'or set EMBEDDING_PROVIDER="fake" to run without semantic scoring.'
        )


def _extra_installed() -> bool:
    """Whether sentence-transformers can be imported, without importing it."""
    try:
        return importlib.util.find_spec(SENTENCE_TRANSFORMERS) is not None
    except (ImportError, ValueError):
        # A broken or partially removed installation: treat it as absent rather
        # than letting the failure surface at the first encode call.
        return False


@lru_cache(maxsize=1)
def get_provider() -> EmbeddingProvider:
    """Return the process-wide provider selected by ``settings.embedding_provider``.

    Cached so the model singleton it holds is genuinely a singleton. Tests that
    change the setting call ``get_provider.cache_clear()``.
    """
    if settings.embedding_provider == "fake":
        return FakeEmbeddingProvider()
    if not _extra_installed():
        logger.warning(
            "embeddings.provider_unavailable",
            provider=settings.embedding_provider,
            model=settings.embedding_model,
        )
        return UnavailableEmbeddingProvider(
            f"embedding_provider={settings.embedding_provider!r} needs "
            f"{SENTENCE_TRANSFORMERS}, which is not installed"
        )
    return BGEM3Provider()


async def encode_texts(texts: Sequence[str]) -> list[list[float]]:
    """Encode texts through the configured provider, serving the disk cache first."""
    if not texts:
        return []

    provider = get_provider()
    cache_dir = _cache_dir()
    vectors: dict[int, list[float]] = {}
    pending: list[int] = []

    # Cache I/O is synchronous rather than threaded on purpose: reading a 4 KB
    # file is microseconds against the hundreds of milliseconds the model costs,
    # and a thread hop per lookup would cost more than the read it avoids.
    for index, text in enumerate(texts):
        cached = _read_cached(cache_dir, text) if cache_dir is not None else None
        if cached is None:
            pending.append(index)
        else:
            vectors[index] = cached

    for chunk in _chunks(pending, settings.embedding_batch_size):
        encoded = await provider.encode([texts[index] for index in chunk])
        if len(encoded) != len(chunk):
            raise EmbeddingError(
                f"provider {provider.name!r} returned {len(encoded)} vectors for {len(chunk)} texts"
            )
        for index, vector in zip(chunk, encoded, strict=True):
            _check_dimension(vector, provider.name)
            narrowed = _as_float32(vector)
            vectors[index] = narrowed
            if cache_dir is not None:
                _write_cached(cache_dir, texts[index], narrowed)

    logger.debug(
        "embeddings.encoded",
        provider=provider.name,
        texts=len(texts),
        cache_hits=len(texts) - len(pending),
        cache_misses=len(pending),
    )
    return [vectors[index] for index in range(len(texts))]


#: How much of a posting's body is embedded. Changing it changes every vector,
#: so it is a constant with a reason rather than configuration.
MAX_DESCRIPTION_CHARS = 4000


async def encode_profile(
    *,
    headline: str | None,
    skills: Sequence[str],
    titles: Sequence[str],
    domains: Sequence[str],
) -> list[float]:
    """Embed what the candidate can do, not the document that says so.

    Deliberately not the raw resume. Embedding the whole document makes the
    vector describe the resume's formatting and boilerplate — section headings,
    contact blocks, "references available on request" — which every resume
    shares, so every candidate ends up looking alike and the similarity signal
    collapses. Feeding it the extracted profile instead keeps the vector about
    competencies: headline, canonical skills, job titles, domains.

    Raises :class:`EmbeddingError` when all four are empty: that is a failed
    extraction, and a vector built from nothing would match everything.
    """
    text = _profile_text(headline=headline, skills=skills, titles=titles, domains=domains)
    if not text:
        raise EmbeddingError("profile has no headline, skills, titles or domains to embed")
    return (await encode_texts([text]))[0]


def vacancy_text(
    *,
    title: str,
    company: str | None,
    city: str | None,
    description: str | None,
) -> str:
    """Assemble the posting summary that gets embedded.

    Same labelled shape as :func:`_profile_text`, so a vacancy and a profile
    land in comparable regions of the space rather than being compared across
    two different writing styles.

    The description is truncated. A long posting is mostly boilerplate — legal
    notices, benefits, "we are an equal opportunity employer" — and past the
    first few thousand characters it dilutes the part that says what the job
    actually is. The cut is a constant rather than a setting because moving it
    invalidates every stored vector, which is a migration, not a knob.
    """
    body = (description or "").strip()
    if len(body) > MAX_DESCRIPTION_CHARS:
        body = body[:MAX_DESCRIPTION_CHARS]
    sections = [
        _section("Role", [title]),
        _section("Company", [company] if company else []),
        _section("Location", [city] if city else []),
        f"Description: {body}" if body else None,
    ]
    return "\n".join(section for section in sections if section)


def _profile_text(
    *,
    headline: str | None,
    skills: Sequence[str],
    titles: Sequence[str],
    domains: Sequence[str],
) -> str:
    """Assemble the profile summary that gets embedded.

    Labelled sections rather than a bare word bag: bge-m3 was trained on prose,
    and the labels give a short list of skills enough context to sit in the
    right region of the space.
    """
    sections = [
        _section("Role", [headline] if headline else []),
        _section("Titles", titles),
        _section("Skills", skills),
        _section("Domains", domains),
    ]
    return "\n".join(section for section in sections if section)


def _section(label: str, values: Sequence[str]) -> str | None:
    """One ``Label: a, b, c`` line, deduplicated case-insensitively, or None."""
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        cleaned = value.strip()
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        unique.append(cleaned)
    if not unique:
        return None
    return f"{label}: {', '.join(unique)}"


def _chunks(items: Sequence[int], size: int) -> Iterator[list[int]]:
    """Split indices into batches of at most ``size``."""
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _check_dimension(vector: Sequence[float], provider_name: str) -> None:
    """Refuse a vector the pgvector column cannot hold.

    A width mismatch is not recoverable and must not be rounded, padded or
    ignored: the column is declared at ``settings.embedding_dim``, so a wrong
    vector either fails the insert far away from here or, after a model swap on
    an existing table, silently corrupts every similarity search that follows.
    """
    if len(vector) != settings.embedding_dim:
        raise EmbeddingError(
            f"provider {provider_name!r} returned a vector of dimension {len(vector)}, "
            f"but settings.embedding_dim is {settings.embedding_dim}"
        )


def _as_float32(vector: Sequence[float]) -> list[float]:
    """Round a fresh vector to float32, the precision pgvector's column stores.

    Done before the value is returned or cached so that a cache hit and a cache
    miss are indistinguishable: otherwise the first call carries float64 tails
    that the cached copy, and the database, no longer have.
    """
    packed = struct.pack(f"<{len(vector)}f", *vector)
    return list(struct.unpack(f"<{len(vector)}f", packed))


def _cache_dir() -> Path | None:
    """The cache directory, created on demand; None when caching is off."""
    directory = settings.embedding_cache_dir
    if directory is None:
        return None
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # A read-only or missing mount degrades to "no cache", never to "no
        # embeddings": the vectors are recomputable, the run is not.
        logger.warning("embeddings.cache_disabled", path=str(directory), error=str(exc))
        return None
    return directory


def _cache_key(text: str) -> str:
    """Digest over model name and text.

    The model is part of the key because the same sentence has a different
    vector under a different model; a key over the text alone would keep
    serving yesterday's model after a switch, and nothing would notice.
    """
    digest = hashlib.sha256()
    digest.update(settings.embedding_model.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(text.encode("utf-8"))
    return digest.hexdigest()


def _read_cached(directory: Path, text: str) -> list[float] | None:
    """Return the cached vector, or None on a miss or an unusable file."""
    path = directory / f"{_cache_key(text)}{_CACHE_SUFFIX}"
    try:
        blob = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("embeddings.cache_read_failed", error=str(exc))
        return None

    expected = settings.embedding_dim * _FLOAT_BYTES
    if len(blob) != expected:
        # Truncated, or written under a different embedding_dim. Either way the
        # bytes are not data: decoding them would produce a plausible-looking
        # vector that means nothing. Delete, count as a miss, re-encode once.
        logger.debug("embeddings.cache_corrupt", size=len(blob), expected=expected)
        _discard(path)
        return None
    return list(struct.unpack(f"<{settings.embedding_dim}f", blob))


def _write_cached(directory: Path, text: str, vector: Sequence[float]) -> None:
    """Store a vector atomically: temp file in the same directory, then replace.

    ``os.replace`` is atomic within a filesystem, so a crash mid-write leaves
    either the previous file or no file — never half a vector that a later run
    would happily read back as garbage.
    """
    path = directory / f"{_cache_key(text)}{_CACHE_SUFFIX}"
    temp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        temp.write_bytes(struct.pack(f"<{len(vector)}f", *vector))
        os.replace(temp, path)
    except OSError as exc:
        logger.warning("embeddings.cache_write_failed", error=str(exc))
        _discard(temp)


def _discard(path: Path) -> None:
    """Remove a cache file, tolerating a filesystem that will not let us."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("embeddings.cache_unlink_failed", error=str(exc))
