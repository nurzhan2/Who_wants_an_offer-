"""A local Ollama server as an LLM provider.

Intended for one task: turning a Telegram post into a structured vacancy. That
job is simple, there are hundreds of them a night, and none of them is
latency-sensitive — which is the exact shape local inference is good at, and
the exact shape that makes an API bill add up for no benefit.

Two honest caveats, both of which the routing already accounts for.

**It may be too slow to matter.** On CPU-only hardware a 7B model in Q4 runs at
single-digit tokens per second, so 200 posts at ~300 output tokens each is
hours, not minutes. Whether that is acceptable is a question for a measurement,
not an opinion; until that measurement exists the fallback to the API is what
keeps the pipeline moving.

**It has no structured outputs.** ``format="json"`` makes the server emit
syntactically valid JSON, which is not the same as JSON matching a schema. The
answer is validated here and retried like the CLI's, not trusted like the API's.
"""

import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import settings
from app.core.exceptions import LLMError
from app.core.logging import get_logger
from app.llm import prompts
from app.llm.base import (
    BatchViaLoop,
    Document,
    Effort,
    LLMResult,
    LLMTask,
    LLMUsage,
)

logger = get_logger(__name__)

MAX_ATTEMPTS = 3
NANOSECONDS_PER_MS = 1_000_000


class OllamaUnavailableError(LLMError):
    """The Ollama server did not answer."""

    title = "Ollama unavailable"
    problem_type = "ollama-unavailable"


class OllamaProvider(BatchViaLoop):
    """Runs prompts against a local Ollama server."""

    name = "ollama"

    def __init__(
        self,
        base_url: str | None = None,
        *,
        model: str | None = None,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self._model = model or settings.ollama_model
        self._timeout = timeout or settings.ollama_timeout
        self._client = client
        #: Set by :meth:`probe`. False until something has actually checked, so
        #: a server that is not running never silently becomes the route.
        self._reachable = False

    def is_available(self) -> bool:
        """The last probe's answer.

        Deliberately not a live request: the router asks before every call, and
        a health check per call would cost more than it saves. ``probe`` runs at
        startup and can be re-run.
        """
        return self._reachable

    async def probe(self) -> bool:
        """Ask the server whether it is there, and remember the answer."""
        try:
            async with self._http() as client:
                response = await client.get(f"{self._base_url}/api/tags", timeout=5.0)
                response.raise_for_status()
                models = [m.get("name") for m in (response.json().get("models") or [])]
        except (httpx.HTTPError, ValueError) as exc:
            self._reachable = False
            logger.info(
                "llm.ollama.unreachable", base_url=self._base_url, reason=type(exc).__name__
            )
            return False

        self._reachable = True
        if self._model not in models:
            # Reachable but not loaded is worth saying out loud: the call will
            # fail later with a much less obvious message.
            logger.warning(
                "llm.ollama.model_not_pulled",
                model=self._model,
                available=sorted(filter(None, models)),
            )
        logger.info("llm.ollama.available", base_url=self._base_url, model=self._model)
        return True

    @asynccontextmanager
    async def _http(self) -> AsyncIterator[httpx.AsyncClient]:
        """Borrow a client for one exchange.

        An injected client is borrowed, not owned: closing it would make the
        seam single-use, and httpx refuses to reopen a closed client with a
        RuntimeError that neither caller's except clause catches. A client we
        built ourselves is ours to close.
        """
        if self._client is not None:
            yield self._client
            return
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            yield client

    async def complete_json[ResultT: BaseModel](
        self,
        prompt_name: str,
        response_model: type[ResultT],
        *,
        task: LLMTask,
        variables: dict[str, Any] | None = None,
        documents: Sequence[Document] = (),
        effort: Effort | None = None,
        cached_prefix: str | None = None,
    ) -> LLMResult[ResultT]:
        """Render a prompt, ask the local model, validate the answer.

        ``documents`` are not supported — the models this runs are text-only —
        and are rejected rather than dropped, because silently ignoring the
        resume would produce a confident answer about nothing.
        """
        if documents:
            raise LLMError("the Ollama provider cannot take documents; route this task elsewhere")

        rendered = prompts.render(prompt_name, **(variables or {}))
        schema = _schema_instruction(response_model)
        message = (
            f"{cached_prefix}\n\n{rendered}\n\n{schema}"
            if cached_prefix
            else (f"{rendered}\n\n{schema}")
        )

        usage = LLMUsage(provider=self.name, model=self._model, task=task, accounting="measured")
        for attempt in range(1, MAX_ATTEMPTS + 1):
            body, call_usage = await self._chat(message, task)
            usage = _merge(usage, call_usage)
            try:
                value = response_model.model_validate_json(body)
            except ValidationError as exc:
                if attempt == MAX_ATTEMPTS:
                    raise LLMError(
                        f"{prompt_name} returned a payload that failed validation "
                        f"{MAX_ATTEMPTS} times"
                    ) from exc
                message = (
                    f"{message}\n\n## Your previous answer was rejected\n\n{exc}\n\n"
                    "Return the corrected JSON object and nothing else."
                )
                continue
            return LLMResult(value=value, usage=usage, attempts=attempt)

        raise LLMError(f"{prompt_name} exhausted its attempts")  # pragma: no cover

    async def _chat(self, message: str, task: LLMTask) -> tuple[str, LLMUsage]:
        """One /api/chat round trip."""
        started = time.perf_counter()
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": message}],
            # Syntactically valid JSON, not schema-conformant JSON. Hence the
            # validation loop above.
            "format": "json",
            "stream": False,
        }
        try:
            async with self._http() as client:
                response = await client.post(
                    f"{self._base_url}/api/chat", json=payload, timeout=self._timeout
                )
                response.raise_for_status()
                data: dict[str, Any] = response.json()
        except httpx.HTTPError as exc:
            self._reachable = False
            raise OllamaUnavailableError(
                f"the Ollama server at {self._base_url} did not answer"
            ) from exc

        content = str((data.get("message") or {}).get("content") or "")
        return content, LLMUsage(
            provider=self.name,
            model=str(data.get("model") or self._model),
            task=task,
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            # Local inference costs electricity, not dollars. Zero is the honest
            # figure here, unlike an unpriced API model, which is unknown.
            cost_usd=0.0,
            duration_ms=float(data.get("total_duration") or 0) / NANOSECONDS_PER_MS
            or (time.perf_counter() - started) * 1000,
            accounting="measured",
        )


def _schema_instruction(response_model: type[BaseModel]) -> str:
    """Describe the required shape; nothing here constrains generation."""
    import json

    schema = json.dumps(response_model.model_json_schema(), ensure_ascii=False)
    return (
        "Return one JSON object and nothing else. It must validate against this "
        f"JSON Schema:\n{schema}"
    )


def _merge(left: LLMUsage, right: LLMUsage) -> LLMUsage:
    """Accumulate across retries: a retried call is paid for twice."""
    return LLMUsage(
        provider=right.provider,
        model=right.model,
        task=right.task,
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        # None means "unknown", never "free". Two unknowns stay unknown; a
        # known value plus an unknown one is still not a total worth quoting.
        cost_usd=(
            None
            if left.cost_usd is None and right.cost_usd is None
            else (left.cost_usd or 0.0) + (right.cost_usd or 0.0)
        ),
        duration_ms=left.duration_ms + right.duration_ms,
        accounting="measured",
    )
