"""Anthropic client wrapper.

What this adds over the bare SDK:

* **Two models, chosen per call.** ``LLMTier.HOT`` is the cheap path — vacancy
  re-rank and Telegram post parsing, thousands of calls per pipeline run.
  ``LLMTier.HEAVY`` is resume extraction and cover letters, which happen rarely
  and where quality is worth the money.
* **Effort is chosen per call too**, never globally. One global setting would
  either overspend on the hot path or underthink on the cold one; phase 8 turns
  that difference into a real invoice.
* **PDFs go to the model as documents**, not as text somebody extracted first.
  Resumes are usually laid out in two columns and line-oriented text extraction
  reads straight across them, interleaving the sidebar with the body.
* **Refusals are surfaced, not swallowed.** A safety classifier can decline a
  request and the call still returns HTTP 200 with empty content, so
  ``stop_reason`` is checked before the content is read. Server-side fallback to
  another model is deliberately NOT enabled: silently answering from a different
  model would give a different extraction quality with no trace in the logs.
* **Cost accounting on every call**, priced from configuration.
"""

import base64
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types import Message, TextBlock
from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.core.config import settings
from app.core.exceptions import LLMError
from app.core.logging import get_logger
from app.llm import prompts
from app.llm.pricing import TokenUsage, cost_usd

logger = get_logger(__name__)

#: How hard the model should think. Named at the call site, never defaulted
#: globally — see the module docstring.
Effort = Literal["low", "medium", "high", "xhigh", "max"]

#: Transport failures worth retrying. A 400 is a bug in our request and must
#: not be retried; a 429 or a 5xx is the server asking us to wait.
RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.OverloadedError,
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
)


class LLMTier(StrEnum):
    """Which configured model a call should use."""

    HOT = "hot"
    HEAVY = "heavy"


class LLMRefusalError(LLMError):
    """A safety classifier declined the request.

    Its own class so callers can tell "the model would not answer" apart from
    "the model answered something unusable".
    """

    title = "Model declined the request"
    problem_type = "llm-refusal"


@dataclass(frozen=True, slots=True)
class Document:
    """A file handed to the model as-is, so it sees the layout."""

    content: bytes
    media_type: str = "application/pdf"

    def to_block(self) -> dict[str, Any]:
        """The content block the Messages API expects."""
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": self.media_type,
                "data": base64.standard_b64encode(self.content).decode(),
            },
        }


@dataclass(slots=True)
class RetryBudget:
    """Requests one logical operation may still make.

    Shared across the transport retries and the validation retry so the ceiling
    is the one the configuration states, not twice it.
    """

    remaining: int

    def spend(self, attempts: int) -> None:
        """Record requests already made, never dropping below one."""
        self.remaining = max(1, self.remaining - attempts)


@dataclass(frozen=True, slots=True)
class LLMResult[ResultT: BaseModel]:
    """A validated answer plus what it cost to get."""

    value: ResultT
    model: str
    usage: TokenUsage
    cost_usd: float | None
    #: 1 when the model got it right first time, 2 after a validation retry.
    attempts: int


class LLMClient:
    """Everything this project sends to Anthropic goes through here."""

    def __init__(self, client: AsyncAnthropic | None = None) -> None:
        self._client = client or self._build_client()

    @staticmethod
    def _build_client() -> AsyncAnthropic:
        """Construct the SDK client from settings.

        ``max_retries=0`` on purpose: retries are driven by tenacity below so
        the policy is ours, is logged, and is testable.
        """
        key = settings.anthropic_api_key
        return AsyncAnthropic(
            api_key=key.get_secret_value() if key else None,
            timeout=settings.anthropic_timeout,
            max_retries=0,
        )

    @staticmethod
    def model_for(tier: LLMTier) -> str:
        """Resolve a tier to the configured model id."""
        return settings.anthropic_model_heavy if tier is LLMTier.HEAVY else settings.anthropic_model

    async def complete_json[ResultT: BaseModel](
        self,
        prompt_name: str,
        response_model: type[ResultT],
        *,
        effort: Effort,
        tier: LLMTier = LLMTier.HOT,
        variables: dict[str, object] | None = None,
        documents: Sequence[Document] = (),
        max_tokens: int | None = None,
    ) -> LLMResult[ResultT]:
        """Render a prompt, call the model, return a validated object.

        Structured outputs constrain generation against ``response_model``, so
        malformed JSON is rare rather than routine — but the answer is still
        validated here, and a validation failure earns exactly one retry with
        the error text fed back. A second failure is an :class:`LLMError`: at
        that point the caller should fall back, not keep paying.
        """
        model = self.model_for(tier)
        rendered = prompts.render(prompt_name, **(variables or {}))
        content: list[dict[str, Any]] = [document.to_block() for document in documents]
        content.append({"type": "text", "text": rendered})

        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        total = TokenUsage()
        budget = RetryBudget(remaining=settings.anthropic_max_retries + 1)

        for attempt in (1, 2):
            response = await self._call(
                model=model,
                messages=messages,
                response_model=response_model,
                effort=effort,
                max_tokens=max_tokens or settings.anthropic_max_tokens,
                budget=budget,
            )
            total = total + _usage_of(response)

            # Before anything reads content: a refusal is a 200 with nothing in
            # it, and treating that as "the resume was empty" would be worse
            # than failing.
            if response.stop_reason == "refusal":
                self._log(prompt_name, model, total, attempt, "refusal")
                raise LLMRefusalError(
                    "the model declined to process this document",
                    category=_refusal_category(response),
                )

            try:
                value, source = _parsed(response, response_model)
            except (ValidationError, ValueError) as exc:
                if attempt == 2:
                    self._log(prompt_name, model, total, attempt, "invalid")
                    raise LLMError(
                        f"{prompt_name} returned a payload that failed validation twice"
                    ) from exc
                logger.warning("llm.invalid_payload_retrying", prompt=prompt_name, model=model)
                messages = [
                    *messages,
                    {"role": "assistant", "content": _text_of(response) or "(no content)"},
                    {
                        "role": "user",
                        "content": (
                            "That response did not validate against the required schema. "
                            f"The validator said:\n{exc}\n"
                            "Return the corrected object and nothing else."
                        ),
                    },
                ]
                continue

            self._log(prompt_name, model, total, attempt, "ok", source=source)
            return LLMResult(
                value=value,
                model=model,
                usage=total,
                cost_usd=cost_usd(model, total),
                attempts=attempt,
            )

        raise LLMError(f"{prompt_name} exhausted its attempts")  # pragma: no cover

    async def _call(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        response_model: type[BaseModel],
        effort: Effort,
        max_tokens: int,
        budget: RetryBudget,
    ) -> Message:
        """One request, retried on rate limits and server errors.

        The budget is shared with the validation retry rather than being
        rebuilt here. A fresh policy per attempt would double the ceiling
        without saying so: with ``anthropic_max_retries=4`` a pathological call
        could issue ten requests while the configuration promises five, and the
        per-run cost cap would have no way to see it coming.
        """
        retrying = AsyncRetrying(
            retry=retry_if_exception_type(RETRYABLE),
            stop=stop_after_attempt(budget.remaining),
            wait=wait_exponential_jitter(initial=1, max=30),
            reraise=True,
        )
        try:
            async for policy in retrying:
                with policy:
                    response: Message = await self._client.messages.parse(
                        model=model,
                        max_tokens=max_tokens,
                        messages=messages,  # type: ignore[arg-type]
                        output_format=response_model,
                        output_config={"effort": effort},
                        thinking={"type": "adaptive"},
                    )
                    return response
        finally:
            budget.spend(int(retrying.statistics.get("attempt_number", 1)))
        raise LLMError("retry policy exited without a result")  # pragma: no cover

    @staticmethod
    def _log(
        prompt_name: str,
        model: str,
        usage: TokenUsage,
        attempts: int,
        outcome: str,
        *,
        source: str | None = None,
    ) -> None:
        """One structured line per call. Never the prompt, never the document.

        ``source`` says whether the value came from schema-constrained
        generation or from validating a free-form text block. The second is a
        weaker guarantee, and the two must never be indistinguishable after the
        fact.
        """
        logger.info(
            "llm.call",
            prompt=prompt_name,
            model=model,
            outcome=outcome,
            attempts=attempts,
            source=source,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cost_usd=cost_usd(model, usage),
        )


def _usage_of(response: Message) -> TokenUsage:
    """Read the usage block, tolerating fields an API version may omit.

    getattr rather than attribute access: the cache counters are newer than the
    token counters, and an object that omits them entirely — an older payload,
    or a stand-in in a test — should report zero rather than raise.
    """
    usage = response.usage
    return TokenUsage(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )


def _refusal_category(response: Message) -> str | None:
    """Which classifier declined, when the API says."""
    details = getattr(response, "stop_details", None)
    return getattr(details, "category", None)


def _text_of(response: Message) -> str | None:
    """First text block of a response, if there is one.

    A response can also carry thinking and tool blocks, none of which have
    text; isinstance narrows to the one that does.
    """
    for block in response.content:
        if isinstance(block, TextBlock):
            return block.text
    return None


def _parsed[ResultT: BaseModel](
    response: Message, response_model: type[ResultT]
) -> tuple[ResultT, str]:
    """The validated object, plus which path produced it.

    ``messages.parse`` populates ``parsed_output``, which means generation was
    constrained by the schema. Falling back to validating the text block keeps
    the wrapper working against a stand-in transport — but that path is live in
    production too, and text that merely happens to validate is a weaker
    guarantee than text the server constrained. The caller logs which one ran,
    so the two are never silently confused.
    """
    parsed = getattr(response, "parsed_output", None)
    if isinstance(parsed, response_model):
        return parsed, "structured"
    text = _text_of(response)
    if text is None:
        raise ValueError("response carried no text block to validate")
    return response_model.model_validate_json(text), "text_fallback"
