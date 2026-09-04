"""The local Ollama provider: what it promises, what it refuses, and when it steps aside.

Nothing here reaches the network. Every request is served by ``respx``, bound to
this file's own base URL, so an unmocked call fails the test instead of hanging
against a server that is not running on this machine anyway.

The provider is the cheap route for a task that runs hundreds of times a night,
which makes its failure modes the interesting part. Three of them are load
bearing and each has a test below: availability is a fact somebody checked
rather than an assumption, a text-only model is told about documents instead of
quietly dropping them, and a server that stops answering takes itself out of the
routing rather than failing every call from now on.
"""

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
import structlog
from pydantic import BaseModel

from app.core.config import default_fallback_chain, default_routing, settings
from app.core.exceptions import LLMError
from app.llm import prompts
from app.llm.base import (
    BatchViaLoop,
    Document,
    Effort,
    LLMResult,
    LLMTask,
    LLMUsage,
)
from app.llm.pricing import TokenUsage, cost_usd
from app.llm.providers.ollama import MAX_ATTEMPTS, OllamaProvider, OllamaUnavailableError
from app.llm.router import LLMRouter

pytestmark = pytest.mark.unit

#: Not localhost:11434. A test that passed because something happened to be
#: listening on the real port would be proving nothing about this code.
BASE_URL = "http://ollama.invalid:11434"

#: Deliberately not the configured default, so an assertion on the request body
#: proves the model came from configuration rather than from a coincidence.
MODEL = "test-model:7b-instruct"

PROMPT_NAME = "demo"
PROMPT_TEMPLATE = "Turn this post into a vacancy: {{post}}."
POST = "Ищем Python-разработчика, удалёнка."


class Vacancy(BaseModel):
    """Minimal answer schema: one required field is enough to reject garbage."""

    title: str
    remote: bool = False


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def ollama_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[None]:
    """A throwaway prompt and a configuration pointing at nothing real."""
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / f"{PROMPT_NAME}.md").write_text(PROMPT_TEMPLATE, encoding="utf-8")
    prompts.load.cache_clear()
    monkeypatch.setattr(prompts, "PROMPT_DIR", prompt_dir)
    monkeypatch.setattr(settings, "ollama_base_url", BASE_URL)
    monkeypatch.setattr(settings, "ollama_model", MODEL)
    monkeypatch.setattr(settings, "ollama_timeout", 5.0)
    yield
    prompts.load.cache_clear()


@pytest.fixture
def http(ollama_env: None) -> Iterator[respx.MockRouter]:
    """Every request the provider makes, intercepted before it leaves.

    ``base_url`` is the guard rail: a request to anywhere else is not merely
    unmocked, it raises, so a typo in a URL cannot turn into a live call.
    """
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield router


@pytest.fixture
def provider(ollama_env: None) -> OllamaProvider:
    """A provider built from settings, exactly as ``build_providers`` builds it."""
    return OllamaProvider()


def tags(*models: str) -> httpx.Response:
    """What ``GET /api/tags`` answers on a healthy server."""
    return httpx.Response(200, json={"models": [{"name": name} for name in models]})


def chat(
    content: str,
    *,
    prompt_eval_count: int = 0,
    eval_count: int = 0,
    total_duration: int = 0,
    model: str | None = None,
) -> httpx.Response:
    """What ``POST /api/chat`` answers, with the counters the provider reads."""
    return httpx.Response(
        200,
        json={
            "model": model or MODEL,
            "message": {"role": "assistant", "content": content},
            "prompt_eval_count": prompt_eval_count,
            "eval_count": eval_count,
            "total_duration": total_duration,
            "done": True,
        },
    )


async def parse(provider: OllamaProvider, **overrides: Any) -> LLMResult[Vacancy]:
    """The one entry point, with this file's defaults filled in."""
    kwargs: dict[str, Any] = {"task": LLMTask.TELEGRAM_PARSE, "variables": {"post": POST}}
    kwargs.update(overrides)
    result: LLMResult[Vacancy] = await provider.complete_json(PROMPT_NAME, Vacancy, **kwargs)
    return result


def sent_body(route: respx.Route, index: int = 0) -> dict[str, Any]:
    """The JSON one recorded request actually carried."""
    payload: dict[str, Any] = json.loads(route.calls[index].request.content)
    return payload


# ── availability ──────────────────────────────────────────────────────


def test_a_provider_nobody_checked_is_not_available(provider: OllamaProvider) -> None:
    """Availability starts False and only a probe can change it.

    The router picks the first provider that says it is available. If a freshly
    constructed provider claimed to be up, a machine with no Ollama installed
    would route every Telegram post at a dead port and fail the whole run — the
    default has to be "nobody has checked", not "probably fine".
    """
    assert provider.is_available() is False


async def test_probe_flips_availability_when_the_server_answers(
    provider: OllamaProvider, http: respx.MockRouter
) -> None:
    """A successful /api/tags is what makes the provider routable at all."""
    route = http.get("/api/tags").mock(return_value=tags(MODEL, "llama3:8b"))

    assert await provider.probe() is True

    assert provider.is_available() is True
    assert route.call_count == 1


async def test_probe_reports_a_refused_connection_as_unavailable(
    provider: OllamaProvider, http: respx.MockRouter
) -> None:
    """Nothing listening is the normal case on a laptop, not an exception to raise.

    ``probe`` is called at startup. If a refused connection propagated instead of
    returning False, the absence of an optional local server would take the whole
    service down on boot.
    """
    http.get("/api/tags").mock(side_effect=httpx.ConnectError("connection refused"))

    assert await provider.probe() is False

    assert provider.is_available() is False


async def test_a_later_failed_probe_takes_the_provider_back_out(
    provider: OllamaProvider, http: respx.MockRouter
) -> None:
    """Availability is the last answer, not the best one ever seen.

    Ollama is a desktop app people quit. Without the second assignment, one
    successful probe at boot would keep sending work at a server that has been
    gone for hours.
    """
    route = http.get("/api/tags").mock(
        side_effect=[tags(MODEL), httpx.ConnectError("connection refused")]
    )

    assert await provider.probe() is True
    assert await provider.probe() is False

    assert provider.is_available() is False
    assert route.call_count == 2


async def test_probe_warns_when_the_configured_model_is_not_pulled(
    provider: OllamaProvider, http: respx.MockRouter
) -> None:
    """A reachable server without the model is the confusing failure this warning prevents.

    The provider is available — the server answers — so routing will send it
    work, and every one of those calls fails deep inside /api/chat with a
    message about a model name. Saying it once at startup, with the list of what
    the server does have, turns that into a one-line fix (``ollama pull ...``).
    """
    http.get("/api/tags").mock(return_value=tags("llama3:8b", "mistral:7b"))

    with structlog.testing.capture_logs() as captured:
        assert await provider.probe() is True

    warnings = [entry for entry in captured if entry["log_level"] == "warning"]
    assert [entry["event"] for entry in warnings] == ["llm.ollama.model_not_pulled"]
    assert warnings[0]["model"] == MODEL
    assert warnings[0]["available"] == ["llama3:8b", "mistral:7b"]
    # Reachable but unloaded still counts as reachable: the warning is advice,
    # not a veto, and the model may be pulled while the process runs.
    assert provider.is_available() is True


# ── a successful call ─────────────────────────────────────────────────


async def test_a_good_answer_comes_back_validated(
    provider: OllamaProvider, http: respx.MockRouter
) -> None:
    """The happy path returns a typed object, not the raw string the server sent."""
    route = http.post("/api/chat").mock(
        return_value=chat('{"title": "Python Developer", "remote": true}')
    )

    result = await parse(provider)

    assert result.value == Vacancy(title="Python Developer", remote=True)
    assert result.attempts == 1
    assert route.call_count == 1


async def test_the_request_asks_for_json_from_the_configured_model_in_one_piece(
    provider: OllamaProvider, http: respx.MockRouter
) -> None:
    """Three fields in the request body are the whole contract with the server.

    ``format="json"`` is what stops the model wrapping its answer in prose that
    no parser will accept; ``stream=False`` is what makes a single response body
    exist to parse at all — streaming would return newline-delimited chunks and
    ``response.json()`` would raise on the first one; and the model name is what
    proves configuration reached the wire rather than the server's own default.
    """
    route = http.post("/api/chat").mock(return_value=chat('{"title": "Python Developer"}'))

    await parse(provider)

    body = sent_body(route)
    assert body["format"] == "json"
    assert body["stream"] is False
    assert body["model"] == MODEL
    # The rendered prompt and the schema instruction travel as one user turn.
    content = body["messages"][0]["content"]
    assert POST in content
    assert "JSON Schema" in content
    assert "title" in json.loads(content.split("JSON Schema:")[1])["properties"]


# ── usage accounting ──────────────────────────────────────────────────


async def test_usage_maps_the_servers_counters_and_prices_local_inference_at_zero(
    provider: OllamaProvider, http: respx.MockRouter
) -> None:
    """Zero dollars and unknown dollars are different facts and must stay different.

    Ollama reports real token counts, so the accounting is "measured", and the
    cost of running a model on hardware already paid for is genuinely 0.0. That
    is not the same as an API model missing from the price table, whose cost is
    None — unknown, never free. If this provider reported None, a dashboard would
    show local inference as unpriced risk; if the API reported 0.0, a real
    invoice would arrive for a total that said nothing was spent.

    The zero is asserted on the round trip that produces it rather than on the
    merged total, because ``_merge`` folds retries with ``cost_usd or 0.0``: a
    provider that reported None would come out of ``complete_json`` as 0.0
    anyway, so the public figure cannot tell "free" from "unknown" apart and an
    assertion there would hold whichever the provider said.
    """
    http.post("/api/chat").mock(
        return_value=chat(
            '{"title": "Python Developer"}',
            prompt_eval_count=1234,
            eval_count=567,
            total_duration=2_000_000_000,
        )
    )

    usage = (await parse(provider)).usage

    assert usage.provider == "ollama"
    assert usage.model == MODEL
    assert usage.task is LLMTask.TELEGRAM_PARSE
    assert usage.input_tokens == 1234
    assert usage.output_tokens == 567
    assert usage.total_tokens == 1234 + 567
    assert usage.accounting == "measured"
    assert usage.duration_ms == 2000.0
    assert usage.cost_usd == 0.0

    # LLMUsage defaults cost_usd to None, so 0.0 has to be stated by the
    # provider on every call. This is the assertion that notices if it stops.
    _, one_call = await provider._chat("anything", LLMTask.TELEGRAM_PARSE)
    assert one_call.cost_usd == 0.0
    assert one_call.cost_usd is not None
    # The contrast, stated with the production pricing function rather than a
    # comment: an API model with no price sheet answers None to the same question.
    assert cost_usd("model-with-no-price-sheet", TokenUsage(1234, 567)) is None


# ── bad answers ───────────────────────────────────────────────────────


async def test_an_unparseable_answer_is_retried_and_then_given_up_on(
    provider: OllamaProvider, http: respx.MockRouter
) -> None:
    """``format="json"`` buys syntax, not a schema, so validation has to be able to fail.

    The server will happily return a syntactically perfect object with none of
    the required fields. Two retries carry the validation error back to the
    model; a third failure is an error rather than a fourth attempt, because a
    7B model that has missed the schema three times will not find it on the
    tenth and the run has a few hundred more posts to get through.
    """
    route = http.post("/api/chat").mock(
        side_effect=[chat('{"nope": 1}'), chat("not json at all"), chat("[]")]
    )

    with pytest.raises(LLMError) as excinfo:
        await parse(provider)

    assert route.call_count == MAX_ATTEMPTS == 3
    assert "failed validation" in str(excinfo.value)
    # A bad answer is not an absent server: turning this into an availability
    # error would move the task to the API and hide a broken prompt.
    assert not isinstance(excinfo.value, OllamaUnavailableError)
    # The retry has to tell the model what was wrong, or it is just a re-roll.
    assert "rejected" in sent_body(route, 1)["messages"][0]["content"]


async def test_a_corrected_answer_is_accepted_and_both_calls_are_paid_for(
    provider: OllamaProvider, http: respx.MockRouter
) -> None:
    """A retry that works still costs two calls' worth of tokens.

    Reporting only the successful attempt would understate throughput on exactly
    the runs where it matters — a model that needs two goes at every post takes
    twice as long as the estimate that decided local inference was fast enough.
    """
    http.post("/api/chat").mock(
        side_effect=[
            chat('{"nope": 1}', prompt_eval_count=100, eval_count=10),
            chat('{"title": "Python Developer"}', prompt_eval_count=140, eval_count=20),
        ]
    )

    result = await parse(provider)

    assert result.value.title == "Python Developer"
    assert result.attempts == 2
    assert result.usage.input_tokens == 240
    assert result.usage.output_tokens == 30


# ── the server going away mid-call ────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "outcome"),
    [
        ("refused", httpx.ConnectError("connection refused")),
        ("timeout", httpx.ReadTimeout("too slow")),
        ("server error", httpx.Response(500, json={"error": "model runner crashed"})),
    ],
)
async def test_a_failed_call_raises_unavailable_and_stops_the_route(
    provider: OllamaProvider,
    http: respx.MockRouter,
    name: str,
    outcome: httpx.Response | Exception,
) -> None:
    """The provider that just failed must not be picked for the next post.

    ``OllamaUnavailableError`` is the one error class the router treats as "not
    here", so raising it is what moves this call to the API — and clearing the
    flag is what keeps the remaining few hundred posts from each paying a
    timeout to rediscover the same dead server.
    """
    http.get("/api/tags").mock(return_value=tags(MODEL))
    assert await provider.probe() is True
    if isinstance(outcome, httpx.Response):
        http.post("/api/chat").mock(return_value=outcome)
    else:
        http.post("/api/chat").mock(side_effect=outcome)

    with pytest.raises(OllamaUnavailableError):
        await parse(provider)

    assert provider.is_available() is False


async def test_documents_are_refused_rather_than_dropped(
    provider: OllamaProvider, http: respx.MockRouter
) -> None:
    """A text-only model handed a PDF must fail loudly, before any request goes out.

    Silently ignoring the attachment is the dangerous version: the prompt still
    says "the resume is below", so the model answers confidently about a resume
    it never saw, and the result is a plausible profile of nobody that flows
    into matching like any other.
    """
    route = http.post("/api/chat").mock(return_value=chat('{"title": "never asked"}'))

    with pytest.raises(LLMError) as excinfo:
        await parse(provider, documents=(Document(content=b"%PDF-1.7 resume"),))

    assert "documents" in str(excinfo.value)
    assert route.call_count == 0


# ── through the router ────────────────────────────────────────────────


class FakeAPIProvider(BatchViaLoop):
    """The terminus of the fallback chain, always up, always answering."""

    name = "api"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def is_available(self) -> bool:
        """The API is available whenever a key is configured; here, always."""
        return True

    async def complete_json[ResultT: BaseModel](
        self,
        prompt_name: str,
        response_model: type[ResultT],
        *,
        task: LLMTask,
        variables: dict[str, Any] | None = None,
        documents: tuple[Document, ...] = (),
        effort: Effort | None = None,
        cached_prefix: str | None = None,
    ) -> LLMResult[ResultT]:
        """Record the call and answer with a fixed, valid object."""
        self.calls.append({"prompt_name": prompt_name, "task": task, "variables": variables})
        value = response_model.model_validate({"title": "from the API"})
        return LLMResult(
            value=value,
            usage=LLMUsage(provider=self.name, model="api-model", task=task, cost_usd=0.004),
        )


@pytest.fixture
def routed(monkeypatch: pytest.MonkeyPatch, ollama_env: None) -> tuple[LLMRouter, FakeAPIProvider]:
    """A router wired exactly like production: telegram_parse to Ollama, then the API."""
    monkeypatch.setattr(settings, "llm_routing", default_routing())
    monkeypatch.setattr(settings, "llm_fallback_chain", default_fallback_chain())
    api = FakeAPIProvider()
    router = LLMRouter({"api": api, "cli": api, "ollama": OllamaProvider()})
    return router, api


async def test_an_unavailable_ollama_sends_the_task_to_the_api_and_says_so(
    routed: tuple[LLMRouter, FakeAPIProvider], http: respx.MockRouter
) -> None:
    """The fallback must work and must be audible.

    Two extraction qualities in one vacancies table with nothing recording which
    rows came from which is the failure this WARNING exists to prevent: months
    later the symptom is "matching got worse" and there is nothing to point at.
    The Ollama provider here has never been probed, which is exactly the state a
    machine without Ollama installed is in.
    """
    router, api = routed

    with structlog.testing.capture_logs() as captured:
        result = await router.complete_json(
            PROMPT_NAME, Vacancy, task=LLMTask.TELEGRAM_PARSE, variables={"post": POST}
        )

    assert result.value.title == "from the API"
    assert len(api.calls) == 1
    fallbacks = [entry for entry in captured if entry["event"] == "llm.router.fell_back"]
    assert len(fallbacks) == 1
    assert fallbacks[0]["log_level"] == "warning"
    assert fallbacks[0]["configured"] == "ollama"
    assert fallbacks[0]["used"] == "api"
    # Nothing was even attempted against the local server.
    assert not http.calls


async def test_a_probed_ollama_keeps_the_task_and_logs_no_fallback(
    routed: tuple[LLMRouter, FakeAPIProvider], http: respx.MockRouter
) -> None:
    """The other half of the contract: a warning on every run would train people to ignore it.

    Without this test, a provider that always reported itself unavailable would
    still pass the fallback test above while quietly sending every Telegram post
    to the API — the exact bill the local route exists to avoid.
    """
    router, api = routed
    http.get("/api/tags").mock(return_value=tags(MODEL))
    http.post("/api/chat").mock(return_value=chat('{"title": "from Ollama"}'))
    assert await router.provider("ollama").probe() is True  # type: ignore[attr-defined]

    with structlog.testing.capture_logs() as captured:
        result = await router.complete_json(
            PROMPT_NAME, Vacancy, task=LLMTask.TELEGRAM_PARSE, variables={"post": POST}
        )

    assert result.value.title == "from Ollama"
    assert api.calls == []
    assert [entry for entry in captured if entry["event"] == "llm.router.fell_back"] == []
