"""Picking a provider for a task, and saying so when the pick changes.

Routing is configuration: ``LLM_ROUTING`` maps every task to a provider and
``LLM_FALLBACK_CHAIN`` says where a call goes when that provider cannot serve
it. Both are validated at startup, so an unrouted task or a chain that loops is
a boot failure rather than a surprise on the one path nobody exercised.

**Every fallback is logged at WARNING.** This is the point of the module. A
quiet switch would put two extraction qualities in the same dataset with no way
to tell which rows came from which — and the symptom, months later, is "the
matching got worse" with nothing to point at.

Falling back happens on *availability*, not on failure. A provider that is
present and answers badly has produced a real answer to a real call, and
retrying it elsewhere would hide a prompt or schema problem behind a second
opinion. Only "this provider is not here" moves a call.
"""

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from app.core.config import settings
from app.core.exceptions import LLMError
from app.core.logging import get_logger
from app.llm.base import (
    BatchItem,
    Document,
    Effort,
    LLMProvider,
    LLMResult,
    LLMTask,
)
from app.llm.providers.anthropic_api import AnthropicAPIProvider
from app.llm.providers.claude_cli import ClaudeCLIProvider, CLIUnavailableError
from app.llm.providers.ollama import OllamaProvider, OllamaUnavailableError

logger = get_logger(__name__)

#: Errors that mean "this provider is not here", as opposed to "this provider
#: answered badly". Only these move a call down the chain.
UNAVAILABLE = (CLIUnavailableError, OllamaUnavailableError)


class NoProviderAvailableError(LLMError):
    """Every provider in the chain was unavailable."""

    title = "No LLM provider available"
    problem_type = "no-llm-provider"


def build_providers() -> dict[str, LLMProvider]:
    """Construct one instance of each provider.

    Built once and shared: the CLI provider resolves its binary and holds a
    concurrency semaphore, and both are meaningless per call.
    """
    return {
        "api": AnthropicAPIProvider(),
        "cli": ClaudeCLIProvider(
            settings.claude_cli_binary,
            timeout=settings.claude_cli_timeout,
            max_turns=settings.claude_cli_max_turns,
            concurrency=settings.claude_cli_concurrency,
        ),
        "ollama": OllamaProvider(),
    }


class LLMRouter:
    """Sends each task to its configured provider, or to the next one along."""

    def __init__(self, providers: dict[str, LLMProvider] | None = None) -> None:
        self._providers = providers if providers is not None else build_providers()

    def provider(self, name: str) -> LLMProvider:
        """One provider by name."""
        try:
            return self._providers[name]
        except KeyError as exc:  # pragma: no cover - settings validation prevents this
            raise NoProviderAvailableError(f"no provider named {name!r}") from exc

    def chain_for(self, task: LLMTask) -> list[str]:
        """Provider names to try for a task, preferred first."""
        first = settings.provider_for(task)
        chain = [first]
        current = first
        while nxt := settings.llm_fallback_chain.get(current):
            current = nxt[0]
            if current in chain:  # pragma: no cover - settings validation prevents this
                break
            chain.append(current)
        return chain

    def resolve(self, task: LLMTask) -> tuple[LLMProvider, str | None]:
        """The provider that will serve this task, and who it displaced.

        Returns the chosen provider plus the name of the configured one when
        they differ, so the caller can say what happened.
        """
        chain = self.chain_for(task)
        preferred = chain[0]
        for name in chain:
            provider = self.provider(name)
            if provider.is_available():
                return provider, (None if name == preferred else preferred)
        raise NoProviderAvailableError(
            f"no provider is available for {task.value!r}; tried {chain}"
        )

    def _announce(self, task: LLMTask, provider: LLMProvider, displaced: str | None) -> None:
        """Say which provider ran, loudly when it was not the configured one."""
        if displaced is None:
            return
        logger.warning(
            "llm.router.fell_back",
            task=task.value,
            configured=displaced,
            used=provider.name,
            reason="the configured provider reported itself unavailable",
        )

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
        """Serve one call, moving down the chain only if a provider is absent."""
        chain = self.chain_for(task)
        preferred = chain[0]
        last: Exception | None = None

        for name in chain:
            provider = self.provider(name)
            if not provider.is_available():
                continue
            self._announce(task, provider, None if name == preferred else preferred)
            try:
                return await provider.complete_json(
                    prompt_name,
                    response_model,
                    task=task,
                    variables=variables,
                    documents=documents,
                    effort=effort or settings.effort_for(task),
                    cached_prefix=cached_prefix,
                )
            except UNAVAILABLE as exc:
                # It said it was available and then was not. Keep walking, but
                # never swallow a genuine bad answer this way.
                last = exc
                logger.warning(
                    "llm.router.provider_dropped_out",
                    task=task.value,
                    provider=name,
                    reason=type(exc).__name__,
                )
                continue

        raise NoProviderAvailableError(
            f"no provider could serve {task.value!r}; tried {chain}"
        ) from last

    async def complete_json_batch[ResultT: BaseModel](
        self,
        prompt_name: str,
        response_model: type[ResultT],
        items: Sequence[BatchItem],
        *,
        task: LLMTask,
        effort: Effort | None = None,
        cached_prefix: str | None = None,
    ) -> list[LLMResult[ResultT]]:
        """Serve several items through whichever provider is serving this task.

        Walks the chain exactly as ``complete_json`` does. Checking availability
        and then calling leaves a race — the binary can vanish, the local server
        can stop — and handling that race on the single-call path but not here
        would mean the same failure falls back quietly for one row and raises
        for a hundred.
        """
        chain = self.chain_for(task)
        preferred = chain[0]
        last: Exception | None = None

        for name in chain:
            provider = self.provider(name)
            if not provider.is_available():
                continue
            self._announce(task, provider, None if name == preferred else preferred)
            try:
                return await provider.complete_json_batch(
                    prompt_name,
                    response_model,
                    items,
                    task=task,
                    effort=effort or settings.effort_for(task),
                    cached_prefix=cached_prefix,
                )
            except UNAVAILABLE as exc:
                last = exc
                logger.warning(
                    "llm.router.provider_dropped_out",
                    task=task.value,
                    provider=name,
                    items=len(items),
                    reason=type(exc).__name__,
                )
                continue

        raise NoProviderAvailableError(
            f"no provider could serve a batch of {task.value!r}; tried {chain}"
        ) from last


_router: LLMRouter | None = None


def get_router() -> LLMRouter:
    """The process-wide router.

    A singleton because the providers behind it are: the CLI one resolves a
    binary and owns a semaphore, and rebuilding it per request would undo both.
    """
    global _router
    if _router is None:
        _router = LLMRouter()
    return _router


def reset_router() -> None:
    """Drop the singleton. For tests and for a settings change."""
    global _router
    _router = None
