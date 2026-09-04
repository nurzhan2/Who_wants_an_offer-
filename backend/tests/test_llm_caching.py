"""Prompt caching in the Anthropic provider: the block order that saves the money.

This is a cost test, not a behaviour test. Re-rank sends the same candidate
profile with all thirty vacancies of a batch; cached, that profile is one
full-price write and twenty-nine reads at a tenth of the price. But caching
matches a *prefix*: the moment the profile is placed after anything that varies
between calls, every one of those thirty calls pays full price — and the calls
still succeed, the answers are still correct, and nothing fails. The only
symptom is the invoice. So the assertions here are about position and about
byte-identity, and they are deliberately stricter than "the block is present".

Everything runs against a hand-built stand-in for ``AsyncAnthropic`` injected
through ``AnthropicAPIProvider(client=...)``: no key, no network, no cost.
"""

import base64
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from anthropic.types import TextBlock
from pydantic import BaseModel

from app.core.config import ModelPricing, settings
from app.llm import prompts
from app.llm.base import Document, LLMTask
from app.llm.pricing import TokenUsage, cost_usd
from app.llm.providers.anthropic_api import AnthropicAPIProvider

pytestmark = pytest.mark.unit

#: Deliberately not a real model id: a price that matched by accident would
#: prove nothing about the table being consulted.
MODEL = "test-cache-model"

#: Cache reads are a tenth of input and cache writes a quarter more — the real
#: shape of the price sheet, with round numbers so a mistake is visible.
PRICING = ModelPricing(
    input_usd_per_mtok=3.0,
    output_usd_per_mtok=15.0,
    cache_read_usd_per_mtok=0.3,
    cache_write_usd_per_mtok=3.75,
)

PROMPT_NAME = "rerank_demo"
PROMPT_TEMPLATE = "Score this vacancy: {{vacancy}}."

#: The part that is identical across every call of a re-rank batch, and the
#: only reason caching pays for itself here.
PROFILE = "CANDIDATE PROFILE\nBackend engineer, Python, FastAPI, PostgreSQL." * 20

EPHEMERAL = {"type": "ephemeral"}


class Answer(BaseModel):
    """Minimal response schema; one field is enough for a valid answer."""

    name: str


# ── stand-ins for the SDK ─────────────────────────────────────────────


@dataclass(slots=True)
class FakeUsage:
    """A usage block, named exactly as the API names its cache counters."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None


class FakeResponse:
    """A ``Message``-shaped object carrying only what the provider reads."""

    def __init__(
        self,
        *,
        parsed_output: BaseModel | None = None,
        text: str | None = None,
        usage: FakeUsage | None = None,
    ) -> None:
        self.content: list[TextBlock] = []
        if text is not None:
            self.content.append(TextBlock(type="text", text=text, citations=None))
        self.parsed_output = parsed_output
        self.stop_reason = "end_turn"
        self.usage = usage or FakeUsage(input_tokens=100, output_tokens=50)
        self.stop_details = None


class FakeMessages:
    """``client.messages`` with a scripted ``parse`` that records every request."""

    def __init__(self, outcomes: tuple[FakeResponse, ...]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> FakeResponse:
        """Record the request, then replay the next scripted response."""
        self.calls.append(kwargs)
        if not self._outcomes:
            raise AssertionError(f"unscripted call number {len(self.calls)}")
        return self._outcomes.pop(0)

    def content_of(self, index: int = 0) -> list[dict[str, Any]]:
        """The user content blocks of one recorded request."""
        blocks: list[dict[str, Any]] = self.calls[index]["messages"][0]["content"]
        return blocks


class FakeAnthropic:
    """Injectable replacement for ``AsyncAnthropic``."""

    def __init__(self, *outcomes: FakeResponse) -> None:
        self.messages = FakeMessages(outcomes)


MakeProvider = Callable[..., tuple[AnthropicAPIProvider, FakeAnthropic]]


@pytest.fixture
def caching_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[None]:
    """A known model, a known price sheet and a throwaway prompt on disk."""
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / f"{PROMPT_NAME}.md").write_text(PROMPT_TEMPLATE, encoding="utf-8")
    prompts.load.cache_clear()
    monkeypatch.setattr(prompts, "PROMPT_DIR", prompt_dir)
    monkeypatch.setattr(settings, "anthropic_model", MODEL)
    monkeypatch.setattr(settings, "llm_pricing", {MODEL: PRICING})
    yield
    prompts.load.cache_clear()


@pytest.fixture
def make_provider(caching_env: None) -> MakeProvider:
    """Build a provider over a scripted transport, returning both halves."""

    def _make(*outcomes: FakeResponse) -> tuple[AnthropicAPIProvider, FakeAnthropic]:
        fake = FakeAnthropic(*outcomes)
        return AnthropicAPIProvider(client=fake), fake  # type: ignore[arg-type]

    return _make


def ok(usage: FakeUsage | None = None) -> FakeResponse:
    """A response that validates first time."""
    return FakeResponse(parsed_output=Answer(name="ok"), usage=usage)


async def complete(provider: AnthropicAPIProvider, **overrides: Any) -> Any:
    """Run the provider's entry point with this file's defaults.

    ``task=RERANK`` is not incidental: re-rank is the caller whose economics
    this whole file is about.
    """
    kwargs: dict[str, Any] = {
        "task": LLMTask.RERANK,
        "effort": "low",
        "variables": {"vacancy": "Backend Engineer at Acme"},
    }
    kwargs.update(overrides)
    return await provider.complete_json(PROMPT_NAME, Answer, **kwargs)


# ── where the cached prefix sits ──────────────────────────────────────


async def test_the_cached_prefix_is_the_first_block_and_is_marked_ephemeral(
    make_provider: MakeProvider,
) -> None:
    """Caching matches a prefix, so the position IS the feature. A cache_control
    on a block that sits behind the varying part caches nothing at all, and the
    call still returns a perfectly good answer — no test that merely checks the
    marker is 'present somewhere' would ever notice the saving disappearing."""
    provider, fake = make_provider(ok())

    await complete(provider, cached_prefix=PROFILE)

    content = fake.messages.content_of()
    assert content[0] == {
        "type": "text",
        "text": PROFILE,
        "cache_control": EPHEMERAL,
    }


async def test_the_rendered_prompt_is_the_last_block(make_provider: MakeProvider) -> None:
    """The varying part must sit behind everything cacheable. If the prompt were
    emitted first, the cached prefix would start after a string that differs on
    every vacancy and the shared prefix would be zero tokens long."""
    provider, fake = make_provider(ok())

    await complete(provider, cached_prefix=PROFILE)

    content = fake.messages.content_of()
    assert content[-1] == {"type": "text", "text": "Score this vacancy: Backend Engineer at Acme."}
    assert "cache_control" not in content[-1]


async def test_documents_sit_between_the_cached_prefix_and_the_prompt(
    make_provider: MakeProvider,
) -> None:
    """Order with all three parts present: prefix, documents, prompt. A document
    ahead of the prefix would push the cacheable text out of the prefix position
    for every call that carries a file, which is the expensive case — a PDF is
    thousands of tokens."""
    provider, fake = make_provider(ok())
    pdf = b"%PDF-1.7\nnot really a pdf"

    await complete(provider, cached_prefix=PROFILE, documents=[Document(content=pdf)])

    content = fake.messages.content_of()
    assert [block["type"] for block in content] == ["text", "document", "text"]
    assert content[0]["text"] == PROFILE
    assert content[0]["cache_control"] == EPHEMERAL
    assert base64.standard_b64decode(content[1]["source"]["data"]) == pdf
    assert content[2]["text"].endswith("Backend Engineer at Acme.")


async def test_no_cached_prefix_means_no_cache_control_anywhere(
    make_provider: MakeProvider,
) -> None:
    """A cache write costs a quarter more than a plain read of the same tokens.
    Marking a block the caller never asked to cache would charge that premium on
    a one-off call that can never be read back."""
    provider, fake = make_provider(ok())

    await complete(provider, documents=[Document(content=b"%PDF-1.7\nx")])

    content = fake.messages.content_of()
    assert all("cache_control" not in block for block in content)
    assert [block["type"] for block in content] == ["document", "text"]


# ── identity across calls ─────────────────────────────────────────────


async def test_the_same_prefix_serialises_identically_across_calls(
    make_provider: MakeProvider,
) -> None:
    """A cache hit is a byte-for-byte prefix match. Any per-call difference in the
    first block — a re-rendered timestamp, a re-ordered key, a stray space — turns
    twenty-nine tenth-price reads into twenty-nine full-price writes, and every
    call still succeeds while it happens."""
    provider, fake = make_provider(ok(), ok())

    await complete(provider, cached_prefix=PROFILE, variables={"vacancy": "First posting"})
    await complete(provider, cached_prefix=PROFILE, variables={"vacancy": "Second posting"})

    first, second = fake.messages.content_of(0), fake.messages.content_of(1)
    assert json.dumps(first[0]) == json.dumps(second[0])
    # The tail really did vary, so the identity above is not vacuous.
    assert first[-1] != second[-1]


async def test_the_validation_retry_reuses_the_original_cached_block(
    make_provider: MakeProvider,
) -> None:
    """The retry appends to the conversation instead of rebuilding it, so the
    second request still opens with the identical cached block and reads it back
    at a tenth of the price. Rebuilding the message list would make the retry —
    the attempt that already cost extra — pay full price for the prefix again."""
    provider, fake = make_provider(FakeResponse(text='{"wrong": 1}'), ok())

    result = await complete(provider, cached_prefix=PROFILE)

    assert result.attempts == 2
    first, retry = fake.messages.content_of(0), fake.messages.content_of(1)
    assert json.dumps(retry[0]) == json.dumps(first[0])
    assert retry[0]["cache_control"] == EPHEMERAL


# ── what the counters cost ────────────────────────────────────────────


async def test_the_cache_counters_reach_the_usage_report(make_provider: MakeProvider) -> None:
    """``cache_read_input_tokens`` and ``cache_creation_input_tokens`` are the only
    evidence the cache was hit at all. Dropped on the floor, a broken cache looks
    exactly like a working one until the monthly bill arrives."""
    provider, _ = make_provider(
        ok(
            FakeUsage(
                input_tokens=1_000,
                output_tokens=500,
                cache_read_input_tokens=20_000,
                cache_creation_input_tokens=2_000,
            )
        )
    )

    result = await complete(provider, cached_prefix=PROFILE)

    assert result.usage.cache_read_tokens == 20_000
    assert result.usage.cache_write_tokens == 2_000
    assert result.usage.input_tokens == 1_000
    assert result.usage.total_tokens == 23_500


async def test_cached_tokens_are_priced_at_the_cache_rate_not_the_input_rate(
    make_provider: MakeProvider,
) -> None:
    """The saving only exists if cached tokens are billed from the cache columns of
    the price sheet. Pricing them as ordinary input would report a re-rank batch at
    roughly ten times its real cost and make the per-run budget refuse work that is
    comfortably affordable — the expected figure below is computed from settings so
    a price change cannot quietly desynchronise it."""
    provider, _ = make_provider(
        ok(
            FakeUsage(
                input_tokens=1_000,
                output_tokens=500,
                cache_read_input_tokens=20_000,
                cache_creation_input_tokens=2_000,
            )
        )
    )

    result = await complete(provider, cached_prefix=PROFILE)

    price = settings.llm_pricing[MODEL]
    expected = (
        1_000 * price.input_usd_per_mtok
        + 500 * price.output_usd_per_mtok
        + 20_000 * price.cache_read_usd_per_mtok
        + 2_000 * price.cache_write_usd_per_mtok
    ) / 1_000_000
    assert result.usage.cost_usd == pytest.approx(expected)

    # And the saving is real, measured against the same pricing code: the very
    # same 23_000 tokens, all of them billed as ordinary input, cost strictly
    # more. A cost function that ignored the cache columns would land on this
    # figure or above it instead of below.
    uncached = cost_usd(MODEL, TokenUsage(input_tokens=23_000, output_tokens=500))
    assert uncached is not None
    assert result.usage.cost_usd is not None
    assert result.usage.cost_usd < uncached
