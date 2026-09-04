"""The Anthropic provider: what it costs, what it retries, what it refuses to guess.

Every call in this file is driven by a hand-built stand-in for ``AsyncAnthropic``
injected through ``AnthropicAPIProvider(client=...)``. Nothing here touches the
network and nothing needs an API key: the provider's whole job is policy — model
selection per task, effort defaults, retry rules, refusal handling and cost
accounting — and policy is exactly what a live call would hide behind a
plausible-looking answer.
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
from pydantic import BaseModel, SecretStr
from tenacity import wait_none

from app.core.config import ModelPricing, Settings, default_pricing, settings
from app.core.exceptions import LLMError
from app.llm import prompts
from app.llm.base import BatchItem, Document, Effort, LLMTask
from app.llm.pricing import TokenUsage, cost_usd, pricing_for
from app.llm.providers import anthropic_api as provider_module
from app.llm.providers.anthropic_api import (
    HEAVY_TASKS,
    AnthropicAPIProvider,
    LLMRefusalError,
)

pytestmark = pytest.mark.unit

#: Deliberately not the real model ids: a test that passes because the default
#: happened to match would not prove the task was consulted at all.
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

#: One distinct effort per task, so a test can tell "the table was consulted"
#: apart from "everything happens to be low".
TASK_EFFORT: dict[str, Effort] = {
    LLMTask.RESUME_EXTRACTION.value: "max",
    LLMTask.COVER_LETTER.value: "high",
    LLMTask.TOOLING.value: "high",
    LLMTask.VACANCY_PARSE.value: "medium",
    LLMTask.TELEGRAM_PARSE.value: "low",
    LLMTask.RERANK.value: "low",
}

#: The task every helper defaults to: a hot-path one, so a test that forgets to
#: choose does not accidentally prove the heavy branch.
DEFAULT_TASK = LLMTask.RERANK

PROMPT_NAME = "demo"
PROMPT_TEMPLATE = "Extract structured data about {{subject}}."
#: Stands in for resume content: if this string ever reaches a log line, the
#: provider is leaking the document it was given.
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
    """A ``Message``-shaped object carrying only what the provider reads off it."""

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


MakeProvider = Callable[..., tuple[AnthropicAPIProvider, FakeAnthropic]]


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
    monkeypatch.setattr(settings, "llm_task_effort", TASK_EFFORT)
    monkeypatch.setattr(settings, "anthropic_max_tokens", 4321)
    monkeypatch.setattr(settings, "anthropic_max_retries", 2)
    # Real jitter would make the retry tests take seconds for nothing.
    monkeypatch.setattr(provider_module, "wait_exponential_jitter", lambda **_: wait_none())
    yield
    prompts.load.cache_clear()


@pytest.fixture
def make_provider(llm_env: None) -> MakeProvider:
    """Build a provider over a scripted transport, returning both halves."""

    def _make(*outcomes: object) -> tuple[AnthropicAPIProvider, FakeAnthropic]:
        fake = FakeAnthropic(*outcomes)
        return AnthropicAPIProvider(client=fake), fake  # type: ignore[arg-type]

    return _make


async def complete(provider: AnthropicAPIProvider, **overrides: Any) -> Any:
    """Run the provider's one public entry point with this file's defaults."""
    kwargs: dict[str, Any] = {
        "task": DEFAULT_TASK,
        "variables": {"subject": SUBJECT},
    }
    kwargs.update(overrides)
    return await provider.complete_json(PROMPT_NAME, Answer, **kwargs)


# ── availability ──────────────────────────────────────────────────────


async def test_the_provider_is_available_only_with_a_key(
    make_provider: MakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The router asks this before routing a task here. Answering yes without a
    key would send the call down a path that can only fail with a 401, instead
    of falling back to a provider that can actually answer."""
    provider, _ = make_provider()

    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("sk-not-a-real-key"))
    assert provider.is_available() is True

    monkeypatch.setattr(settings, "anthropic_api_key", None)
    assert provider.is_available() is False


async def test_availability_costs_nothing_to_ask(
    make_provider: MakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The router calls this before every single call; a probe that talked to the
    API would put a network round trip in front of each one."""
    provider, fake = make_provider()
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("sk-not-a-real-key"))

    provider.is_available()

    assert fake.messages.calls == []


# ── the happy path ────────────────────────────────────────────────────


async def test_first_valid_response_is_returned_without_a_retry(
    make_provider: MakeProvider,
) -> None:
    """The common case must cost exactly one call: a provider that quietly asks
    twice doubles the invoice for every vacancy in a pipeline run."""
    provider, fake = make_provider(
        FakeResponse(parsed_output=Answer(name="Ivan", score=7), text='{"name": "Ivan"}')
    )

    result = await complete(provider)

    assert result.value == Answer(name="Ivan", score=7)
    assert result.attempts == 1
    assert len(fake.messages.calls) == 1


async def test_structured_output_is_preferred_over_reparsing_the_text(
    make_provider: MakeProvider,
) -> None:
    """``parsed_output`` is what the API actually constrained; re-reading the text
    block would silently accept a value the schema never validated."""
    provider, _ = make_provider(
        FakeResponse(parsed_output=Answer(name="from-parsed"), text='{"name": "from-text"}')
    )

    result = await complete(provider)

    assert result.value.name == "from-parsed"


async def test_the_schema_is_sent_so_the_api_constrains_generation(
    make_provider: MakeProvider,
) -> None:
    """Structured outputs are what make malformed answers rare enough for a single
    retry to be an adequate policy. Omit the schema from the request and the model
    is free-generating JSON — the provider still works, just at a failure rate the
    retry budget was never sized for."""
    provider, fake = make_provider(FakeResponse(parsed_output=Answer(name="Ivan")))

    await complete(provider)

    assert fake.messages.calls[0]["output_format"] is Answer


async def test_usage_and_cost_come_back_with_the_answer(make_provider: MakeProvider) -> None:
    """A caller that cannot see what a call cost cannot enforce a per-run budget,
    and the overspend is only discovered on the invoice."""
    provider, _ = make_provider(
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

    result = await complete(provider)

    usage = result.usage
    assert usage.model == HOT_MODEL
    assert (usage.input_tokens, usage.output_tokens) == (1000, 500)
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (2000, 400)
    # 1000*2 + 500*10 + 2000*0.2 + 400*2.5 = 8400 per million.
    assert usage.cost_usd == pytest.approx(0.0084)


async def test_usage_says_which_provider_answered_and_how_the_cost_was_obtained(
    make_provider: MakeProvider,
) -> None:
    """The ledger sums per provider and must never add API dollars to CLI
    subscription quota. Both facts have to travel with the number, because after
    the call returns there is nothing left to infer them from."""
    provider, _ = make_provider(FakeResponse(parsed_output=Answer(name="Ivan")))

    result = await complete(provider)

    assert result.usage.provider == "api"
    assert result.usage.accounting == "measured"


async def test_the_task_travels_with_the_usage(make_provider: MakeProvider) -> None:
    """Cost per task is the only view that answers "is re-rank worth it?"; a usage
    record that has forgotten what it was for cannot be attributed to anything."""
    provider, _ = make_provider(FakeResponse(parsed_output=Answer(name="Ivan")))

    result = await complete(provider, task=LLMTask.VACANCY_PARSE)

    assert result.usage.task is LLMTask.VACANCY_PARSE


async def test_missing_usage_counters_are_read_as_zero(make_provider: MakeProvider) -> None:
    """An API version that omits the cache counters must not crash the call:
    unknown cache usage is zero cache usage, not a failed extraction."""
    provider, _ = make_provider(
        FakeResponse(parsed_output=Answer(name="Ivan"), usage=FakeUsage(input_tokens=10))
    )

    result = await complete(provider)

    assert result.usage.input_tokens == 10
    assert result.usage.cache_read_tokens == 0
    assert result.usage.cache_write_tokens == 0


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


async def test_a_payload_that_fails_validation_earns_one_retry(
    make_provider: MakeProvider,
) -> None:
    """Structured outputs make malformed answers rare, not impossible; one retry
    rescues the call instead of failing an upload the user is waiting on."""
    provider, fake = make_provider(*invalid_then_valid())

    result = await complete(provider)

    assert result.value == Answer(name="Ivan")
    assert result.attempts == 2
    assert len(fake.messages.calls) == 2


async def test_the_retry_tells_the_model_what_the_validator_rejected(
    make_provider: MakeProvider,
) -> None:
    """Asking again with no explanation just re-rolls the dice; quoting the
    validator is what makes the second attempt likelier to succeed."""
    provider, fake = make_provider(*invalid_then_valid())

    await complete(provider)

    retry_messages = fake.messages.calls[1]["messages"]
    assert [message["role"] for message in retry_messages] == ["user", "assistant", "user"]
    assert retry_messages[1]["content"] == '{"score": 3}'
    complaint = retry_messages[2]["content"]
    assert "name" in complaint
    assert "Field required" in complaint


async def test_a_retried_call_reports_the_tokens_of_both_attempts(
    make_provider: MakeProvider,
) -> None:
    """A retry costs twice. Reporting only the successful attempt would understate
    the bill by exactly the amount the failure wasted."""
    provider, _ = make_provider(*invalid_then_valid())

    result = await complete(provider)

    assert (result.usage.input_tokens, result.usage.output_tokens) == (2000, 1000)
    # Double the 0.007 a single 1000-in/500-out call costs on the hot model.
    assert result.usage.cost_usd == pytest.approx(0.014)


async def test_a_response_with_no_text_at_all_counts_as_invalid(
    make_provider: MakeProvider,
) -> None:
    """There is nothing to validate and nothing to parse, so it must be treated as
    a failed answer rather than as an object with every field missing."""
    provider, fake = make_provider(FakeResponse(), FakeResponse(parsed_output=Answer(name="Ivan")))

    result = await complete(provider)

    assert result.attempts == 2
    assert len(fake.messages.calls) == 2


async def test_two_invalid_payloads_raise_an_error_naming_the_prompt(
    make_provider: MakeProvider,
) -> None:
    """After two failures the caller should fall back, not keep paying — and the
    error has to say which prompt is misbehaving to be actionable."""
    provider, fake = make_provider(
        FakeResponse(text="not json at all"), FakeResponse(text='{"score": 3}')
    )

    with pytest.raises(LLMError) as excinfo:
        await complete(provider)

    assert PROMPT_NAME in str(excinfo.value)
    assert len(fake.messages.calls) == 2


# ── refusals ──────────────────────────────────────────────────────────


async def test_a_refusal_with_empty_content_raises_a_refusal_error(
    make_provider: MakeProvider,
) -> None:
    """A refusal is HTTP 200 with nothing in it. Reading that as data would turn
    "the model declined" into "this resume is empty", which is a silent wrong
    answer instead of a loud failure."""
    provider, _ = make_provider(FakeResponse(stop_reason="refusal"))

    with pytest.raises(LLMRefusalError):
        await complete(provider)


async def test_a_refusal_is_not_treated_as_a_validation_failure(
    make_provider: MakeProvider,
) -> None:
    """Retrying a refusal buys a second refusal at full price, and the resulting
    error would blame the schema for a decision the classifier made."""
    provider, fake = make_provider(FakeResponse(stop_reason="refusal"))

    with pytest.raises(LLMRefusalError) as excinfo:
        await complete(provider)

    assert len(fake.messages.calls) == 1
    assert "validation" not in str(excinfo.value)
    assert excinfo.value.problem_type == "llm-refusal"


async def test_a_refusal_carries_the_category_the_api_reported(
    make_provider: MakeProvider,
) -> None:
    """Which classifier declined is the only clue an operator gets; dropping it
    makes every refusal look identical in the error response."""
    provider, _ = make_provider(FakeResponse(stop_reason="refusal", refusal_category="pii"))

    with pytest.raises(LLMRefusalError) as excinfo:
        await complete(provider)

    assert excinfo.value.to_problem()["category"] == "pii"


# ── what reaches the API ──────────────────────────────────────────────


@pytest.mark.parametrize("task", list(LLMTask))
async def test_the_task_decides_which_model_id_is_sent(
    make_provider: MakeProvider, task: LLMTask
) -> None:
    """The two models exist to keep the thousands-per-run hot path off the
    expensive one; if the task did not reach the request, the saving is
    imaginary. Parametrised over every task so a newly added one cannot default
    into the heavy model unnoticed."""
    expected = HEAVY_MODEL if task in HEAVY_TASKS else HOT_MODEL
    provider, fake = make_provider(FakeResponse(parsed_output=Answer(name="Ivan")))

    result = await complete(provider, task=task)

    assert fake.messages.calls[0]["model"] == expected
    assert result.usage.model == expected


def test_only_the_rare_high_quality_tasks_are_heavy() -> None:
    """Resume extraction, cover letters and tooling run a handful of times each;
    re-rank and post parsing run thousands. Letting a hot task into this set is
    how a pipeline run silently costs several times what it budgeted for."""
    assert set(HEAVY_TASKS) == {LLMTask.RESUME_EXTRACTION, LLMTask.COVER_LETTER, LLMTask.TOOLING}


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        (LLMTask.RESUME_EXTRACTION, "max"),
        (LLMTask.VACANCY_PARSE, "medium"),
        (LLMTask.RERANK, "low"),
    ],
)
async def test_effort_defaults_to_the_configured_value_for_the_task(
    make_provider: MakeProvider, task: LLMTask, expected: str
) -> None:
    """Effort is per task, not global: one setting would either overspend on
    re-rank or underthink resume extraction. Callers pass no effort at all, so
    the whole policy lives in the config table — and only works if the provider
    reads it instead of falling back to some hardcoded level."""
    provider, fake = make_provider(FakeResponse(parsed_output=Answer(name="Ivan")))

    await complete(provider, task=task)

    assert fake.messages.calls[0]["output_config"] == {"effort": expected}


async def test_a_caller_supplied_effort_beats_the_configured_default(
    make_provider: MakeProvider,
) -> None:
    """The table is a default, not a ceiling: a retry or a one-off tooling call
    needs to think harder than the task usually does. If the config won here,
    that escape hatch would be silently ignored."""
    provider, fake = make_provider(FakeResponse(parsed_output=Answer(name="Ivan")))

    await complete(provider, task=LLMTask.RERANK, effort="max")

    assert fake.messages.calls[0]["output_config"] == {"effort": "max"}


async def test_the_rendered_prompt_is_the_last_content_block(
    make_provider: MakeProvider,
) -> None:
    """The prompt has to arrive filled in: an unrendered ``{{subject}}`` would
    make the model answer about a placeholder."""
    provider, fake = make_provider(FakeResponse(parsed_output=Answer(name="Ivan")))

    await complete(provider)

    content = fake.messages.calls[0]["messages"][0]["content"]
    assert content[-1] == {"type": "text", "text": f"Extract structured data about {SUBJECT}."}


async def test_a_pdf_is_sent_as_a_document_block(make_provider: MakeProvider) -> None:
    """Resumes are laid out in two columns and line-oriented text extraction reads
    straight across them; the model has to see the file, not somebody's flattened
    transcription of it."""
    provider, fake = make_provider(FakeResponse(parsed_output=Answer(name="Ivan")))
    pdf = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\nnot really a pdf"

    await complete(provider, documents=[Document(content=pdf)])

    block = fake.messages.calls[0]["messages"][0]["content"][0]
    assert block["type"] == "document"
    assert block["source"]["media_type"] == "application/pdf"
    assert base64.standard_b64decode(block["source"]["data"]) == pdf


async def test_max_tokens_falls_back_to_the_configured_ceiling(
    make_provider: MakeProvider,
) -> None:
    """An unset limit must come from configuration rather than from the SDK's own
    default, which is not the number this project budgeted for."""
    provider, fake = make_provider(FakeResponse(parsed_output=Answer(name="Ivan")))

    await complete(provider)

    assert fake.messages.calls[0]["max_tokens"] == 4321


async def test_a_caller_can_raise_the_token_ceiling_for_one_call(
    make_provider: MakeProvider,
) -> None:
    """A resume extraction needs a far bigger answer than a vacancy re-rank. If the
    per-call limit were ignored in favour of the global one, the long answer would
    be truncated mid-JSON and read back as a validation failure."""
    provider, fake = make_provider(FakeResponse(parsed_output=Answer(name="Ivan")))

    await complete(provider, max_tokens=64_000)

    assert fake.messages.calls[0]["max_tokens"] == 64_000


# ── batching ──────────────────────────────────────────────────────────


async def test_a_batch_answers_every_item_in_order(make_provider: MakeProvider) -> None:
    """This provider is billed per token, so batching buys it nothing and the
    inherited loop is the right implementation — but the caller cannot tell which
    provider it got, and pairs each answer with its input by position. Losing the
    order would attach every vacancy's score to the wrong vacancy."""
    provider, fake = make_provider(
        FakeResponse(parsed_output=Answer(name="first")),
        FakeResponse(parsed_output=Answer(name="second")),
        FakeResponse(parsed_output=Answer(name="third")),
    )
    items = [BatchItem(variables={"subject": name}) for name in ("a", "b", "c")]

    results = await provider.complete_json_batch(PROMPT_NAME, Answer, items, task=LLMTask.RERANK)

    assert [result.value.name for result in results] == ["first", "second", "third"]
    assert len(fake.messages.calls) == 3


async def test_each_batch_item_sends_its_own_variables(make_provider: MakeProvider) -> None:
    """One shared rendering would ask the same question three times and return
    three answers about the first item — an error no downstream assertion about
    result count would catch."""
    provider, fake = make_provider(
        FakeResponse(parsed_output=Answer(name="first")),
        FakeResponse(parsed_output=Answer(name="second")),
    )
    items = [BatchItem(variables={"subject": "alpha"}), BatchItem(variables={"subject": "beta"})]

    await provider.complete_json_batch(PROMPT_NAME, Answer, items, task=LLMTask.RERANK)

    sent = [call["messages"][0]["content"][-1]["text"] for call in fake.messages.calls]
    assert sent == [
        "Extract structured data about alpha.",
        "Extract structured data about beta.",
    ]


# ── transport retries ─────────────────────────────────────────────────


async def test_rate_limits_are_retried_until_the_call_succeeds(
    make_provider: MakeProvider,
) -> None:
    """A 429 is the server asking us to wait, not a failure; giving up on it would
    abort a pipeline run halfway through for a condition that clears itself."""
    provider, fake = make_provider(
        api_error(anthropic.RateLimitError, 429),
        api_error(anthropic.RateLimitError, 429),
        FakeResponse(parsed_output=Answer(name="Ivan")),
    )

    result = await complete(provider)

    assert len(fake.messages.calls) == 3
    # Transport retries are invisible to the caller: the answer validated first try.
    assert result.attempts == 1


async def test_retries_stop_at_the_configured_limit(make_provider: MakeProvider) -> None:
    """Retrying forever turns a sustained outage into a hung pipeline run."""
    provider, fake = make_provider(*[api_error(anthropic.RateLimitError, 429) for _ in range(3)])

    with pytest.raises(anthropic.RateLimitError):
        await complete(provider)

    assert len(fake.messages.calls) == 3  # anthropic_max_retries=2, so 1 + 2


async def test_a_validation_retry_does_not_get_a_fresh_retry_budget(
    make_provider: MakeProvider,
) -> None:
    """The configured ceiling counts requests per operation, not per attempt. If
    the validation retry opened its own transport policy, one logical call could
    issue twice the requests the configuration promises — unnoticed by the
    per-run cost cap, and at its worst exactly when the API is already
    struggling."""
    provider, fake = make_provider(
        api_error(anthropic.RateLimitError, 429),
        api_error(anthropic.RateLimitError, 429),
        FakeResponse(text='{"score": 3}'),  # spends the third and last request
        api_error(anthropic.RateLimitError, 429),
        # Reached only if the second attempt was handed a rebuilt budget.
        api_error(anthropic.RateLimitError, 429),
        FakeResponse(parsed_output=Answer(name="Ivan")),
    )

    with pytest.raises(anthropic.RateLimitError):
        await complete(provider)

    assert len(fake.messages.calls) == 4


async def test_a_bad_request_is_not_retried(make_provider: MakeProvider) -> None:
    """A 400 means our request is malformed. Retrying it burns quota and delays
    the traceback that would tell us which field is wrong."""
    provider, fake = make_provider(api_error(anthropic.BadRequestError, 400))

    with pytest.raises(anthropic.BadRequestError):
        await complete(provider)

    assert len(fake.messages.calls) == 1


# ── logging ───────────────────────────────────────────────────────────


async def test_the_call_is_logged_with_tokens_and_cost_but_never_the_prompt(
    make_provider: MakeProvider,
) -> None:
    """Cost tracking lives in these lines, and resume text must never appear in
    them: logs are shipped, retained and read by people who were never given
    the candidate's contact details."""
    provider, _ = make_provider(
        FakeResponse(
            parsed_output=Answer(name="Ivan"),
            usage=FakeUsage(input_tokens=1000, output_tokens=500),
        )
    )

    with structlog.testing.capture_logs() as captured:
        await complete(provider)

    line = next(entry for entry in captured if entry["event"] == "llm.call")
    assert line["input_tokens"] == 1000
    assert line["output_tokens"] == 500
    assert line["cost_usd"] == pytest.approx(0.007)
    assert line["model"] == HOT_MODEL
    assert line["outcome"] == "ok"
    assert SUBJECT not in str(line)


async def test_a_refusal_is_logged_before_it_is_raised(make_provider: MakeProvider) -> None:
    """A refused call still consumed tokens; leaving it out of the log makes the
    run's reported cost lower than the invoice."""
    provider, _ = make_provider(
        FakeResponse(stop_reason="refusal", usage=FakeUsage(input_tokens=1000))
    )

    with structlog.testing.capture_logs() as captured, pytest.raises(LLMRefusalError):
        await complete(provider)

    line = next(entry for entry in captured if entry["event"] == "llm.call")
    assert line["outcome"] == "refusal"
    assert line["input_tokens"] == 1000


async def test_the_log_says_whether_the_schema_or_the_text_produced_the_value(
    make_provider: MakeProvider,
) -> None:
    """A value the API constrained and a text block that merely happened to
    validate are not the same guarantee. Logged identically, a silent fall-off
    from structured outputs — an SDK or model change — would look exactly like
    business as usual."""
    constrained, _ = make_provider(FakeResponse(parsed_output=Answer(name="Ivan")))
    with structlog.testing.capture_logs() as captured:
        await complete(constrained)
    assert next(e for e in captured if e["event"] == "llm.call")["source"] == "structured"

    fallback, _ = make_provider(FakeResponse(text='{"name": "Ivan"}'))
    with structlog.testing.capture_logs() as captured:
        await complete(fallback)
    assert next(e for e in captured if e["event"] == "llm.call")["source"] == "text_fallback"


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


def test_the_shipped_price_table_covers_both_configured_models() -> None:
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
