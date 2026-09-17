"""The dashboard's operation buttons: start, poll, and hand off to the agent.

Until 2026-09-16 the only thing a browser could start was a document. Every
other step of the day — ``wwao crawl``, ``scripts/embed_backlog.py``, ``wwao
match``, ``wwao letters``, ``wwao outcomes`` — lived in a terminal, so somebody
without one could not collect a single vacancy. This module is the other half of
the overview's operations panel.

**The shape is the crawl job's, deliberately.** ``app.services.pipeline``
already argued the design out loud: an asyncio task in this process, a small
in-memory registry of handles, a 409 for a second copy, and a restart that
loses the handle rather than leaving a ``running`` row behind for ever. Every
one of those reasons holds for embedding, scoring and letter-writing too, so the
same decisions are taken here and the crawl itself keeps living where it was —
:func:`state` reads its jobs and shows them beside these.

**One running copy per kind, not one operation overall.** Two scoring passes at
once would write the same ``match`` rows twice; two letter batches would pick
the same vacancies. A letter batch *while* the scorer runs is merely reading a
moving table, which is what a person clicking both buttons expects.

**Two operations are not run here at all.** Reading hh's answers and sending
confirmed applications need the owner's browser and hh session. The API does
not have them and must not (CLAUDE.md: backend is anonymous, agent is local and
never on a server). So for ``outcomes`` and ``send`` this module only records
that the owner asked; the local watcher (``python -m wwao watch``) claims the
request over the token-guarded ``/applications`` seam, runs the agent in its
own process and reports back. A request nobody claims stays
``waiting_agent`` and says what to start — it never pretends to be running.

**Sending still needs a person, and this module cannot supply one.** A ``send``
request carries no applications and no letters: the agent sends only rows the
owner confirmed card by card (``app.services.confirmations``), each bound to
the letter and card digest that was shown. The button is "go ahead with what I
already confirmed", not "send".
"""

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Final
from uuid import UUID, uuid4

from app.core.config import settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.db.session import session_factory
from app.letters import store
from app.letters.service import write_batch
from app.matching import profile_vectors
from app.matching.scorer import ProfileNotReadyError, score_corpus
from app.pipeline.embedding import embed_pending, embed_pending_titles
from app.schemas.operations import (
    AgentProgress,
    OperationKind,
    OperationRead,
    OperationsState,
    OperationStatus,
)
from app.schemas.pipeline_job import PipelineJobRead, PipelineJobStatus
from app.services import pipeline as pipeline_service

logger = get_logger(__name__)

#: Finished operations kept per process. A cache of handles, not history.
HISTORY: Final[int] = 30

#: How many letters one press of the button writes. Each letter is an LLM call
#: that takes tens of seconds; five is what ``wwao letters`` writes by default.
LETTER_BATCH: Final[int] = 5

#: How many embedding passes one press may run. Each pass is bounded by
#: ``EMBEDDING_TIME_BUDGET_SECONDS``; the cap stops a pass that keeps finding
#: work from running all night off one click.
MAX_EMBED_PASSES: Final[int] = 20

#: How long a watcher may stay silent before the panel stops calling it alive.
AGENT_STALE_SECONDS: Final[int] = 90

#: The command the owner has to have running for the two agent operations.
WATCH_COMMAND: Final[str] = "python -m wwao watch"


class OperationBusyError(AppError):
    """This kind of operation is already running."""

    status_code = HTTPStatus.CONFLICT
    title = "Operation already running"
    problem_type = "operation-busy"


class OperationNotFoundError(AppError):
    """No operation with that id in this process."""

    status_code = HTTPStatus.NOT_FOUND
    title = "Operation not found"
    problem_type = "operation-unknown"


class OperationNotCancellableError(AppError):
    """Only a request the agent has not picked up yet can be withdrawn."""

    status_code = HTTPStatus.CONFLICT
    title = "Operation cannot be cancelled"
    problem_type = "operation-not-cancellable"


class EmbeddingUnavailableError(AppError):
    """The embedding model is not installed or not configured."""

    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    title = "Embedding model unavailable"
    problem_type = "embedding-unavailable"


class ProfileMissingError(AppError):
    """Scoring or writing needs a parsed, active resume."""

    status_code = HTTPStatus.CONFLICT
    title = "Profile not ready"
    problem_type = "profile-not-ready"


@dataclass(slots=True)
class Progress:
    """What a running backend operation can say about itself while it runs."""

    done: int | None = None
    total: int | None = None
    note: str | None = None


@dataclass(slots=True)
class _Operation:
    """The mutable half of an operation. Never leaves this module."""

    id: UUID
    kind: OperationKind
    status: OperationStatus
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    progress: Progress = field(default_factory=Progress)
    report: list[str] = field(default_factory=list)
    error: str | None = None
    #: When the watcher last reported on an agent operation.
    heard_at: datetime | None = None
    #: Held so the task is not garbage collected mid-run.
    task: asyncio.Task[None] | None = None


#: A backend operation: does the work, updates ``progress`` as it goes, returns
#: the report lines. Raises ``AppError`` with a sentence for a person.
Runner = Callable[[Progress], Awaitable[list[str]]]


class OperationRegistry:
    """Operations this process knows about, oldest first."""

    def __init__(self, runners: Mapping[OperationKind, Runner]) -> None:
        self._runners = dict(runners)
        self._operations: OrderedDict[UUID, _Operation] = OrderedDict()
        self.agent_seen_at: datetime | None = None

    # ── starting ──────────────────────────────────────────────────────

    def start(self, kind: OperationKind) -> _Operation:
        """Accept one operation of this kind, or refuse because one is live."""
        live = self.live(kind)
        if live is not None:
            raise OperationBusyError(
                f"{_TITLES[kind]}: уже идёт. Дождитесь окончания — вторая копия "
                "сделала бы ту же работу дважды.",
                operation_id=str(live.id),
            )
        operation = _Operation(
            id=uuid4(),
            kind=kind,
            status=OperationStatus.WAITING_AGENT if kind.needs_agent else OperationStatus.QUEUED,
            queued_at=datetime.now(UTC),
        )
        self._operations[operation.id] = operation
        self._evict()
        if not kind.needs_agent:
            operation.task = asyncio.create_task(self._execute(operation), name=f"op-{kind}")
        logger.info("operations.accepted", kind=kind.value, operation_id=str(operation.id))
        return operation

    async def _execute(self, operation: _Operation) -> None:
        """Run a backend operation and record how it ended, whatever happens."""
        operation.status = OperationStatus.RUNNING
        operation.started_at = datetime.now(UTC)
        try:
            operation.report = await self._runners[operation.kind](operation.progress)
        except asyncio.CancelledError:
            operation.status = OperationStatus.CANCELLED
            operation.error = "Операция прервана: сервер остановили."
            raise
        except AppError as exc:
            operation.status = OperationStatus.FAILED
            operation.error = exc.detail
            logger.warning("operations.failed", kind=operation.kind.value, problem=exc.problem_type)
        except Exception as exc:
            # A bug. The traceback goes to the log; the screen gets a sentence,
            # because an exception's text can carry a URL with a key in it.
            operation.status = OperationStatus.FAILED
            operation.error = "Операцию прервала ошибка. Подробности записаны в журнал сервера."
            logger.exception(
                "operations.crashed", kind=operation.kind.value, error=type(exc).__name__
            )
        else:
            operation.status = OperationStatus.SUCCESS
            logger.info("operations.finished", kind=operation.kind.value)
        finally:
            if not operation.status.is_terminal:  # pragma: no cover - BaseException only
                operation.status = OperationStatus.FAILED
                operation.error = "Операция прервана неизвестной ошибкой."
            operation.finished_at = datetime.now(UTC)

    # ── the agent's side ──────────────────────────────────────────────

    def claim(self) -> _Operation | None:
        """Hand the oldest unclaimed agent request to the watcher asking."""
        self.agent_seen_at = datetime.now(UTC)
        for operation in self._operations.values():
            if operation.status is OperationStatus.WAITING_AGENT:
                operation.status = OperationStatus.RUNNING
                operation.started_at = operation.heard_at = datetime.now(UTC)
                logger.info("operations.claimed", kind=operation.kind.value)
                return operation
        return None

    def report_progress(self, operation_id: UUID, progress: AgentProgress) -> _Operation:
        """Record what the watcher says about an operation it claimed."""
        operation = self.get(operation_id)
        if not operation.kind.needs_agent or operation.status is not OperationStatus.RUNNING:
            raise OperationNotCancellableError(
                "Эта операция не ждёт отчёта агента.", operation_id=str(operation_id)
            )
        now = datetime.now(UTC)
        self.agent_seen_at = operation.heard_at = now
        if progress.message:
            operation.progress.note = progress.message
        if progress.report:
            operation.report = list(progress.report)
        if progress.status.is_terminal:
            operation.status = progress.status
            operation.finished_at = now
            if progress.status is not OperationStatus.SUCCESS:
                operation.error = progress.message or "Агент завершился с ошибкой."
        return operation

    def cancel(self, operation_id: UUID) -> _Operation:
        """Withdraw an agent request nobody has picked up yet."""
        operation = self.get(operation_id)
        if operation.status is not OperationStatus.WAITING_AGENT:
            raise OperationNotCancellableError(
                "Отменить можно только запрос, который агент ещё не взял.",
                operation_id=str(operation_id),
            )
        operation.status = OperationStatus.CANCELLED
        operation.finished_at = datetime.now(UTC)
        operation.error = "Запрос отменён до того, как агент его взял."
        return operation

    # ── reading ───────────────────────────────────────────────────────

    def get(self, operation_id: UUID) -> _Operation:
        """One operation, or a 404 that says why it may be gone."""
        operation = self._operations.get(operation_id)
        if operation is None:
            raise OperationNotFoundError(
                "Операция не найдена. Операции живут в памяти сервера: после его "
                "перезапуска их не остаётся, а сделанное ими остаётся в базе."
            )
        return operation

    def live(self, kind: OperationKind) -> _Operation | None:
        """The operation of this kind that has not finished, if any."""
        for operation in self._operations.values():
            if operation.kind is kind and not operation.status.is_terminal:
                return operation
        return None

    def newest(self, kind: OperationKind) -> _Operation | None:
        """The most recently accepted operation of this kind."""
        for operation in reversed(self._operations.values()):
            if operation.kind is kind:
                return operation
        return None

    def agent_alive(self) -> bool:
        """Whether a watcher has asked for work recently."""
        if self.agent_seen_at is None:
            return False
        return (datetime.now(UTC) - self.agent_seen_at).total_seconds() <= AGENT_STALE_SECONDS

    def _evict(self) -> None:
        """Forget the oldest finished operations once there are too many."""
        for operation_id, operation in list(self._operations.items()):
            if len(self._operations) <= HISTORY:
                return
            if operation.status.is_terminal:
                del self._operations[operation_id]


# ── the runners ───────────────────────────────────────────────────────


async def _embed(progress: Progress) -> list[str]:
    """Compute missing description vectors, then title vectors.

    Passes until the backlog is gone, the model is missing, a pass writes
    nothing, or :data:`MAX_EMBED_PASSES` is spent — the same stopping rules as
    ``scripts/embed_backlog.py --until-drained``.
    """
    written = 0
    backlog: int | None = None
    for _ in range(MAX_EMBED_PASSES):
        async with session_factory() as session:
            outcome = await embed_pending(session)
        if outcome.stopped == "unavailable":
            raise EmbeddingUnavailableError(
                "Векторы не посчитаны: модель эмбеддингов недоступна. Установите её "
                "(uv sync --extra embeddings) или включите EMBEDDING_PROVIDER=fake."
            )
        written += outcome.embedded
        backlog = outcome.backlog
        progress.done, progress.total = written, written + backlog
        progress.note = f"описания: посчитано {written}, осталось {backlog}"
        if outcome.backlog == 0 or outcome.embedded == 0:
            break

    titles_written = 0
    title_backlog: int | None = None
    for _ in range(MAX_EMBED_PASSES):
        async with session_factory() as session:
            outcome = await embed_pending_titles(session)
        titles_written += outcome.embedded
        title_backlog = outcome.backlog
        progress.note = f"названия: посчитано {titles_written}, осталось {title_backlog}"
        if outcome.stopped == "unavailable" or outcome.backlog == 0 or outcome.embedded == 0:
            break

    lines = [f"Векторов описаний посчитано: {written}, осталось: {backlog}."]
    lines.append(f"Векторов названий посчитано: {titles_written}, осталось: {title_backlog}.")
    if backlog:
        lines.append("Остаток есть — нажмите ещё раз, продолжится с того же места.")
    return lines


async def _match(progress: Progress) -> list[str]:
    """Embed the profile if needed, then score the corpus and keep the result."""
    async with session_factory() as session:
        note = await profile_vectors.ensure_profile_embedding(session, allowed=True)
        try:
            outcome = await score_corpus(session)
        except ProfileNotReadyError as error:
            raise ProfileMissingError(str(error)) from error
        await session.commit()
    progress.done = outcome.written
    progress.total = outcome.considered
    lines = [
        f"Рассмотрено вакансий: {outcome.considered}, записано оценок: {outcome.written}.",
        "По корзинам: "
        + ", ".join(
            f"{_BUCKETS.get(key, key)} — {count}" for key, count in outcome.buckets.items()
        ),
    ]
    if outcome.without_embedding:
        lines.append(
            f"Без вектора описания: {outcome.without_embedding} — посчитайте эмбеддинги и "
            "пересчитайте подбор."
        )
    if note:
        lines.append(note)
    return lines


async def _letters(progress: Progress) -> list[str]:
    """Write letters for the best-scored vacancies that have none yet."""
    async with session_factory() as session:
        profile = await store.load_profile_facts(session, None)
        if profile is None:
            raise ProfileMissingError(
                "Писать не для кого: активного резюме нет. Загрузите резюме на «Мои данные»."
            )
        progress.total = LETTER_BATCH
        outcomes = await write_batch(session, profile_id=profile.profile_id, limit=LETTER_BATCH)
        await session.commit()
    saved = [outcome for outcome in outcomes if outcome.saved]
    progress.done = len(saved)
    if not outcomes:
        return [
            "Писать не для чего: у всех подходящих вакансий письма уже есть, или ни одна "
            f"не набрала порог {settings.agent_queue_min_score}."
        ]
    lines = [f"Написано писем: {len(saved)} из {len(outcomes)}."]
    lines.extend(
        f"{outcome.title}: {_SKIPPED.get(outcome.skipped, outcome.skipped)}"
        for outcome in outcomes
        if outcome.skipped
    )
    return lines


_BUCKETS: Final[dict[str, str]] = {
    "apply_now": "откликаться",
    "strong": "сильное",
    "stretch": "на вырост",
    "skip": "мимо",
    "filtered": "отфильтровано",
}

_SKIPPED: Final[dict[str, str]] = {
    "vacancy_not_found": "вакансия не найдена",
    "letter_exists": "письмо уже есть",
    "dry_run": "пробный прогон",
    "letter_unwritable": "не удалось написать письмо, которое проходит проверки",
}

_TITLES: Final[dict[OperationKind, str]] = {
    OperationKind.CRAWL: "Сбор вакансий",
    OperationKind.EMBED: "Эмбеддинги",
    OperationKind.MATCH: "Пересчёт подбора",
    OperationKind.LETTERS: "Письма",
    OperationKind.OUTCOMES: "Исходы откликов",
    OperationKind.SEND: "Отправка подтверждённых",
}

#: Replaced wholesale in tests, which is also what a restart looks like.
_registry = OperationRegistry(
    {
        OperationKind.EMBED: _embed,
        OperationKind.MATCH: _match,
        OperationKind.LETTERS: _letters,
    }
)


# ── the public surface ────────────────────────────────────────────────


async def start(kind: OperationKind) -> OperationRead:
    """Start one operation and answer with its handle."""
    if kind is OperationKind.CRAWL:
        return _from_crawl(await pipeline_service.request_run())
    if kind is OperationKind.EMBED and pipeline_service.list_jobs(limit=1).busy:
        # Found walking the dashboard (2026-09-17): a crawl ends in an embedding
        # pass over the same rows, so a second pass alongside it only halves the
        # speed of both on one CPU.
        raise OperationBusyError(
            "Эмбеддинги: сейчас идёт обход, и он сам считает векторы новых вакансий. "
            "Дождитесь его окончания — остаток, если будет, посчитается этой кнопкой."
        )
    return view(_registry.start(kind))


def cancel(operation_id: UUID) -> OperationRead:
    """Withdraw an agent request nobody has claimed."""
    return view(_registry.cancel(operation_id))


def state() -> OperationsState:
    """The newest operation of every kind, and which kinds are busy."""
    operations: list[OperationRead] = []
    busy: list[OperationKind] = []
    crawl = pipeline_service.list_jobs(limit=pipeline_service.JOB_HISTORY)
    newest_crawl = next((job for job in crawl.jobs if not job.dry_run), None)
    if newest_crawl is not None:
        operations.append(_from_crawl(newest_crawl))
    if crawl.busy:
        busy.append(OperationKind.CRAWL)
    for kind in OperationKind:
        if kind is OperationKind.CRAWL:
            continue
        newest = _registry.newest(kind)
        if newest is not None:
            operations.append(view(newest))
        if _registry.live(kind) is not None:
            busy.append(kind)
    return OperationsState(operations=operations, busy=busy, agent_seen_at=_registry.agent_seen_at)


def claim_for_agent() -> OperationRead | None:
    """The oldest agent request, now marked as taken by the caller."""
    claimed = _registry.claim()
    return None if claimed is None else view(claimed)


def report_from_agent(operation_id: UUID, progress: AgentProgress) -> OperationRead:
    """What the watcher says about the operation it is running."""
    return view(_registry.report_progress(operation_id, progress))


def view(operation: _Operation) -> OperationRead:
    """The wire form, with the clock read at the moment of asking."""
    return OperationRead(
        id=operation.id,
        kind=operation.kind,
        status=operation.status,
        message=_message(operation),
        queued_at=operation.queued_at,
        started_at=operation.started_at,
        finished_at=operation.finished_at,
        duration_seconds=_elapsed(operation.started_at, operation.finished_at),
        done=operation.progress.done,
        total=operation.progress.total,
        report=list(operation.report),
        error=operation.error,
    )


def _elapsed(started: datetime | None, finished: datetime | None) -> float | None:
    """Seconds so far while running, in total once finished."""
    if started is None:
        return None
    return round(((finished or datetime.now(UTC)) - started).total_seconds(), 3)


def _message(operation: _Operation) -> str:
    """One line about this operation, for the panel to render as it is."""
    title = _TITLES[operation.kind]
    status = operation.status
    if status is OperationStatus.QUEUED:
        return f"{title}: принято, сейчас начнётся."
    if status is OperationStatus.WAITING_AGENT:
        if _registry.agent_alive():
            return f"{title}: ждёт локального агента, он заберёт запрос в течение минуты."
        return (
            f"{title}: ждёт локального агента, а он не запущен. Запустите приложение "
            f"двойным щелчком по start.cmd (или командой {WATCH_COMMAND}) на своём компьютере."
        )
    if status is OperationStatus.RUNNING:
        note = f" {operation.progress.note}." if operation.progress.note else ""
        return f"{title}: идёт.{note}"
    if status is OperationStatus.SUCCESS:
        return f"{title}: готово."
    return operation.error or f"{title}: не удалось."


def _from_crawl(job: PipelineJobRead) -> OperationRead:
    """A crawl job in this envelope, so the panel reads one shape."""
    statuses = {
        PipelineJobStatus.QUEUED: OperationStatus.QUEUED,
        PipelineJobStatus.RUNNING: OperationStatus.RUNNING,
        PipelineJobStatus.SUCCESS: OperationStatus.SUCCESS,
        PipelineJobStatus.FAILED: OperationStatus.FAILED,
        PipelineJobStatus.CANCELLED: OperationStatus.CANCELLED,
    }
    report: list[str] = []
    if job.report is not None:
        report.append(
            f"Найдено: {job.report.found}, новых: {job.report.new}, "
            f"дублей схлопнуто: {job.report.duplicates}."
        )
    return OperationRead(
        id=job.id,
        kind=OperationKind.CRAWL,
        status=statuses[job.status],
        message=job.message,
        queued_at=job.queued_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        duration_seconds=job.duration_seconds,
        done=job.report.new if job.report is not None else None,
        report=report,
        error=job.error,
    )
