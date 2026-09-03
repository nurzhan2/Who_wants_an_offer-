"""The Anthropic wrapper: what it costs, what it retries, what it refuses to guess.

Every call in this file is driven by a hand-built stand-in for ``AsyncAnthropic``
injected through ``LLMClient(client=...)``. Nothing here touches the network and
nothing needs an API key: the wrapper's whole job is policy — tier selection,
retry rules, refusal handling and cost accounting — and policy is exactly what a
live call would hide behind a plausible-looking answer.
"""

import base64
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
import structlog
from anthropic.types import TextBlock
from pydantic import BaseModel
from tenacity import wait_none

from app.core.config import ModelPricing, Settings, default_pricing, settings
from app.core.exceptions import LLMError
from app.llm import client as client_module
from app.llm import prompts
from app.llm.client import Document, LLMClient, LLMRefusalError, LLMTier
from app.llm.pricing import TokenUsage, cost_usd, pricing_for

pytestmark = pytest.mark.unit

#: Deliberately not the real model ids: a test that passes because the default
#: happened to match would not prove the tier was consulted at all.
HOT_MODEL = "test-hot-model"
HEAVY_MODEL = "test-heavy-model"

#: Round numbers so an expected cost can be written out by hand.
PRICING: dict[str, ModelPricing] = {
    HOT_MODEL: ModelPricing(
        input_usd_per_mtok=2.0,
        output_usd_per_mtok=10.0,
        cache_read_usd_per_mtok=0.2,
        cache_write_usd_per_mtok=2.5,
    ),
    HEAVY_MODEL: ModelPricing(
        input_usd_per_mtok=5.0,
        output_usd_per_mtok=25.0,
    ),
}

PROMPT_NAME = "demo"
PROMPT_TEMPLATE = "Extract structured data about {{subject}}."
#: Stands in for resume content: if this string ever reaches a log line, the
#: wrapper is leaking the document it was given.
SUBJECT = "Ivan Petrov, ivan.petrov@example.com, +7 700 000 00 00"


class Answer(BaseModel):
    """Minimal response schema: one required field is enough to fail validation."""

    name: str
    score: int = 0


# ── stand-ins for the SDK ─────────────────────────────────────────────


@dataclass(slots=True)
class FakeUsage:
    """The usage block, including the two fields an older API version omits."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None


class FakeResponse:
    """A ``Message``-shaped object carrying only what the wrapper reads off it."""

    def __init__(
        self,
        *,
        text: str | None = None,
        parsed_output: BaseModel | None = None,
        stop_reason: str = "end_turn",
        usage: FakeUsage | None = None,
        refusal_category: str | None = None,
    ) -> None:
        self.content: list[TextBlock] = []
        if text is not None:
            self.content.append(TextBlock(type="text", text=text))
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason
        self.usage = usage or FakeUsage(input_tokens=1000, output_tokens=500)
        self.stop_details = SimpleNamespace(category=refusal_category) if refusal_category else None


class FakeMessages:
    """``client.messages`` with a scripted ``parse``: one outcome per call."""

    def __init__(self, outcomes: tuple[object, ...]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> FakeResponse:
        """Record the request, then replay the next scripted outcome."""
        self.calls.append(kwargs)
        if not self._outcomes:
            raise AssertionError(f"unscripted call number {len(self.calls)}")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, FakeResponse)
        return outcome


class FakeAnthropic:
    """Injectable replacement for ``AsyncAnthropic``."""

    def __init__(self, *outcomes: object) -> None:
        self.messages = FakeMessages(outcomes)


def api_error(exc_type: type[anthropic.APIStatusError], status_code: int) -> Exception:
    """An SDK status error, built the way the SDK builds them."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return exc_type(
        "boom",
        response=httpx.Response(status_code, request=request),
        body=None,
    )


MakeClient = Callable[..., tuple[LLMClient, FakeAnthropic]]


@pytest.fixture
def llm_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[None]:
    """Known models, known prices, a throwaway prompt and no real backoff sleeps."""
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / f"{PROMPT_NAME}.md").write_text(PROMPT_TEMPLATE, encoding="utf-8")
    prompts.load.cache_clear()
    monkeypatch.setattr(prompts, "PROMPT_DIR", prompt_dir)
    monkeypatch.setattr(settings, "anthropic_model", HOT_MODEL)
    monkeypatch.setattr(settings, "anthropic_model_heavy", HEAVY_MODEL)
    monkeypatch.setattr(settings, "llm_pricing", PRICING)
    monkeypatch.setattr(settings, "anthropic_max_tokens", 4321)
    monkeypatch.setattr(settings, "anthropic_max_retries", 2)
    # Real jitter would make the retry tests take seconds for nothing.
    monkeypatch.setattr(client_module, "wait_exponential_jitter", lambda **_: wait_none())
    yield
    prompts.load.cache_clear()


@pytest.fixture
def make_client(llm_env: None) -> MakeClient:
    """Build a client over a scripted transport, returning both halves."""

    def _make(*outcomes: object) -> tuple[LLMClient, FakeAnthropic]:
        fake = FakeAnthropic(*outcomes)
        return LLMClient(client=fake), fake  # type: ignore[arg-type]

    return _make


async def complete(client: LLMClient, **overrides: Any) -> Any:
    """Run the wrapper's one public entry point with this file's defaults."""
    kwargs: dict[str, Any] = {
        "effort": "low",
        "variables": {"subject": SUBJECT},
    }
    kwargs.update(overrides)
    return await client.complete_json(PROMPT_NAME, Answer, **kwargs)


# ── the happy path ────────────────────────────────────────────────────


async def test_first_valid_response_is_returned_without_a_retry(make_client: MakeClient) -> None:
    """The common case must cost exactly one call: a wrapper that quietly asks
    twice doubles the invoice for every vacancy in a pipeline run."""
    client, fake = make_client(
        FakeResponse(parsed_output=Answer(name="Ivan", score=7), text='{"name": "Ivan"}')
    )

    result = await complete(client)

    assert result.value == Answer(name="Ivan", score=7)
    assert result.attempts == 1
    assert len(fake.messages.calls) == 1


async def test_structured_output_is_preferred_over_reparsing_the_text(
    make_client: MakeClient,
) -> None:
    """``parsed_output`` is what the API actually constrained; re-reading the text
    block would silently accept a value the schema never validated."""
    client, _ = make_client(
        FakeResponse(parsed_output=Answer(name="from-parsed"), text='{"name": "from-text"}')
    )

    result = await complete(client)

    assert result.value.name == "from-parsed"


async def test_the_schema_is_sent_so_the_api_constrains_generation(
    make_client: MakeClient,
) -> None:
    """Structured outputs are what make malformed answers rare enough for a single
    retry to be an adequate policy. Omit the schema from the request and the model
    is free-generating JSON — the wrapper still works, just at a failure rate the
    retry budget was never sized for."""
    client, fake = make_client(FakeResponse(parsed_output=Answer(name="Ivan")))

    await complete(client)

    assert fake.messages.calls[0]["output_format"] is Answer


async def test_usage_and_cost_come_back_with_the_answer(make_client: MakeClient) -> None:
    """A caller that cannot see what a call cost cannot enforce a per-run budget,
    and the overspend is only discovered on the invoice."""
    client, _ = make_client(
        FakeResponse(
            parsed_output=Answer(name="Ivan"),
            usage=FakeUsage(
                input_tokens=1000,
                output_tokens=500,
                cache_read_input_tokens=2000,
                cache_creation_input_tokens=400,
            ),
        )
    )

    result = await complete(client)

    assert result.model == HOT_MODEL
    assert result.usage == TokenUsage(
        input_tokens=1000, output_tokens=500, cache_read_tokens=2000, cache_write_tokens=400
    )
    # 1000*2 + 500*10 + 2000*0.2 + 400*2.5 = 8400 per million.
    assert result.cost_usd == pytest.approx(0.0084)


async def test_missing_usage_counters_are_read_as_zero(make_client: MakeClient) -> None:
    """An API version that omits the cache counters must not crash the call:
    unknown cache usage is zero cache usage, not a failed extraction."""
    client, _ = make_client(
        FakeResponse(parsed_output=Answer(name="Ivan"), usage=FakeUsage(input_tokens=10))
    )

    result = await complete(client)

    assert result.usage == TokenUsage(input_tokens=10)


# ── validation retry ──────────────────────────────────────────────────


def invalid_then_valid() -> tuple[FakeResponse, FakeResponse]:
    """A response missing the required field, followed by a good one."""
    return (
        FakeResponse(text='{"score": 3}', usage=FakeUsage(input_tokens=1000, output_tokens=500)),
        FakeResponse(
            parsed_output=Answer(name="Ivan"),
            usage=FakeUsage(input_tokens=1000, output_tokens=500),
        ),
    )


async def test_a_payload_that_fails_validation_earns_one_retry(make_client: MakeClient) -> None:
    """Structured outputs make malformed answers rare, not impossible; one retry
    rescues the call instead of failing an upload the user is waiting on."""
    client, fake = make_client(*invalid_then_valid())

    result = await complete(client)

    assert result.value == Answer(name="Ivan")
    assert result.attempts == 2
    assert len(fake.messages.calls) == 2


async def test_the_retry_tells_the_model_what_the_validator_rejected(
    make_client: MakeClient,
) -> None:
    """Asking again with no explanation just re-rolls the dice; quoting the
    validator is what makes the second attempt likelier to succeed."""
    client, fake = make_client(*invalid_then_valid())

    await complete(client)

    retry_messages = fake.messages.calls[1]["messages"]
    assert [message["role"] for message in retry_messages] == ["user", "assistant", "user"]
    assert retry_messages[1]["content"] == '{"score": 3}'
    complaint = retry_messages[2]["content"]
    assert "name" in complaint
    assert "Field required" in complaint


async def test_a_retried_call_reports_the_tokens_of_both_attempts(
    make_client: MakeClient,
) -> None:
    """A retry costs twice. Reporting only the successful attempt would understate
    the bill by exactly the amount the failure wasted."""
    client, _ = make_client(*invalid_then_valid())

    result = await complete(client)

    assert result.usage == TokenUsage(input_tokens=2000, output_tokens=1000)
    # Double the 0.007 a single 1000-in/500-out call costs on the hot model.
    assert result.cost_usd == pytest.approx(0.014)


async def test_a_response_with_no_text_at_all_counts_as_invalid(make_client: MakeClient) -> None:
    """There is nothing to validate and nothing to parse, so it must be treated as
    a failed answer rather than as an object with every field missing."""
    client, fake = make_client(FakeResponse(), FakeResponse(parsed_output=Answer(name="Ivan")))

    result = await complete(client)

    assert result.attempts == 2
    assert len(fake.messages.calls) == 2


async def test_two_invalid_payloads_raise_an_error_naming_the_prompt(
    make_client: MakeClient,
) -> None:
    """After two failures the caller should fall back, not keep paying — and the
    error has to say which prompt is misbehaving to be actionable."""
    client, fake = make_client(
        FakeResponse(text="not json at all"), FakeResponse(text='{"score": 3}')
    )

    with pytest.raises(LLMError) as excinfo:
        await complete(client)

    assert PROMPT_NAME in str(excinfo.value)
    assert len(fake.messages.calls) == 2


# ── refusals ──────────────────────────────────────────────────────────


async def test_a_refusal_with_empty_content_raises_a_refusal_error(
    make_client: MakeClient,
) -> None:
    """A refusal is HTTP 200 with nothing in it. Reading that as data would turn
    "the model declined" into "this resume is empty", which is a silent wrong
    answer instead of a loud failure."""
    client, _ = make_client(FakeResponse(stop_reason="refusal"))

    with pytest.raises(LLMRefusalError):
        await complete(client)


async def test_a_refusal_is_not_treated_as_a_validation_failure(
    make_client: MakeClient,
) -> None:
    """Retrying a refusal buys a second refusal at full price, and the resulting
    error would blame the schema for a decision the classifier made."""
    client, fake = make_client(FakeResponse(stop_reason="refusal"))

    with pytest.raises(LLMRefusalError) as excinfo:
        await complete(client)

    assert len(fake.messages.calls) == 1
    assert "validation" not in str(excinfo.value)
    assert excinfo.value.problem_type == "llm-refusal"


async def test_a_refusal_carries_the_category_the_api_reported(make_client: MakeClient) -> None:
    """Which classifier declined is the only clue an operator gets; dropping it
    makes every refusal look identical in the error response."""
    client, _ = make_client(FakeResponse(stop_reason="refusal", refusal_category="pii"))

    with pytest.raises(LLMRefusalError) as excinfo:
        await complete(client)

    assert excinfo.value.to_problem()["category"] == "pii"


# ── what reaches the API ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tier", "expected"),
    [(LLMTier.HOT, HOT_MODEL), (LLMTier.HEAVY, HEAVY_MODEL)],
)
async def test_the_tier_decides_which_model_id_is_sent(
    make_client: MakeClient, tier: LLMTier, expected: str
) -> None:
    """The tiers exist to keep the thousands-per-run hot path off the expensive
    model; if the tier did not reach the request, the saving is imaginary."""
    client, fake = make_client(FakeResponse(parsed_output=Answer(name="Ivan")))

    result = await complete(client, tier=tier)

    assert fake.messages.calls[0]["model"] == expected
    assert result.model == expected


async def test_the_hot_tier_is_the_default(make_client: MakeClient) -> None:
    """A caller that forgets to choose must land on the cheap model, not the one
    that costs several times more per token."""
    client, fake = make_client(FakeResponse(parsed_output=Answer(name="Ivan")))

    await complete(client)

    assert fake.messages.calls[0]["model"] == HOT_MODEL


@pytest.mark.parametrize("effort", ["low", "max"])
async def test_effort_is_chosen_per_call(make_client: MakeClient, effort: str) -> None:
    """One global effort setting would either overspend on the hot path or
    underthink on resume extraction; it only works if the call site's choice
    actually reaches the request."""
    client, fake = make_client(FakeResponse(parsed_output=Answer(name="Ivan")))

    await complete(client, effort=effort)

    assert fake.messages.calls[0]["output_config"] == {"effort": effort}


async def test_the_rendered_prompt_is_the_last_content_block(make_client: MakeClient) -> None:
    """The prompt has to arrive filled in: an unrendered ``{{subject}}`` would
    make the model answer about a placeholder."""
    client, fake = make_client(FakeResponse(parsed_output=Answer(name="Ivan")))

    await complete(client)

    content = fake.messages.calls[0]["messages"][0]["content"]
    assert content[-1] == {"type": "text", "text": f"Extract structured data about {SUBJECT}."}


async def test_a_pdf_is_sent_as_a_document_block(make_client: MakeClient) -> None:
    """Resumes are laid out in two columns and line-oriented text extraction reads
    straight across them; the model has to see the file, not somebody's flattened
    transcription of it."""
    client, fake = make_client(FakeResponse(parsed_output=Answer(name="Ivan")))
    pdf = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\nnot really a pdf"

    await complete(client, documents=[Document(content=pdf)])

    block = fake.messages.calls[0]["messages"][0]["content"][0]
    assert block["type"] == "document"
    assert block["source"]["media_type"] == "application/pdf"
    assert base64.standard_b64decode(block["source"]["data"]) == pdf


async def test_max_tokens_falls_back_to_the_configured_ceiling(make_client: MakeClient) -> None:
    """An unset limit must come from configuration rather than from the SDK's own
    default, which is not the number this project budgeted for."""
    client, fake = make_client(FakeResponse(parsed_output=Answer(name="Ivan")))

    await complete(client)

    assert fake.messages.calls[0]["max_tokens"] == 4321


async def test_a_caller_can_raise_the_token_ceiling_for_one_call(
    make_client: MakeClient,
) -> None:
    """A resume extraction needs a far bigger answer than a vacancy re-rank. If the
    per-call limit were ignored in favour of the global one, the long answer would
    be truncated mid-JSON and read back as a validation failure."""
    client, fake = make_client(FakeResponse(parsed_output=Answer(name="Ivan")))

    await complete(client, max_tokens=64_000)

    assert fake.messages.calls[0]["max_tokens"] == 64_000


# ── transport retries ─────────────────────────────────────────────────


async def test_rate_limits_are_retried_until_the_call_succeeds(make_client: MakeClient) -> None:
    """A 429 is the server asking us to wait, not a failure; giving up on it would
    abort a pipeline run halfway through for a condition that clears itself."""
    client, fake = make_client(
        api_error(anthropic.RateLimitError, 429),
        api_error(anthropic.RateLimitError, 429),
        FakeResponse(parsed_output=Answer(name="Ivan")),
    )

    result = await complete(client)

    assert len(fake.messages.calls) == 3
    # Transport retries are invisible to the caller: the answer validated first try.
    assert result.attempts == 1


async def test_retries_stop_at_the_configured_limit(make_client: MakeClient) -> None:
    """Retrying forever turns a sustained outage into a hung pipeline run."""
    client, fake = make_client(*[api_error(anthropic.RateLimitError, 429) for _ in range(3)])

    with pytest.raises(anthropic.RateLimitError):
        await complete(client)

    assert len(fake.messages.calls) == 3  # anthropic_max_retries=2, so 1 + 2


async def test_a_bad_request_is_not_retried(make_client: MakeClient) -> None:
    """A 400 means our request is malformed. Retrying it burns quota and delays
    the traceback that would tell us which field is wrong."""
    client, fake = make_client(api_error(anthropic.BadRequestError, 400))

    with pytest.raises(anthropic.BadRequestError):
        await complete(client)

    assert len(fake.messages.calls) == 1


# ── logging ───────────────────────────────────────────────────────────


async def test_the_call_is_logged_with_tokens_and_cost_but_never_the_prompt(
    make_client: MakeClient,
) -> None:
    """Cost tracking lives in these lines, and resume text must never appear in
    them: logs are shipped, retained and read by people who were never given
    the candidate's contact details."""
    client, _ = make_client(
        FakeResponse(
            parsed_output=Answer(name="Ivan"),
            usage=FakeUsage(input_tokens=1000, output_tokens=500),
        )
    )

    with structlog.testing.capture_logs() as captured:
        await complete(client)

    line = next(entry for entry in captured if entry["event"] == "llm.call")
    assert line["input_tokens"] == 1000
    assert line["output_tokens"] == 500
    assert line["cost_usd"] == pytest.approx(0.007)
    assert line["model"] == HOT_MODEL
    assert line["outcome"] == "ok"
    assert SUBJECT not in str(line)


async def test_a_refusal_is_logged_before_it_is_raised(make_client: MakeClient) -> None:
    """A refused call still consumed tokens; leaving it out of the log makes the
    run's reported cost lower than the invoice."""
    client, _ = make_client(FakeResponse(stop_reason="refusal", usage=FakeUsage(input_tokens=1000)))

    with structlog.testing.capture_logs() as captured, pytest.raises(LLMRefusalError):
        await complete(client)

    line = next(entry for entry in captured if entry["event"] == "llm.call")
    assert line["outcome"] == "refusal"
    assert line["input_tokens"] == 1000


# ── pricing ───────────────────────────────────────────────────────────


def test_cost_is_the_sum_of_every_priced_bucket(llm_env: None) -> None:
    """Cached tokens are cheap but not free, and output costs several times input;
    pricing only one bucket silently under-reports every call."""
    usage = TokenUsage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )

    assert cost_usd(HOT_MODEL, usage) == pytest.approx(2.0 + 10.0 + 0.2 + 2.5)


def test_an_unpriced_model_costs_an_unknown_amount_not_zero(llm_env: None) -> None:
    """Zero would flow into a per-run budget as headroom that does not exist; None
    makes the missing price sheet visible."""
    assert cost_usd("model-nobody-priced", TokenUsage(input_tokens=1_000_000)) is None
    assert pricing_for("model-nobody-priced") is None


def test_unset_buckets_default_to_free_rather_than_to_the_input_price(llm_env: None) -> None:
    """The heavy entry omits cache prices; guessing them from the input price would
    invent a charge the provider never made."""
    usage = TokenUsage(cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)

    assert cost_usd(HEAVY_MODEL, usage) == pytest.approx(0.0)


def test_the_shipped_price_table_covers_both_configured_tiers() -> None:
    """Every call the app makes on its defaults must report a real cost; a default
    model missing from the table reports None for the whole pipeline run."""
    shipped = default_pricing()
    fields = Settings.model_fields

    assert fields["anthropic_model"].default in shipped
    assert fields["anthropic_model_heavy"].default in shipped


def test_adding_usage_accumulates_every_field() -> None:
    """One operation can span several calls; a field left out of the addition
    disappears from the total and from the cost derived from it."""
    first = TokenUsage(input_tokens=1, output_tokens=2, cache_read_tokens=3, cache_write_tokens=4)
    second = TokenUsage(
        input_tokens=10, output_tokens=20, cache_read_tokens=30, cache_write_tokens=40
    )

    assert first + second == TokenUsage(
        input_tokens=11, output_tokens=22, cache_read_tokens=33, cache_write_tokens=44
    )


def test_total_counts_cached_tokens_too() -> None:
    """Cache reads are billable; excluding them makes a cache-heavy run look
    smaller than it was."""
    usage = TokenUsage(input_tokens=1, output_tokens=2, cache_read_tokens=4, cache_write_tokens=8)

    assert usage.total == 15


# ── prompts ───────────────────────────────────────────────────────────


def test_render_fills_every_placeholder(llm_env: None) -> None:
    """Placeholders are ``{{name}}`` rather than ``str.format`` because prompts are
    full of JSON braces; the substitution has to work anyway."""
    rendered = prompts.render(PROMPT_NAME, subject="Ivan")

    assert rendered == "Extract structured data about Ivan."
    assert "{{" not in rendered


def test_a_missing_variable_is_an_error_not_an_empty_string(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A prompt silently missing half its context produces plausible nonsense, and
    plausible nonsense is the hardest kind of bug to notice downstream."""
    prompts.load.cache_clear()
    monkeypatch.setattr(prompts, "PROMPT_DIR", tmp_path)
    (tmp_path / "two.md").write_text("{{given}} and {{forgotten}}", encoding="utf-8")

    with pytest.raises(KeyError) as excinfo:
        prompts.render("two", given="here")

    assert "forgotten" in str(excinfo.value)
    prompts.load.cache_clear()


def test_braces_that_are_not_placeholders_survive_rendering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Prompts contain JSON examples; mangling them would change the schema the
    model is being shown."""
    prompts.load.cache_clear()
    monkeypatch.setattr(prompts, "PROMPT_DIR", tmp_path)
    (tmp_path / "json.md").write_text('{"skills": [], "for": "{{who}}"}', encoding="utf-8")

    assert prompts.render("json", who="Ivan") == '{"skills": [], "for": "Ivan"}'
    prompts.load.cache_clear()


def test_an_unknown_prompt_name_names_the_ones_that_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A typo in a prompt name is a deploy-time mistake; the error has to point at
    the fix rather than at a missing file path."""
    prompts.load.cache_clear()
    monkeypatch.setattr(prompts, "PROMPT_DIR", tmp_path)
    (tmp_path / "real.md").write_text("hi", encoding="utf-8")

    with pytest.raises(prompts.PromptNotFoundError) as excinfo:
        prompts.load("imaginary")

    assert "real" in str(excinfo.value)
    prompts.load.cache_clear()


def test_the_real_extract_profile_prompt_loads_and_renders() -> None:
    """The one prompt the product actually ships must keep rendering: a renamed
    placeholder in the .md file would only surface on a live upload."""
    prompts.load.cache_clear()
    rendered = prompts.render(
        "extract_profile", today="2026-09-03", resume_text="RESUME BODY MARKER"
    )

    assert "2026-09-03" in rendered
    assert "{{" not in rendered
    assert "YYYY-MM" in rendered
    # The resume must actually reach the model. It once did not: the template
    # had no placeholder for it, render only complained about the other
    # direction, and every non-PDF upload was extracted from an empty document
    # while the prompt still claimed the resume was included below.
    assert "RESUME BODY MARKER" in rendered


def test_a_variable_the_template_never_uses_is_an_error() -> None:
    """Passing a variable with no placeholder means the caller believes it is
    sending something the model will never see — which is exactly how the resume
    text went missing. Silence there is worse than a failure."""
    prompts.load.cache_clear()

    with pytest.raises(KeyError, match="no placeholder"):
        prompts.render("extract_profile", today="x", resume_text="y", unused="z")
