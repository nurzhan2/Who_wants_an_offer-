"""Vectors: provider selection, the width guard, batching and the disk cache.

Four failures this file exists to prevent, none of which announce themselves:

* **The app refuses to boot without the optional model runtime.** The extra is
  1.5 GB of torch plus 2.3 GB of weights; the API has to start, answer health
  checks and serve every non-semantic endpoint without it.
* **A vector of the wrong width reaches the database.** pgvector's column is
  declared at ``embedding_dim``. A width mismatch that is padded, truncated or
  ignored corrupts every similarity search that follows, silently.
* **A cache hit and a cache miss disagree.** If the cached copy is not bit-for-bit
  what the first run returned, scores drift between runs for no visible reason.
* **Inference freezes the event loop.** ``encode`` is hundreds of milliseconds of
  blocking CPU work; on the loop it stalls every other request for that long.

Nothing here loads the real model: an autouse guard makes the loader raise, and
the tests that need a model monkeypatch a stand-in over it.
"""

import asyncio
import importlib.util
import inspect
import math
import time
from collections.abc import Callable, Iterator, Sequence
from importlib.machinery import ModuleSpec
from pathlib import Path
from typing import Any

import pytest

from app.core.config import settings
from app.matching import embeddings
from app.matching.embeddings import (
    BGEM3Provider,
    EmbeddingError,
    EmbeddingsUnavailableError,
    FakeEmbeddingProvider,
    UnavailableEmbeddingProvider,
    encode_profile,
    encode_texts,
    get_provider,
)

pytestmark = pytest.mark.unit

#: A profile with every field populated, so a test can change exactly one.
BASE_PROFILE: dict[str, Any] = {
    "headline": "Backend developer",
    "skills": ["Python", "PostgreSQL"],
    "titles": ["Software Engineer"],
    "domains": ["Fintech"],
}


def other_width() -> int:
    """A width belonging to some other model — what a model swap produces."""
    return 768 if settings.embedding_dim != 768 else 1024


def dot(left: Sequence[float], right: Sequence[float]) -> float:
    """Dot product, which is cosine similarity for unit vectors."""
    return sum(a * b for a, b in zip(left, right, strict=True))


def norm(vector: Sequence[float]) -> float:
    """Euclidean length."""
    return math.sqrt(sum(value * value for value in vector))


# ── doubles ───────────────────────────────────────────────────────────


class RecordingProvider:
    """Answers like the fake provider and remembers every batch it was given.

    Call counting is how the cache and batching tests prove their point: timing
    would make them flaky, and both behaviours are exactly "how many times did
    the expensive thing run, and with what".
    """

    name: str = "recording"

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    async def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """Record the batch, then return the deterministic fake vectors."""
        self.batches.append(list(texts))
        return [FakeEmbeddingProvider.vector_for(text) for text in texts]

    @property
    def calls(self) -> int:
        """How many times the provider was asked for vectors."""
        return len(self.batches)


class FixedWidthProvider:
    """A provider that always returns vectors of a width it was told to use."""

    name: str = "fixed-width"

    def __init__(self, width: int) -> None:
        self.width = width

    async def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one wrong-width vector per text."""
        return [[0.1] * self.width for _ in texts]


class ShortCountProvider:
    """A provider that returns fewer vectors than it was given texts."""

    name: str = "short-count"

    async def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """Drop the last vector, the way a batching bug in a real client would."""
        return [FakeEmbeddingProvider.vector_for(text) for text in list(texts)[:-1]]


class ExplodingProvider:
    """A provider that refuses to encode, so a cache read can be caught alone."""

    name: str = "exploding"

    async def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """Always fail."""
        raise RuntimeError("provider must not be reached")


class BlockingModel:
    """Stand-in for ``SentenceTransformer``: it sleeps in the calling thread.

    ``time.sleep`` rather than ``asyncio.sleep`` on purpose — the real model is
    synchronous CPU work, and only a synchronous sleep can show whether it was
    moved off the loop.
    """

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay

    def encode(
        self,
        texts: Sequence[str],
        *,
        normalize_embeddings: bool,
        convert_to_numpy: bool,
    ) -> list[list[float]]:
        """Block for ``delay`` seconds, then return deterministic vectors."""
        time.sleep(self.delay)
        return [FakeEmbeddingProvider.vector_for(text) for text in texts]


def patch_find_spec(
    monkeypatch: pytest.MonkeyPatch, answer: Callable[[], ModuleSpec | None]
) -> None:
    """Change what ``find_spec`` reports for the extra, and nothing else.

    ``importlib.util.find_spec`` is global and other libraries probe it to decide
    whether an optional feature of their own is available — often caching the
    answer for the life of the process. A patch that replied for every module
    name would therefore corrupt unrelated packages for the rest of the session,
    far away from this file, which is exactly what an earlier version did.
    """
    real_find_spec = importlib.util.find_spec

    def find_spec(name: str, package: str | None = None) -> ModuleSpec | None:
        if name == embeddings.SENTENCE_TRANSFORMERS:
            return answer()
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)


def use_provider(monkeypatch: pytest.MonkeyPatch, provider: object) -> None:
    """Point ``encode_texts`` at an explicit provider.

    ``encode_texts`` resolves the provider itself; there is no injection
    parameter, so the module-level lookup is what a test has to replace.
    """
    monkeypatch.setattr(embeddings, "get_provider", lambda: provider)


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_provider() -> Iterator[None]:
    """Empty ``get_provider``'s cache around every test.

    It is an ``lru_cache``: one test that changes ``embedding_provider`` would
    otherwise hand its provider to every test that runs after it, and the order
    those tests pass in would depend on collection order.
    """
    get_provider.cache_clear()
    yield
    get_provider.cache_clear()


@pytest.fixture(autouse=True)
def _no_disk_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caching off unless a test asks for it.

    The default is ``.cache/embeddings`` relative to the working directory, so
    without this a test run reads and writes the developer's real cache and its
    result depends on what was left there yesterday.
    """
    monkeypatch.setattr(settings, "embedding_cache_dir", None)


@pytest.fixture(autouse=True)
def _model_never_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make loading the real model a loud failure.

    Nothing in this suite may pull 2.3 GB of weights or import torch. Tests that
    need a model patch this again with a stand-in.
    """

    def refuse(self: BGEM3Provider) -> Any:
        raise AssertionError("the real embedding model must never load in tests")

    monkeypatch.setattr(BGEM3Provider, "_load_blocking", refuse)


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Turn the disk cache on, pointed at a directory of this test's own."""
    directory = tmp_path / "vectors"
    monkeypatch.setattr(settings, "embedding_cache_dir", directory)
    return directory


@pytest.fixture
def extra_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the ``[embeddings]`` extra look uninstalled, whatever this machine has.

    It genuinely is absent in CI, but the behaviour under test is "no extra",
    not "this laptop": the test has to keep meaning the same thing on a machine
    where somebody ran ``uv sync --extra embeddings``.
    """
    patch_find_spec(monkeypatch, lambda: None)


# ── the fake provider ─────────────────────────────────────────────────


@pytest.mark.parametrize("dim", [8, 384, 1024])
def test_fake_vectors_are_as_wide_as_the_configured_column(
    dim: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The width comes from settings, not from a constant in the provider.

    Everything downstream — the pgvector column, the cache file size, the
    dimension guard — is derived from ``embedding_dim``. A fake that ignored it
    would make every test that uses it pass against a shape production rejects.
    """
    monkeypatch.setattr(settings, "embedding_dim", dim)

    assert len(FakeEmbeddingProvider.vector_for("senior python developer")) == dim


@pytest.mark.parametrize("text", ["", "python", "Разработчик Python, Алматы", "x" * 5000])
def test_fake_vectors_are_unit_length(text: str) -> None:
    """Cosine similarity is computed as a plain dot product here and in pgvector.

    That shortcut is only correct for unit vectors. A fake of some other length
    would make every score in the matching tests quietly wrong.
    """
    assert norm(FakeEmbeddingProvider.vector_for(text)) == pytest.approx(1.0, abs=1e-9)


def test_the_same_text_always_gets_the_same_vector() -> None:
    """Determinism is what makes the disk cache correct and reruns reproducible.

    Cache keys are digests of the text: a provider that answered differently on
    the second call would make a cached vector disagree with a fresh one, and
    nothing would report it.
    """
    first = FakeEmbeddingProvider.vector_for("senior backend engineer")
    second = FakeEmbeddingProvider.vector_for("senior backend engineer")

    assert first == second


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("python developer", "java developer"),
        ("a", "b"),
        ("Алматы", "Astana"),
    ],
)
def test_unrelated_texts_do_not_point_the_same_way(left: str, right: str) -> None:
    """Different texts must be different vectors, and not near-parallel ones.

    The trap: pseudo-random components confined to [0, 1) all sit in the positive
    orthant, so every pair of unrelated texts scores ~0.75 similar. Matching would
    then rank every candidate against every vacancy as a decent fit, and the fake
    provider would hide that instead of exposing it.
    """
    first = FakeEmbeddingProvider.vector_for(left)
    second = FakeEmbeddingProvider.vector_for(right)

    assert first != second
    assert abs(dot(first, second)) < 0.3


async def test_fake_encode_returns_one_vector_per_text_in_order() -> None:
    """Callers zip vectors back onto their inputs by position, nothing else."""
    provider = FakeEmbeddingProvider()

    vectors = await provider.encode(["alpha", "beta"])

    assert vectors == [
        FakeEmbeddingProvider.vector_for("alpha"),
        FakeEmbeddingProvider.vector_for("beta"),
    ]


# ── provider selection ────────────────────────────────────────────────


def test_configuration_selects_the_fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """``EMBEDDING_PROVIDER=fake`` is the documented way to run without 3.8 GB
    on disk; if it stopped being honoured, every such install would break."""
    monkeypatch.setattr(settings, "embedding_provider", "fake")

    assert isinstance(get_provider(), FakeEmbeddingProvider)


def test_the_provider_is_a_process_wide_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """The provider holds the loaded model; a second instance means a second
    2.3 GB copy of the weights in memory."""
    monkeypatch.setattr(settings, "embedding_provider", "fake")

    assert get_provider() is get_provider()


def test_a_settings_change_takes_effect_only_after_cache_clear(
    monkeypatch: pytest.MonkeyPatch, extra_absent: None
) -> None:
    """The cache is what makes the singleton a singleton, and it means changing
    the setting at runtime changes nothing.

    Stated as a test because it is the trap every other test in this file has to
    work around: without ``cache_clear`` a test configures one provider and gets
    the previous test's.
    """
    monkeypatch.setattr(settings, "embedding_provider", "fake")
    fake = get_provider()

    monkeypatch.setattr(settings, "embedding_provider", "bge-m3")
    assert get_provider() is fake

    get_provider.cache_clear()
    assert isinstance(get_provider(), UnavailableEmbeddingProvider)


def test_a_missing_extra_degrades_instead_of_failing_to_boot(
    monkeypatch: pytest.MonkeyPatch, extra_absent: None
) -> None:
    """Asking for the real model on an install without it must not raise here.

    ``get_provider`` runs during startup and dependency wiring. If a missing
    optional extra raised at construction, the whole API — health checks, resume
    upload, every non-semantic endpoint — would be down on a machine that simply
    never ran ``uv sync --extra embeddings``.
    """
    monkeypatch.setattr(settings, "embedding_provider", "bge-m3")

    provider = get_provider()

    assert isinstance(provider, UnavailableEmbeddingProvider)
    assert not isinstance(provider, BGEM3Provider)


def test_a_broken_install_of_the_extra_counts_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-removed package makes ``find_spec`` raise rather than return None.

    That has to degrade the same way a clean absence does. Letting the error
    escape would turn a stale ``site-packages`` directory into a crash at boot.
    """

    def broken() -> ModuleSpec | None:
        raise ValueError(f"{embeddings.SENTENCE_TRANSFORMERS} has no __spec__")

    monkeypatch.setattr(settings, "embedding_provider", "bge-m3")
    patch_find_spec(monkeypatch, broken)

    assert isinstance(get_provider(), UnavailableEmbeddingProvider)


async def test_the_placeholder_fails_only_on_use_and_says_what_to_install(
    monkeypatch: pytest.MonkeyPatch, extra_absent: None
) -> None:
    """The failure has to arrive at the one feature that needs a vector, carrying
    the command that fixes it.

    A bare ImportError deep in a pipeline run tells the reader nothing; the whole
    point of the placeholder is that the error names the install command.
    """
    monkeypatch.setattr(settings, "embedding_provider", "bge-m3")
    provider = get_provider()

    with pytest.raises(EmbeddingsUnavailableError) as error:
        await provider.encode(["python developer"])

    assert "uv sync --extra embeddings" in str(error.value)


# ── the width guard ───────────────────────────────────────────────────


async def test_a_wrong_width_vector_is_refused_naming_both_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE guard of this module. A vector of the wrong width must never be stored.

    After a model swap on an existing table, a 768-wide vector either explodes at
    insert time far from here or — worse — lands in a column that still holds
    1024-wide neighbours and silently corrupts every similarity search. The
    message has to carry both numbers, because "which side is wrong" is the whole
    question the reader is asking.
    """
    wrong = other_width()
    use_provider(monkeypatch, FixedWidthProvider(wrong))

    with pytest.raises(EmbeddingError) as error:
        await encode_texts(["python developer"])

    message = str(error.value)
    assert str(wrong) in message
    assert str(settings.embedding_dim) in message


async def test_a_provider_returning_too_few_vectors_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vectors are matched to texts by position, so a short answer would shift
    every remaining vector onto the wrong vacancy rather than fail."""
    use_provider(monkeypatch, ShortCountProvider())

    with pytest.raises(EmbeddingError) as error:
        await encode_texts(["alpha", "beta", "gamma"])

    assert "2" in str(error.value)
    assert "3" in str(error.value)


# ── batching and ordering ─────────────────────────────────────────────


async def test_encoding_nothing_never_reaches_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty run — every vacancy already embedded — must not load the model.

    The provider lookup constructs (and eventually loads) the real model, so the
    empty case has to short-circuit before it, not merely return early after.
    """

    def explode() -> object:
        raise AssertionError("the provider was resolved for an empty request")

    monkeypatch.setattr(embeddings, "get_provider", explode)

    assert await encode_texts([]) == []


async def test_batches_are_capped_at_the_configured_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batch size is the memory ceiling of a pipeline run.

    One call with a thousand texts is what exhausts GPU or process memory
    mid-run; the setting exists to bound it, and only the shape of the calls the
    provider actually receives can show that it does.
    """
    provider = RecordingProvider()
    use_provider(monkeypatch, provider)
    monkeypatch.setattr(settings, "embedding_batch_size", 2)

    await encode_texts(["a", "b", "c", "d", "e"])

    assert provider.batches == [["a", "b"], ["c", "d"], ["e"]]


async def test_results_stay_in_input_order_when_some_texts_are_cached(
    monkeypatch: pytest.MonkeyPatch, cache_dir: Path
) -> None:
    """Cached and freshly encoded vectors are reassembled by index.

    A partially warm cache is the normal case for a pipeline run. If the two
    groups were concatenated instead of reassembled, every score would be
    computed against a different vacancy's vector, and the output would still
    look like a plausible list of matches.
    """
    provider = RecordingProvider()
    use_provider(monkeypatch, provider)
    await encode_texts(["beta", "delta"])
    provider.batches.clear()

    vectors = await encode_texts(["alpha", "beta", "gamma", "delta"])

    assert provider.batches == [["alpha", "gamma"]]
    for text, vector in zip(["alpha", "beta", "gamma", "delta"], vectors, strict=True):
        assert vector == pytest.approx(FakeEmbeddingProvider.vector_for(text), abs=1e-6)


# ── the disk cache ────────────────────────────────────────────────────


async def test_a_repeated_text_does_not_reach_the_provider_twice(
    monkeypatch: pytest.MonkeyPatch, cache_dir: Path
) -> None:
    """The reason the cache exists: a run re-encodes thousands of vacancies whose
    text has not changed since yesterday, at ~50 ms each."""
    provider = RecordingProvider()
    use_provider(monkeypatch, provider)

    await encode_texts(["senior python developer"])
    await encode_texts(["senior python developer"])

    assert provider.calls == 1


async def test_the_cached_vector_is_identical_to_the_computed_one(
    monkeypatch: pytest.MonkeyPatch, cache_dir: Path
) -> None:
    """A cache hit and a cache miss must be indistinguishable, to the bit.

    Vectors are narrowed to float32 before being returned or cached precisely so
    this holds. Without it the first run carries float64 tails the cached copy
    and the database do not, and scores shift between runs of the same input for
    no reason anybody can see.
    """
    use_provider(monkeypatch, RecordingProvider())

    [computed] = await encode_texts(["senior python developer"])
    [cached] = await encode_texts(["senior python developer"])

    assert cached == computed


@pytest.mark.parametrize(
    ("mutation", "mutate"),
    [
        ("truncated", lambda blob: blob[:-8]),
        ("too wide", lambda blob: blob + b"\x00\x00\x00\x00"),
    ],
)
async def test_a_cache_file_of_the_wrong_size_is_a_miss(
    monkeypatch: pytest.MonkeyPatch,
    cache_dir: Path,
    mutation: str,
    mutate: Callable[[bytes], bytes],
) -> None:
    """Bytes of the wrong length are not data and must not be decoded.

    A crash mid-write, or a file written under a different ``embedding_dim``,
    would otherwise unpack into a plausible-looking vector that means nothing —
    the worst possible outcome, because it is indistinguishable from a real one.
    """
    provider = RecordingProvider()
    use_provider(monkeypatch, provider)
    await encode_texts(["senior python developer"])
    [path] = list(cache_dir.iterdir())
    path.write_bytes(mutate(path.read_bytes()))

    [vector] = await encode_texts(["senior python developer"])

    assert provider.calls == 2
    assert vector == pytest.approx(
        FakeEmbeddingProvider.vector_for("senior python developer"), abs=1e-6
    )


async def test_an_unusable_cache_file_is_deleted(
    monkeypatch: pytest.MonkeyPatch, cache_dir: Path
) -> None:
    """A corrupt file is removed, not merely skipped.

    Skipping alone leaves it there to be re-read and re-rejected on every run
    forever. Deletion is checked while the provider refuses to encode, so the
    removal cannot be confused with the rewrite that a successful re-encode does.
    """
    use_provider(monkeypatch, RecordingProvider())
    await encode_texts(["senior python developer"])
    [path] = list(cache_dir.iterdir())
    path.write_bytes(b"\x00" * 8)
    use_provider(monkeypatch, ExplodingProvider())

    with pytest.raises(RuntimeError):
        await encode_texts(["senior python developer"])

    assert list(cache_dir.iterdir()) == []


async def test_no_cache_directory_means_every_call_recomputes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``EMBEDDING_CACHE_DIR`` unset must disable caching, not fall back to a
    default path: an operator who turned it off has a reason, and a process
    writing vectors into an unexpected directory is a surprise on a read-only
    or shared filesystem."""
    provider = RecordingProvider()
    use_provider(monkeypatch, provider)

    await encode_texts(["senior python developer"])
    await encode_texts(["senior python developer"])

    assert provider.calls == 2


async def test_an_unusable_cache_directory_degrades_to_no_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cache directory that cannot be created must not stop the run.

    The cache is an optimisation and the vectors are recomputable; the pipeline
    run is not. A read-only mount, a full disk or — as here — a path whose parent
    is a file has to cost speed, never results.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "embedding_cache_dir", blocker / "vectors")
    provider = RecordingProvider()
    use_provider(monkeypatch, provider)

    [vector] = await encode_texts(["senior python developer"])

    assert provider.calls == 1
    assert len(vector) == settings.embedding_dim


async def test_switching_the_model_invalidates_cached_vectors(
    monkeypatch: pytest.MonkeyPatch, cache_dir: Path
) -> None:
    """The same sentence has a different vector under a different model.

    A key over the text alone would keep serving yesterday's model's vectors
    after a switch, mixing two incompatible spaces in one column, and nothing
    anywhere would report it.
    """
    provider = RecordingProvider()
    use_provider(monkeypatch, provider)
    await encode_texts(["senior python developer"])

    monkeypatch.setattr(settings, "embedding_model", "BAAI/bge-small-en-v1.5")
    await encode_texts(["senior python developer"])

    assert provider.calls == 2


# ── profile embedding ─────────────────────────────────────────────────


def test_profile_embedding_has_no_parameter_for_raw_resume_text() -> None:
    """Embedding the document instead of the profile is the failure mode here.

    A resume's text is mostly boilerplate every resume shares — headings, contact
    blocks, "references available on request" — so a vector built from it
    describes the format, every candidate looks alike, and the semantic signal
    collapses. The defence is structural: there is no way to pass the text in.
    """
    parameters = inspect.signature(encode_profile).parameters

    assert set(parameters) == {"headline", "skills", "titles", "domains"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("headline", "Data engineer"),
        ("skills", ["Go", "Kubernetes"]),
        ("titles", ["Analyst"]),
        ("domains", ["Retail"]),
    ],
)
async def test_every_profile_field_feeds_the_vector(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    """All four inputs must reach the embedded text.

    A field silently dropped from the assembled summary is invisible: the vector
    still looks fine, matching still returns results, and candidates are simply
    ranked as if they had no skills — or no domain experience — at all.
    """
    use_provider(monkeypatch, FakeEmbeddingProvider())
    changed = {**BASE_PROFILE, field: value}

    base_vector = await encode_profile(**BASE_PROFILE)
    changed_vector = await encode_profile(**changed)

    assert changed_vector != base_vector


async def test_the_same_competency_written_differently_gives_one_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case and spacing in an extracted skill list are noise, not signal.

    The LLM returns "Python", "python" and " PYTHON " for one skill. If those
    produced different vectors, two identical candidates would score differently
    and every cache key would be a miss.
    """
    use_provider(monkeypatch, FakeEmbeddingProvider())

    once = await encode_profile(headline=None, skills=["Python"], titles=[], domains=[])
    thrice = await encode_profile(
        headline=None, skills=["Python", "python", " PYTHON "], titles=[], domains=[]
    )

    assert thrice == once


@pytest.mark.parametrize(
    ("headline", "skills", "titles", "domains"),
    [
        (None, [], [], []),
        ("   ", [" ", ""], [], [""]),
    ],
)
async def test_an_empty_profile_is_an_error_not_a_vector(
    monkeypatch: pytest.MonkeyPatch,
    headline: str | None,
    skills: list[str],
    titles: list[str],
    domains: list[str],
) -> None:
    """Nothing to embed means the extraction failed, and that must be said aloud.

    A vector built from an empty string is a valid-looking point in the space
    that sits equally near everything, so the user gets a full page of confident
    matches produced from no information at all.
    """
    use_provider(monkeypatch, FakeEmbeddingProvider())

    with pytest.raises(EmbeddingError):
        await encode_profile(headline=headline, skills=skills, titles=titles, domains=domains)


# ── the event loop ────────────────────────────────────────────────────


async def test_inference_does_not_block_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Encoding must leave the loop free to run everything else.

    Asserted by racing a real blocking encode against a coroutine that counts
    ticks, rather than by asserting that ``asyncio.to_thread`` was called. The
    property that matters to a user is "the API still answers during inference";
    the thread hop is one way to get it. A test pinned to ``to_thread`` would go
    green on a version that offloads differently and red on a refactor that is
    perfectly correct — it would test the implementation, not the promise.

    The counter yields with ``sleep(0)`` rather than a timed sleep so the margin
    is enormous rather than marginal: measured, an offloaded encode leaves room
    for tens of thousands of ticks and an encode on the loop leaves exactly zero,
    against a threshold of a hundred.
    """
    monkeypatch.setattr(BGEM3Provider, "_load_blocking", lambda self: BlockingModel(delay=0.1))
    provider = BGEM3Provider()
    finished = asyncio.Event()
    ticks = 0

    async def count_ticks() -> None:
        nonlocal ticks
        while not finished.is_set():
            ticks += 1
            await asyncio.sleep(0)

    async def run_inference() -> list[list[float]]:
        try:
            return await provider.encode(["senior python developer"])
        finally:
            finished.set()

    vectors, _ = await asyncio.gather(run_inference(), count_ticks())

    assert len(vectors) == 1
    assert ticks > 100


async def test_the_real_provider_loads_nothing_for_an_empty_request() -> None:
    """An empty batch must not be what pulls 2.3 GB of weights into memory.

    The model is loaded lazily on first use, so "first use" has to mean a real
    request. The autouse guard in this module makes the load an assertion error,
    which is what would fire if the empty case stopped short-circuiting.
    """
    assert await BGEM3Provider().encode([]) == []


async def test_a_second_request_does_not_load_the_model_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model is loaded once per process, not once per request.

    A regression that reloaded on every call would put 2.3 GB of weights and
    tens of seconds of load time into every encode, and nothing would fail:
    the vectors would still be right, the run would just take hours. This is
    a different path through the loader than the concurrent case below — the
    second request finds the model already there and never reaches the lock.
    """
    loads = 0

    def load_once(self: BGEM3Provider) -> BlockingModel:
        nonlocal loads
        loads += 1
        return BlockingModel()

    monkeypatch.setattr(BGEM3Provider, "_load_blocking", load_once)
    provider = BGEM3Provider()

    await provider.encode(["alpha"])
    await provider.encode(["beta"])

    assert loads == 1


async def test_concurrent_first_requests_load_the_model_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two requests arriving before the model is ready must not both load it.

    Each load is 2.3 GB of weights; two at once is an out-of-memory kill on a
    box sized for one. Without the lock both callers see an unloaded model and
    both start loading, and the symptom only ever appears under real traffic.
    """
    loads = 0

    def load_once(self: BGEM3Provider) -> BlockingModel:
        nonlocal loads
        loads += 1
        time.sleep(0.05)
        return BlockingModel()

    monkeypatch.setattr(BGEM3Provider, "_load_blocking", load_once)
    provider = BGEM3Provider()

    await asyncio.gather(provider.encode(["alpha"]), provider.encode(["beta"]))

    assert loads == 1


class _Hub:
    """A stand-in ``sentence_transformers`` module that records how it was asked."""

    def __init__(self, cached: bool) -> None:
        self.cached = cached
        self.calls: list[dict[str, Any]] = []
        hub = self

        class SentenceTransformer:
            def __init__(self, name: str, **kwargs: Any) -> None:
                hub.calls.append({"name": name, **kwargs})
                if kwargs.get("local_files_only") and not hub.cached:
                    raise OSError("not in the local cache")

        self.SentenceTransformer = SentenceTransformer


def test_a_cached_model_loads_without_the_network() -> None:
    """A warm machine must not depend on huggingface.co answering."""
    from app.matching.embeddings import load_cached_first

    hub = _Hub(cached=True)
    load_cached_first(hub, "BAAI/bge-m3")

    assert hub.calls == [{"name": "BAAI/bge-m3", "local_files_only": True}]


def test_a_model_not_in_the_cache_is_downloaded() -> None:
    from app.matching.embeddings import load_cached_first

    hub = _Hub(cached=False)
    load_cached_first(hub, "BAAI/bge-m3")

    assert hub.calls == [
        {"name": "BAAI/bge-m3", "local_files_only": True},
        {"name": "BAAI/bge-m3"},
    ]
