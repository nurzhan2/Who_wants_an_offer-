"""Prove the real embedding model works. Run by hand, or from CI on demand.

CI does not install the ``[embeddings]`` extra: it costs about 1.5 GB of
dependencies and 2.3 GB of weights, which is not something every pull request
should pay for. That saving leaves a hole — ``BGEM3Provider`` would be the one
module nothing ever executes, and a break in it would surface in phase 5 as
"the scores look wrong" with no way to tell whether the weights or the
embeddings were at fault.

This script is what closes that hole:

    make verify-embeddings          # locally
    gh workflow run ci.yml -f verify_embeddings=true

It loads the model for real, encodes three texts in three languages, and prints
the dimension, the time per document and whether the disk cache is working. It
exits non-zero if anything is off, so CI can gate on it.
"""

import asyncio
import shutil
import sys
import time
from pathlib import Path
from tempfile import mkdtemp

from app.core.config import settings
from app.matching import embeddings

SAMPLES = (
    "Senior Backend Engineer. Python, FastAPI, PostgreSQL, Kafka. Fintech.",
    "Ведущий backend-разработчик. Python, асинхронные сервисы, PostgreSQL, "
    "высоконагруженные системы.",
    "Аға backend әзірлеуші. Python, микросервистер, дерекқор жобалау.",
)


def cosine(left: list[float], right: list[float]) -> float:
    """Cosine similarity, assuming neither vector is zero."""
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm_left = sum(a * a for a in left) ** 0.5
    norm_right = sum(b * b for b in right) ** 0.5
    return dot / (norm_left * norm_right)


async def main() -> int:
    """Load, encode, measure, report."""
    cache_dir = Path(mkdtemp(prefix="wwao-embed-verify-"))
    settings.embedding_cache_dir = cache_dir
    embeddings.get_provider.cache_clear()

    provider = embeddings.get_provider()
    print(f"provider          {provider.name}")
    print(f"model             {settings.embedding_model}")
    print(f"expected dim      {settings.embedding_dim}")

    if provider.name != "bge-m3":
        print(
            f"\nFAIL: expected the real provider, got {provider.name!r}.\n"
            "Run `uv sync --extra embeddings` and set EMBEDDING_PROVIDER=bge-m3."
        )
        return 1

    load_started = time.perf_counter()
    first = await embeddings.encode_texts(SAMPLES[:1])
    load_seconds = time.perf_counter() - load_started
    print(f"first call        {load_seconds:.1f}s (includes loading the model)")

    dims = {len(vector) for vector in first}
    if dims != {settings.embedding_dim}:
        print(f"\nFAIL: got dimensions {sorted(dims)}, expected {settings.embedding_dim}")
        return 1

    warm_started = time.perf_counter()
    vectors = await embeddings.encode_texts(SAMPLES[1:])
    warm_seconds = time.perf_counter() - warm_started
    print(f"per document      {warm_seconds / len(SAMPLES[1:]) * 1000:.0f} ms (model warm)")

    cached_started = time.perf_counter()
    again = await embeddings.encode_texts(SAMPLES[1:])
    cached_seconds = time.perf_counter() - cached_started
    print(f"cached repeat     {cached_seconds * 1000:.0f} ms total")

    if again != vectors:
        print("\nFAIL: the cached vectors differ from the freshly computed ones")
        return 1
    if cached_seconds >= warm_seconds:
        print("\nFAIL: the second call was not faster, so the disk cache is not working")
        return 1

    files = len(list(cache_dir.rglob("*")))
    print(f"cache files       {files}")

    ru_en = cosine(first[0], vectors[0])
    ru_kk = cosine(vectors[0], vectors[1])
    print(f"cos(en, ru)       {ru_en:.3f}   (same role, different language)")
    print(f"cos(ru, kk)       {ru_kk:.3f}")

    if ru_en < 0.5:
        print(
            "\nFAIL: the English and Russian versions of the same role should be "
            f"close, got {ru_en:.3f}. A multilingual model that does not align "
            "languages is useless for this market."
        )
        return 1

    shutil.rmtree(cache_dir, ignore_errors=True)
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
