"""Routing: who serves a call, and whether anyone is told when that changes.

The router has exactly two jobs and both are silent when they go wrong.

**It picks a provider.** Tasks are routed by configuration because they are not
alike — resume extraction wants quality, Telegram parsing wants to be free — and
a task quietly served by the wrong provider is a change in output quality with
no change in the code.

**It says so when the pick changes.** Every fallback is a WARNING, and that
warning is the whole point of the module: a silent switch puts two extraction
qualities in one dataset with no way to tell which rows came from which, and
the symptom months later is "the matching got worse" with nothing to point at.
Half the tests here are about that log line rather than the return value.

Nothing real is touched. Providers are hand-built stand-ins passed straight to
``LLMRouter(providers=...)``, so no API key is read, no ``claude`` process is
spawned (one real CLI call is ~$0.20 and ~40 seconds) and nothing is sent to
Ollama.

``test_the_capture_sees_a_warning_this_module_logs`` must never be deleted: it
logs through the router's own logger and asserts the capture finds it. Without
it a broken capture would make every "a warning was emitted" test below pass
while the service falls back in total silence.
"""

import io
import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
import structlog
from pydantic import BaseModel

from app.core import logging as logging_module
from app.core.config import settings
from app.core.exceptions import LLMError
from app.core.logging import configure_logging
from app.llm import router as router_module
from app.llm.base import BatchItem, Document, Effort, LLMTask, LLMUsage
from app.llm.providers.claude_cli import CLIUnavailableError
from app.llm.providers.ollama import OllamaUnavailableError
from app.llm.router import (
    LLMRouter,
    NoProviderAvailableError,
    get_router,
    reset_router,
)

pytestmark = pytest.mark.unit

PROMPT_NAME = "demo"

#: The shipped chain: both subscription providers end at the API, which is the
#: only terminus, and the API itself falls nowhere.
FALLBACK_CHAIN: dict[str, list[str]] = {"cli": ["api"], "ollama": ["api"], "api": []}


class Answer(BaseModel):
    """Minimal response schema. ``text`` carries which provider answered."""

    text: str


# ── stand-in providers ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Call:
    """One call a fake provider received, kept so a test can inspect it."""

    prompt_name: str
    task: LLMTask
    effort: Effort | None
    variables: dict[str, Any] | None
    items: int | None = None


@dataclass(slots=True)
class FakeProvider:
    """A provider that answers instantly, or refuses to be here.

    ``available`` is what it claims before a call; ``raises`` is what it does
    during one. The two are separate on purpose — the interesting case is a
    provider that says yes and then is not there.
    """

    name: str
    available: bool = True
    raises: Exception | None = None
    calls: list[Call] = field(default_factory=list)

    def is_available(self) -> bool:
        """Whether this provider claims it can serve a call."""
        return self.available

    async def complete_json(
        self,
        prompt_name: str,
        response_model: type[Answer],
        *,
        task: LLMTask,
        variables: dict[str, Any] | None = None,
        documents: Sequence[Document] = (),
        effort: Effort | None = None,
        cached_prefix: str | None = None,
    ) -> router_module.LLMResult[Answer]:
        """Record the call, then answer with this provider's own name."""
        self.calls.append(Call(prompt_name, task, effort, variables))
        if self.raises is not None:
            raise self.raises
        return router_module.LLMResult(
            value=response_model(text=self.name),
            usage=LLMUsage(provider=self.name, model=f"fake-{self.name}", task=task),
        )

    async def complete_json_batch(
        self,
        prompt_name: str,
        response_model: type[Answer],
        items: Sequence[BatchItem],
        *,
        task: LLMTask,
        effort: Effort | None = None,
        cached_prefix: str | None = None,
    ) -> list[router_module.LLMResult[Answer]]:
        """Record one batch call and answer every item with this provider's name."""
        self.calls.append(Call(prompt_name, task, effort, None, items=len(items)))
        if self.raises is not None:
            raise self.raises
        return [
            router_module.LLMResult(
                value=response_model(text=self.name),
                usage=LLMUsage(provider=self.name, model=f"fake-{self.name}", task=task),
            )
            for _ in items
        ]


def build_router(
    *,
    api: FakeProvider | None = None,
    cli: FakeProvider | None = None,
    ollama: FakeProvider | None = None,
) -> tuple[LLMRouter, dict[str, FakeProvider]]:
    """A router over three stand-ins, plus the stand-ins to assert against."""
    providers = {
        "api": api or FakeProvider("api"),
        "cli": cli or FakeProvider("cli"),
        "ollama": ollama or FakeProvider("ollama"),
    }
    return LLMRouter(providers=dict(providers)), providers


def route(
    monkeypatch: pytest.MonkeyPatch,
    task: LLMTask,
    provider: str,
    *,
    chain: dict[str, list[str]] | None = None,
    effort: Effort = "low",
) -> None:
    """Point one task at one provider, leaving the rest of the table valid.

    Routing is read off ``settings`` on every call, so the table is patched
    rather than the router: this is the same lookup production performs.
    """
    routing = {member.value: "api" for member in LLMTask} | {task.value: provider}
    monkeypatch.setattr(settings, "llm_routing", routing)
    monkeypatch.setattr(settings, "llm_fallback_chain", dict(chain or FALLBACK_CHAIN))
    monkeypatch.setattr(settings, "llm_task_effort", {member.value: effort for member in LLMTask})


# ── capturing what the router logs ────────────────────────────────────


class LogSink:
    """Everything logged during one test, in both the shapes it is stored in.

    The buffer holds the real production JSON stream — the handler
    ``configure_logging`` installs, pointed somewhere readable instead of
    stderr — so a structlog call's individual keys survive and can be asserted
    on by name. ``caplog`` is the second channel, proving the record also
    reaches the stdlib tree the rest of the service logs through.
    """

    def __init__(self, buffer: io.StringIO, caplog: pytest.LogCaptureFixture) -> None:
        self._buffer = buffer
        self._caplog = caplog

    @property
    def text(self) -> str:
        """Everything logged, from both channels, as one searchable string."""
        return f"{self._buffer.getvalue()}\n{self._caplog.text}"

    def records(self) -> list[dict[str, Any]]:
        """The JSON log stream parsed back into structured records."""
        parsed: list[dict[str, Any]] = []
        for line in self._buffer.getvalue().splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:  # a line no logger of ours wrote
                continue
            if isinstance(record, dict):
                parsed.append(record)
        return parsed

    def warnings(self, event: str) -> list[dict[str, Any]]:
        """Every WARNING record for one event name."""
        return [
            record
            for record in self.records()
            if record.get("event") == event and record.get("level") == "warning"
        ]

    def events(self) -> set[str]:
        """The event names logged, to prove a test exercised what it claims."""
        return {str(record["event"]) for record in self.records() if "event" in record}


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Iterator[LogSink]:
    """Real production logging, captured.

    Production rendering because JSON is what a log aggregator actually stores
    and because it parses back into fields; the alternative, asserting on a
    console-rendered line, would pass on a record whose fields were dropped.
    """
    monkeypatch.setattr(logging_module.settings, "environment", "production")
    monkeypatch.setattr(logging_module.settings, "log_level", "INFO")

    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level

    configure_logging()
    buffer = io.StringIO()
    for handler in root.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(buffer)
    # configure_logging clears the root handlers, caplog's included.
    root.addHandler(caplog.handler)
    caplog.set_level(logging.INFO)
    try:
        yield LogSink(buffer, caplog)
    finally:
        root.removeHandler(caplog.handler)
        root.handlers[:] = original_handlers
        root.setLevel(original_level)
        structlog.reset_defaults()


def test_the_capture_sees_a_warning_this_module_logs(logs: LogSink) -> None:
    """The control test. Every fallback assertion below is worthless if the
    capture silently sees nothing, and a capture that sees nothing is exactly
    what a silent fallback looks like.

    Logged through the router's own logger, at the level and with the keys the
    router really uses, so this proves the channel the other tests read."""
    router_module.logger.warning(
        "llm.router.fell_back",
        task="rerank",
        configured="cli",
        used="api",
        reason="control",
    )

    captured = logs.warnings("llm.router.fell_back")
    assert len(captured) == 1
    assert captured[0]["configured"] == "cli"
    assert captured[0]["used"] == "api"
    assert "llm.router.fell_back" in logs.text


# ── routing ───────────────────────────────────────────────────────────


async def test_a_task_goes_to_the_provider_it_is_routed_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routing is configuration, and configuration nobody consults is a comment.
    If the router ignored the table, every task would land on whichever provider
    happened to be first and the per-task cost/quality split would be fiction."""
    route(monkeypatch, LLMTask.TELEGRAM_PARSE, "ollama")
    router, providers = build_router()

    result = await router.complete_json(
        PROMPT_NAME, Answer, task=LLMTask.TELEGRAM_PARSE, variables={"x": 1}
    )

    assert result.value.text == "ollama"
    assert result.usage.provider == "ollama"
    assert len(providers["ollama"].calls) == 1
    assert providers["api"].calls == []
    assert providers["cli"].calls == []


async def test_the_call_reaches_the_provider_with_prompt_task_and_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The router is a dispatcher, not a filter: everything the caller passed has
    to arrive intact, or a prompt renders with missing variables somewhere no
    test looks."""
    route(monkeypatch, LLMTask.VACANCY_PARSE, "api")
    router, providers = build_router()

    await router.complete_json(
        PROMPT_NAME, Answer, task=LLMTask.VACANCY_PARSE, variables={"title": "Backend"}
    )

    call = providers["api"].calls[0]
    assert call.prompt_name == PROMPT_NAME
    assert call.task is LLMTask.VACANCY_PARSE
    assert call.variables == {"title": "Backend"}


def test_chain_for_follows_the_configured_fallbacks_and_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chain is walked link by link, and the walk has to stop. A chain that
    ran off the end or looped would hang the call, not fail it."""
    route(monkeypatch, LLMTask.TELEGRAM_PARSE, "ollama")
    router, _ = build_router()

    assert router.chain_for(LLMTask.TELEGRAM_PARSE) == ["ollama", "api"]
    assert router.chain_for(LLMTask.VACANCY_PARSE) == ["api"]


def test_a_looping_chain_terminates_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settings validation rejects a cycle at startup, so this can only happen
    if that validation is ever weakened — and the failure mode is a hung worker,
    which is far worse than a bad answer. The walk must break, not spin."""
    route(monkeypatch, LLMTask.RERANK, "cli", chain={"cli": ["api"], "api": ["cli"]})
    router, _ = build_router()

    assert router.chain_for(LLMTask.RERANK) == ["cli", "api"]


# ── falling back, and saying so ───────────────────────────────────────


async def test_an_unavailable_provider_hands_the_call_to_the_next_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A laptop without Claude Code installed must still parse a resume. The
    answer has to come back from the provider that actually ran, not an empty
    result the caller then treats as a parse failure."""
    route(monkeypatch, LLMTask.RESUME_EXTRACTION, "cli")
    router, providers = build_router(cli=FakeProvider("cli", available=False))

    result = await router.complete_json(PROMPT_NAME, Answer, task=LLMTask.RESUME_EXTRACTION)

    assert result.value.text == "api"
    assert result.usage.provider == "api"
    assert providers["cli"].calls == []
    assert len(providers["api"].calls) == 1


async def test_falling_back_is_logged_at_warning_naming_task_configured_and_used(
    monkeypatch: pytest.MonkeyPatch, logs: LogSink
) -> None:
    """THE test of this module. A quiet switch mixes two extraction qualities in
    one dataset with no way to tell which rows came from which; months later the
    symptom is "matching got worse" with nothing to point at.

    All three facts must be in the record — which task moved, where it was
    configured to go, where it actually went — because any one alone leaves the
    reader unable to reconstruct what happened."""
    route(monkeypatch, LLMTask.RESUME_EXTRACTION, "cli")
    router, _ = build_router(cli=FakeProvider("cli", available=False))

    await router.complete_json(PROMPT_NAME, Answer, task=LLMTask.RESUME_EXTRACTION)

    captured = logs.warnings("llm.router.fell_back")
    assert len(captured) == 1
    assert captured[0]["task"] == LLMTask.RESUME_EXTRACTION.value
    assert captured[0]["configured"] == "cli"
    assert captured[0]["used"] == "api"


async def test_a_normal_call_logs_no_fallback_warning(
    monkeypatch: pytest.MonkeyPatch, logs: LogSink
) -> None:
    """The warning has to mean something. If every call logged one, the line
    would be filtered out of the aggregator within a week and the real fallback
    would arrive invisible."""
    route(monkeypatch, LLMTask.RERANK, "api")
    router, _ = build_router()

    await router.complete_json(PROMPT_NAME, Answer, task=LLMTask.RERANK)

    assert logs.warnings("llm.router.fell_back") == []
    assert "llm.router.fell_back" not in logs.events()


@pytest.mark.parametrize(
    ("provider_name", "error"),
    [
        ("cli", CLIUnavailableError("the 'claude' binary is not on PATH")),
        ("ollama", OllamaUnavailableError("nothing is listening on 11434")),
    ],
)
async def test_a_provider_that_drops_out_mid_call_still_falls_through(
    monkeypatch: pytest.MonkeyPatch,
    logs: LogSink,
    provider_name: str,
    error: LLMError,
) -> None:
    """``is_available`` is a cheap cached guess, not a guarantee: the CLI can be
    uninstalled and Ollama stopped between the check and the call. That race
    must end in an answer from the next provider, and it must still be logged —
    the row is served by a different model either way."""
    route(monkeypatch, LLMTask.RESUME_EXTRACTION, provider_name)
    dropout = FakeProvider(provider_name, available=True, raises=error)
    router, providers = build_router(**{provider_name: dropout})

    result = await router.complete_json(PROMPT_NAME, Answer, task=LLMTask.RESUME_EXTRACTION)

    assert result.value.text == "api"
    assert len(dropout.calls) == 1
    assert len(providers["api"].calls) == 1

    dropped = logs.warnings("llm.router.provider_dropped_out")
    assert len(dropped) == 1
    assert dropped[0]["provider"] == provider_name
    assert dropped[0]["reason"] == type(error).__name__
    assert dropped[0]["task"] == LLMTask.RESUME_EXTRACTION.value


async def test_an_ordinary_llm_error_is_not_retried_on_another_provider(
    monkeypatch: pytest.MonkeyPatch, logs: LogSink
) -> None:
    """A provider that answers badly has produced a real answer to a real call.
    Asking a second one would hide a broken prompt or a changed schema behind a
    second opinion — the bug would surface as a cost increase, not an error.

    Only "this provider is not here" moves a call; everything else propagates."""
    route(monkeypatch, LLMTask.RESUME_EXTRACTION, "cli")
    boom = LLMError("the model returned something the schema rejects twice")
    router, providers = build_router(cli=FakeProvider("cli", raises=boom))

    with pytest.raises(LLMError) as raised:
        await router.complete_json(PROMPT_NAME, Answer, task=LLMTask.RESUME_EXTRACTION)

    assert raised.value is boom
    assert not isinstance(raised.value, NoProviderAvailableError)
    assert providers["api"].calls == []
    assert logs.warnings("llm.router.provider_dropped_out") == []


async def test_every_provider_unavailable_raises_naming_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When nothing can serve a call the caller gets one clear error naming the
    task and everywhere that was tried — enough to fix the deployment from the
    log line alone, instead of "LLM call failed"."""
    route(monkeypatch, LLMTask.RESUME_EXTRACTION, "cli")
    router, _ = build_router(
        cli=FakeProvider("cli", available=False),
        api=FakeProvider("api", available=False),
    )

    with pytest.raises(NoProviderAvailableError) as raised:
        await router.complete_json(PROMPT_NAME, Answer, task=LLMTask.RESUME_EXTRACTION)

    message = str(raised.value)
    assert LLMTask.RESUME_EXTRACTION.value in message
    assert "cli" in message
    assert "api" in message


async def test_the_dropout_that_exhausts_the_chain_keeps_its_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last provider vanishing mid-call must not erase why. Without the
    chained cause the operator sees "no provider could serve" and no hint that
    Ollama was reachable a second earlier."""
    route(monkeypatch, LLMTask.TELEGRAM_PARSE, "ollama")
    gone = OllamaUnavailableError("connection refused")
    router, _ = build_router(
        ollama=FakeProvider("ollama", raises=gone),
        api=FakeProvider("api", available=False),
    )

    with pytest.raises(NoProviderAvailableError) as raised:
        await router.complete_json(PROMPT_NAME, Answer, task=LLMTask.TELEGRAM_PARSE)

    assert raised.value.__cause__ is gone


# ── effort ────────────────────────────────────────────────────────────


async def test_effort_defaults_to_the_setting_for_the_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One global effort would either overspend on re-rank, which runs 120 times
    a day, or underthink resume extraction, which runs once. A caller that names
    no effort must still get the per-task setting, not the provider's default."""
    route(monkeypatch, LLMTask.RESUME_EXTRACTION, "cli", effort="high")
    router, providers = build_router()

    await router.complete_json(PROMPT_NAME, Answer, task=LLMTask.RESUME_EXTRACTION)

    assert providers["cli"].calls[0].effort == "high"


async def test_a_caller_supplied_effort_wins_over_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Effort is named at the call site when a call needs it; a default that
    quietly overrode the argument would make the parameter a lie."""
    route(monkeypatch, LLMTask.RERANK, "api", effort="low")
    router, providers = build_router()

    await router.complete_json(PROMPT_NAME, Answer, task=LLMTask.RERANK, effort="max")

    assert providers["api"].calls[0].effort == "max"


# ── batches take the same road ────────────────────────────────────────


async def test_a_batch_goes_to_the_routed_provider_with_the_task_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batching exists to amortise per-call overhead, so a batch must reach the
    provider as one call — and be routed and given effort by the same rules as a
    single one, or the cheap path quietly becomes the expensive one."""
    route(monkeypatch, LLMTask.TELEGRAM_PARSE, "ollama", effort="low")
    router, providers = build_router()
    items = [BatchItem(variables={"i": index}) for index in range(3)]

    results = await router.complete_json_batch(
        PROMPT_NAME, Answer, items, task=LLMTask.TELEGRAM_PARSE
    )

    assert [result.value.text for result in results] == ["ollama"] * 3
    assert len(providers["ollama"].calls) == 1
    assert providers["ollama"].calls[0].items == 3
    assert providers["ollama"].calls[0].effort == "low"


async def test_a_batch_falling_back_is_logged_too(
    monkeypatch: pytest.MonkeyPatch, logs: LogSink
) -> None:
    """A batch is where a silent switch does the most damage: hundreds of rows
    parsed by a different model in one call, all of them looking the same
    afterwards."""
    route(monkeypatch, LLMTask.TELEGRAM_PARSE, "ollama")
    router, _ = build_router(ollama=FakeProvider("ollama", available=False))

    results = await router.complete_json_batch(
        PROMPT_NAME, Answer, [BatchItem(variables={})], task=LLMTask.TELEGRAM_PARSE
    )

    assert [result.value.text for result in results] == ["api"]
    captured = logs.warnings("llm.router.fell_back")
    assert len(captured) == 1
    assert captured[0]["configured"] == "ollama"
    assert captured[0]["used"] == "api"


async def test_a_batch_with_no_provider_at_all_raises_naming_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``resolve`` is the batch path's own copy of the availability walk, so it
    needs its own proof that an empty chain fails loudly rather than returning
    an empty list of results."""
    route(monkeypatch, LLMTask.TELEGRAM_PARSE, "ollama")
    router, _ = build_router(
        ollama=FakeProvider("ollama", available=False),
        api=FakeProvider("api", available=False),
    )

    with pytest.raises(NoProviderAvailableError) as raised:
        await router.complete_json_batch(
            PROMPT_NAME, Answer, [BatchItem(variables={})], task=LLMTask.TELEGRAM_PARSE
        )

    assert LLMTask.TELEGRAM_PARSE.value in str(raised.value)
    assert "ollama" in str(raised.value)


# ── the process-wide instance ─────────────────────────────────────────


@pytest.fixture
def fake_singleton(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make ``get_router()`` build stand-ins, and leave no singleton behind.

    Without the patch the singleton would construct the real providers — which
    resolve the ``claude`` binary and read the API key — and would then be
    shared with every test that runs afterwards.
    """
    monkeypatch.setattr(router_module, "build_providers", lambda: {"api": FakeProvider("api")})
    reset_router()
    yield
    reset_router()


def test_get_router_returns_the_same_instance_every_time(fake_singleton: None) -> None:
    """The providers behind the router are stateful — the CLI one resolves a
    binary and owns a concurrency semaphore — so rebuilding per request would
    undo the concurrency limit and re-probe the filesystem on every call."""
    assert get_router() is get_router()


def test_reset_router_drops_the_instance(fake_singleton: None) -> None:
    """A settings change has to be able to take effect, and a test must not
    inherit the router another test built."""
    first = get_router()

    reset_router()

    assert get_router() is not first


def test_the_singleton_is_built_from_build_providers(fake_singleton: None) -> None:
    """Proves the fixture's patch is what ``get_router`` actually uses: if the
    real ``build_providers`` were reached, this suite would spawn a CLI process
    and read an API key from the environment.

    The assertion is ``isinstance`` and not ``.name == "api"`` on purpose — the
    real ``AnthropicAPIProvider`` is also named ``"api"``, so a name check would
    hold whether the patch took effect or not and would prove nothing."""
    assert isinstance(get_router().provider("api"), FakeProvider)
