"""The overview's operation buttons, and the hand-off to the local agent.

What is defended, one property per decision in ``app.services.operations``:

* a button returns while its operation is still running, and the operation can
  be polled to its end without reloading anything;
* a second copy of the same kind is refused while one runs, and a different kind
  is not;
* an operation that stops running never stays ``running`` — on a domain error,
  on a crash, with a sentence for a person rather than a traceback;
* the two operations that need the owner's browser are only *recorded*: they
  wait for the watcher, say so, are claimed only through the token-guarded seam,
  and end only when the watcher reports;
* the start endpoint takes JSON, so a page on another origin cannot press it
  with a simple form post.

No database: every runner is replaced by one the test controls.
"""

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core.config import settings
from app.core.exceptions import AppError
from app.schemas.operations import OperationKind, OperationStatus
from app.services import operations as operations_service
from app.services import pipeline as pipeline_service
from app.services.operations import OperationRegistry, Progress

pytestmark = pytest.mark.unit

OPERATIONS_URL = f"{settings.api_v1_prefix}/operations"
CLAIM_URL = f"{settings.api_v1_prefix}/applications/operations/claim"
TOKEN = "local-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PATIENCE = 5.0


class Gate:
    """A runner that parks until the test lets it finish."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.error: Exception | None = None
        self.calls = 0

    async def __call__(self, progress: Progress) -> list[str]:
        self.calls += 1
        progress.done, progress.total, progress.note = 1, 3, "посчитано 1"
        self.entered.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return ["Готово: 3 из 3."]


@pytest_asyncio.fixture
async def gates(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[OperationKind, Gate]]:
    """A fresh registry whose backend runners are gates, and a known token."""
    runners = {kind: Gate() for kind in (OperationKind.EMBED, OperationKind.MATCH)}
    registry = OperationRegistry(runners)
    monkeypatch.setattr(operations_service, "_registry", registry)
    monkeypatch.setattr(pipeline_service, "_registry", pipeline_service.JobRegistry())
    monkeypatch.setattr(settings, "agent_api_token", SecretStr(TOKEN))
    yield runners
    for gate in runners.values():
        gate.release.set()
    await asyncio.sleep(0)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """In-process client; none of these endpoints reads the database."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


async def _finish(client: AsyncClient, kind: OperationKind) -> dict[str, object]:
    """Poll the panel until the newest operation of this kind has ended."""
    async with asyncio.timeout(PATIENCE):
        while True:
            state = (await client.get(OPERATIONS_URL)).json()
            found = next(op for op in state["operations"] if op["kind"] == kind.value)
            if found["status"] not in {"queued", "running", "waiting_agent"}:
                return dict(found)
            await asyncio.sleep(0.01)


async def test_a_button_returns_while_the_operation_runs_and_can_be_polled_to_the_end(
    client: AsyncClient, gates: dict[OperationKind, Gate]
) -> None:
    response = await client.post(OPERATIONS_URL, json={"kind": "embed"})
    assert response.status_code == 202
    await asyncio.wait_for(gates[OperationKind.EMBED].entered.wait(), PATIENCE)

    state = (await client.get(OPERATIONS_URL)).json()
    assert state["busy"] == ["embed"]
    [running] = state["operations"]
    assert running["status"] == "running"
    assert (running["done"], running["total"]) == (1, 3)
    assert "посчитано 1" in running["message"]

    gates[OperationKind.EMBED].release.set()
    finished = await _finish(client, OperationKind.EMBED)
    assert finished["status"] == "success"
    assert finished["report"] == ["Готово: 3 из 3."]
    assert (await client.get(OPERATIONS_URL)).json()["busy"] == []


async def test_a_second_copy_is_refused_and_another_kind_is_not(
    client: AsyncClient, gates: dict[OperationKind, Gate]
) -> None:
    first = await client.post(OPERATIONS_URL, json={"kind": "embed"})
    await asyncio.wait_for(gates[OperationKind.EMBED].entered.wait(), PATIENCE)

    second = await client.post(OPERATIONS_URL, json={"kind": "embed"})
    assert second.status_code == 409
    assert second.json()["operation_id"] == first.json()["id"]
    assert "уже идёт" in second.json()["detail"]
    assert gates[OperationKind.EMBED].calls == 1

    other = await client.post(OPERATIONS_URL, json={"kind": "match"})
    assert other.status_code == 202


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AppError("Активного резюме нет."), "Активного резюме нет."),
        (RuntimeError("postgres://user:secret@host"), "Подробности записаны в журнал"),
    ],
)
async def test_an_operation_that_fails_ends_failed_with_a_sentence(
    client: AsyncClient, gates: dict[OperationKind, Gate], error: Exception, expected: str
) -> None:
    gates[OperationKind.MATCH].error = error
    gates[OperationKind.MATCH].release.set()
    await client.post(OPERATIONS_URL, json={"kind": "match"})

    finished = await _finish(client, OperationKind.MATCH)
    assert finished["status"] == "failed"
    assert expected in str(finished["error"])
    assert "secret" not in str(finished)


async def test_an_agent_operation_waits_for_the_watcher_and_says_it_is_not_running(
    client: AsyncClient, gates: dict[OperationKind, Gate]
) -> None:
    response = await client.post(OPERATIONS_URL, json={"kind": "outcomes"})
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "waiting_agent"
    # Nobody has asked for work, so the line names what to start.
    assert "не запущен" in body["message"]
    assert "wwao watch" in body["message"]
    assert (await client.get(OPERATIONS_URL)).json()["busy"] == ["outcomes"]


async def test_the_watcher_claims_only_with_the_token_and_ends_the_operation_itself(
    client: AsyncClient, gates: dict[OperationKind, Gate]
) -> None:
    started = (await client.post(OPERATIONS_URL, json={"kind": "send"})).json()

    assert (await client.post(CLAIM_URL)).status_code == 401
    claimed = (await client.post(CLAIM_URL, headers=AUTH)).json()["operation"]
    assert claimed["id"] == started["id"]
    assert claimed["status"] == "running"
    # One request, one claim: a second watcher gets nothing.
    assert (await client.post(CLAIM_URL, headers=AUTH)).json()["operation"] is None

    report_url = f"{settings.api_v1_prefix}/applications/operations/{started['id']}"
    progress = {"status": "running", "message": "отправлен 1 из 2"}
    assert (await client.post(report_url, json=progress)).status_code == 401
    running = (await client.post(report_url, json=progress, headers=AUTH)).json()
    assert "отправлен 1 из 2" in running["message"]

    done = {"status": "success", "message": "готово", "report": ["Отправлено: 2"]}
    finished = (await client.post(report_url, json=done, headers=AUTH)).json()
    assert finished["status"] == "success"
    assert finished["report"] == ["Отправлено: 2"]
    state = (await client.get(OPERATIONS_URL)).json()
    assert state["busy"] == []
    assert state["agent_seen_at"] is not None


async def test_a_waiting_request_can_be_withdrawn_and_a_running_one_cannot(
    client: AsyncClient, gates: dict[OperationKind, Gate]
) -> None:
    waiting = (await client.post(OPERATIONS_URL, json={"kind": "outcomes"})).json()
    cancelled = await client.delete(f"{OPERATIONS_URL}/{waiting['id']}")
    assert cancelled.json()["status"] == "cancelled"
    # Withdrawn means the watcher never sees it.
    assert (await client.post(CLAIM_URL, headers=AUTH)).json()["operation"] is None

    again = (await client.post(OPERATIONS_URL, json={"kind": "outcomes"})).json()
    await client.post(CLAIM_URL, headers=AUTH)
    refused = await client.delete(f"{OPERATIONS_URL}/{again['id']}")
    assert refused.status_code == 409

    unknown = await client.delete(f"{OPERATIONS_URL}/{uuid4()}")
    assert unknown.status_code == 404


async def test_a_report_for_a_backend_operation_is_refused(
    client: AsyncClient, gates: dict[OperationKind, Gate]
) -> None:
    started = (await client.post(OPERATIONS_URL, json={"kind": "embed"})).json()
    url = f"{settings.api_v1_prefix}/applications/operations/{started['id']}"
    refused = await client.post(url, json={"status": "success"}, headers=AUTH)
    assert refused.status_code == 409


async def test_starting_takes_json_and_nothing_else(
    client: AsyncClient, gates: dict[OperationKind, Gate]
) -> None:
    """A cross-origin form post cannot carry JSON without a preflight."""
    form = await client.post(
        OPERATIONS_URL,
        content="kind=embed",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert form.status_code == 422
    unknown = await client.post(OPERATIONS_URL, json={"kind": "apply-everything"})
    assert unknown.status_code == 422
    assert gates[OperationKind.EMBED].calls == 0


async def test_the_crawl_button_uses_the_crawl_job_and_its_refusal(
    client: AsyncClient, gates: dict[OperationKind, Gate], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The crawl keeps its own registry; the panel only shows it in this shape."""
    from app.pipeline import runner as runner_module

    entered = asyncio.Event()
    release = asyncio.Event()

    async def parked(**_: object) -> object:
        entered.set()
        await release.wait()
        raise AppError("Нет активного профиля.")

    monkeypatch.setattr(runner_module, "run_pipeline", parked)
    first = await client.post(OPERATIONS_URL, json={"kind": "crawl"})
    assert first.status_code == 202
    await asyncio.wait_for(entered.wait(), PATIENCE)
    assert (await client.post(OPERATIONS_URL, json={"kind": "crawl"})).status_code == 409
    assert "crawl" in (await client.get(OPERATIONS_URL)).json()["busy"]

    release.set()
    finished = await _finish(client, OperationKind.CRAWL)
    assert finished["status"] == OperationStatus.FAILED.value
    assert finished["error"] == "Нет активного профиля."


# ── the runners, with the work itself replaced ────────────────────────


def _outcome(embedded: int, backlog: int, stopped: str = "budget") -> object:
    from app.pipeline.embedding import EmbeddingOutcome

    return EmbeddingOutcome(
        considered=embedded, unchanged=0, embedded=embedded, backlog=backlog, stopped=stopped
    )


async def test_embedding_runs_passes_until_the_backlog_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    passes = iter([_outcome(50, 70), _outcome(50, 20), _outcome(20, 0, "drained")])
    titles = iter([_outcome(8, 0, "drained")])

    async def descriptions(_: object, **__: object) -> object:
        return next(passes)

    async def title_pass(_: object, **__: object) -> object:
        return next(titles)

    monkeypatch.setattr(operations_service, "embed_pending", descriptions)
    monkeypatch.setattr(operations_service, "embed_pending_titles", title_pass)
    progress = Progress()

    report = await operations_service._embed(progress)

    assert (progress.done, progress.total) == (120, 120)
    assert report[0] == "Векторов описаний посчитано: 120, осталось: 0."
    assert "названий посчитано: 8" in report[1]
    assert len(report) == 2


async def test_embedding_without_a_model_fails_with_what_to_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(_: object, **__: object) -> object:
        return _outcome(0, 500, "unavailable")

    monkeypatch.setattr(operations_service, "embed_pending", unavailable)
    with pytest.raises(AppError, match="uv sync --extra embeddings"):
        await operations_service._embed(Progress())


async def test_embedding_stops_when_a_pass_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def stuck(_: object, **__: object) -> object:
        calls.append("pass")
        return _outcome(0, 40)

    monkeypatch.setattr(operations_service, "embed_pending", stuck)
    monkeypatch.setattr(operations_service, "embed_pending_titles", stuck)

    report = await operations_service._embed(Progress())

    assert calls == ["pass", "pass"]
    assert report[-1].startswith("Остаток есть")


async def test_matching_reports_buckets_and_a_missing_profile_is_a_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.matching.scorer import ProfileNotReadyError, ScoringOutcome

    async def no_vectors(_: object, **__: object) -> str | None:
        return None

    async def scored(_: object, **__: object) -> ScoringOutcome:
        return ScoringOutcome(
            considered=10, written=9, without_embedding=2, buckets={"apply_now": 3, "skip": 6}
        )

    monkeypatch.setattr(operations_service.profile_vectors, "ensure_profile_embedding", no_vectors)
    monkeypatch.setattr(operations_service, "score_corpus", scored)
    monkeypatch.setattr(operations_service, "session_factory", _NoSession)
    progress = Progress()

    report = await operations_service._match(progress)

    assert (progress.done, progress.total) == (9, 10)
    assert report[1] == "По корзинам: откликаться — 3, мимо — 6"
    assert "Без вектора описания: 2" in report[2]

    async def not_ready(_: object, **__: object) -> ScoringOutcome:
        raise ProfileNotReadyError("Нет активного профиля.")

    monkeypatch.setattr(operations_service, "score_corpus", not_ready)
    with pytest.raises(operations_service.ProfileMissingError, match="Нет активного профиля"):
        await operations_service._match(Progress())


async def test_letters_need_a_profile_and_report_what_was_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    profile: object | None = None

    async def load(_: object, __: object) -> object | None:
        return profile

    async def batch(_: object, **__: object) -> list[object]:
        return [
            SimpleNamespace(saved=True, skipped=None, title="Python developer"),
            SimpleNamespace(saved=False, skipped="letter_unwritable", title="Go developer"),
        ]

    monkeypatch.setattr(operations_service.store, "load_profile_facts", load)
    monkeypatch.setattr(operations_service, "write_batch", batch)
    monkeypatch.setattr(operations_service, "session_factory", _NoSession)

    with pytest.raises(operations_service.ProfileMissingError, match="Мои данные"):
        await operations_service._letters(Progress())

    profile = SimpleNamespace(profile_id=uuid4())
    progress = Progress()
    report = await operations_service._letters(progress)
    assert progress.done == 1
    assert report == [
        "Написано писем: 1 из 2.",
        "Go developer: не удалось написать письмо, которое проходит проверки",
    ]

    async def nothing(_: object, **__: object) -> list[object]:
        return []

    monkeypatch.setattr(operations_service, "write_batch", nothing)
    assert "Писать не для чего" in (await operations_service._letters(Progress()))[0]


class _NoSession:
    """A session factory whose session is never asked anything but commit."""

    async def __aenter__(self) -> "_NoSession":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    def __call__(self) -> "_NoSession":
        return self


async def test_embeddings_wait_for_a_running_crawl(
    client: AsyncClient, gates: dict[OperationKind, Gate], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crawl ends in an embedding pass over the same rows; two at once is waste."""
    from app.pipeline import runner as runner_module

    entered = asyncio.Event()
    release = asyncio.Event()

    async def parked(**_: object) -> object:
        entered.set()
        await release.wait()
        raise AppError("стоп")

    monkeypatch.setattr(runner_module, "run_pipeline", parked)
    await client.post(OPERATIONS_URL, json={"kind": "crawl"})
    await asyncio.wait_for(entered.wait(), PATIENCE)

    refused = await client.post(OPERATIONS_URL, json={"kind": "embed"})

    assert refused.status_code == 409
    assert "идёт обход" in refused.json()["detail"]
    assert gates[OperationKind.EMBED].calls == 0
    release.set()
