"""One button for the whole day, and one confirmation for the whole batch.

Two things live here, and they are two halves of the same bargain.

**The chain** runs the day's work in order — collect, embed, score, write
letters, build the queue — from one press. It is long: measured on this corpus
the crawl step alone took 79, 97 and 178 minutes on 19, 17 and 8 September, and
embeddings on a CPU are another twenty. So it reports per step rather than as
one bar, stops at the step that failed and says which, and a second press
continues from there instead of re-crawling.

The twenty minutes quoted elsewhere in this project predate the night crawl:
hh's declared rate is 0.25 requests a second and one run may fetch 1200 vacancy
pages, which is ninety minutes before the sitemap and catalogue reads.

**The batch** is the one thing the chain does not do. It ends with a ready
queue and nothing sent. Sending still needs the owner to read what is about to
go out under their name and say yes — once for the whole batch rather than once
per vacancy, which is the point of the change, but a "yes" that still binds each
letter separately: :func:`confirm` records N confirmations, each bound to the
digest of the card it was given to, and the agent mints one mandate per vacancy
against those digests. A letter that changed between the confirmation and the
send does not go.

**Why one confirmation per batch is safe now and was not before.** Every
end-to-end run of this project until 17 Sep 2026 put something in the queue that
should not have been there — vacancies the scorer had filtered, archived
postings nobody could apply to, a «Бариста» matched on the skill "go", letters
written for a source the agent cannot send on, a posting asking six years of a
candidate with one. Each of those was caught by a person looking at a card. The
selection in ``app.services.agent_queue`` is what replaces that look: an item
reaches this batch only after the score, the bucket, the source, the experience
gap, the language requirement, the workshop rules, the ATS audit and a re-read
of the posting's own page have all passed. What the owner reads in the batch
screen is the letter, which is the one thing no check can judge for them.

**There is no flag that skips it, here or anywhere.** The chain has no ``--yes``
and the schedule starts the chain, never a send; ``wwao/tests/test_cli.py`` and
``backend/tests/test_autopilot.py`` both hold that line. The limits below exist
for the same reason: a mistake in the selection is discovered by an employer
reading it, so the first batch after a quiet period is deliberately small.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from http import HTTPStatus
from typing import Final, final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.db.models import Application
from app.db.session import session_factory
from app.letters import service as letters_service
from app.letters import store as letters_store
from app.matching import profile_vectors
from app.matching.scorer import ProfileNotReadyError, score_corpus
from app.pipeline.embedding import EmbeddingOutcome, embed_pending, embed_pending_titles
from app.schemas.autopilot import (
    BatchConfirmOutcome,
    BatchConfirmRequest,
    BatchConfirmResult,
    BatchItem,
    BatchPlan,
    SetAsideKind,
)
from app.schemas.operations import ChainStep, ChainStepStatus
from app.schemas.pipeline_job import PipelineJobStatus
from app.services import agent_queue, confirmations, freshness
from app.services import pipeline as pipeline_service

logger = get_logger(__name__)

#: How many vacancies the chain looks at when it builds the queue. Well above
#: the batch ceiling, because the second list — what went to a person and why —
#: is most of the value of looking.
SELECTION_LIMIT: Final[int] = 50

#: How many letters one chain writes. Each is an LLM call of tens of seconds,
#: and the batch ceiling is what a person will be asked to read in one sitting,
#: so writing far past it spends money on letters nobody will confirm today.
LETTER_LIMIT: Final[int] = 10

#: How many embedding passes the chain's step runs. Same ceiling as the single
#: operation's, for the same reason: a pass that keeps finding work must not run
#: all night off one press.
MAX_EMBED_PASSES: Final[int] = 20

#: How often the chain asks a running crawl whether it has finished.
CRAWL_POLL_SECONDS: Final[float] = 5.0

#: How long an interrupted chain may be resumed for. Past this the next press
#: starts from the top, because the steps it would skip — yesterday's crawl,
#: yesterday's scoring — are exactly the ones whose answers have gone stale.
RESUME_WINDOW: Final[timedelta] = timedelta(hours=12)


class ChainStepFailedError(AppError):
    """One step of the chain failed; the chain stopped there and says which."""

    status_code = HTTPStatus.CONFLICT
    title = "Chain step failed"
    problem_type = "chain-step-failed"


class BatchRefusedError(AppError):
    """The batch as asked for cannot be confirmed — too large, or not offered."""

    status_code = HTTPStatus.CONFLICT
    title = "Batch refused"
    problem_type = "batch-refused"


# ── the chain ─────────────────────────────────────────────────────────


@final
@dataclass(slots=True)
class _Memory:
    """What an interrupted chain left behind, so the next one can continue.

    In this process and nowhere else, exactly like the operations and the crawl
    jobs it drives: a restart loses it, and losing it means the next chain
    starts from the top, which is the honest fallback rather than a row stuck at
    "half done" for ever. What the steps actually *did* is durable — in
    ``vacancy``, ``match``, ``application`` — which is the part that matters.
    """

    finished: set[str] = field(default_factory=set)
    failed_at: str | None = None
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def resumable(self, *, now: datetime | None = None) -> bool:
        """Whether the next chain may skip what this one finished."""
        return self.failed_at is not None and (now or datetime.now(UTC)) - self.at < RESUME_WINDOW


#: Replaced wholesale by tests, which is also what a restart looks like.
_memory: _Memory | None = None


def forget() -> None:
    """Drop the resume point. For tests, and for a restart's own bookkeeping."""
    global _memory
    _memory = None


async def run_chain(steps: list[ChainStep]) -> list[str]:
    """Collect, embed, score, write and select — in that order, stopping at a failure.

    ``steps`` is the list the operations registry hands to the panel; it is
    mutated in place as the chain goes, which is what makes progress visible
    per step while an hour of crawling is happening behind it.

    Nothing here sends anything, and nothing here can: the last step ends with a
    queue and a count. See the module docstring.
    """
    global _memory
    resume = _memory.finished if _memory is not None and _memory.resumable() else set()
    if resume:
        logger.info("autopilot.chain.resuming", after=sorted(resume))
    done: set[str] = set()
    report: list[str] = []

    for step in steps:
        runner = _RUNNERS[step.key]
        if step.key in resume:
            step.status = ChainStepStatus.SKIPPED
            step.note = "уже сделано в прошлом запуске, который не дошёл до конца"
            done.add(step.key)
            continue
        step.status = ChainStepStatus.RUNNING
        step.started_at = datetime.now(UTC)
        try:
            step.report = await runner(step)
        except AppError as error:
            step.status = ChainStepStatus.FAILED
            step.finished_at = datetime.now(UTC)
            step.note = error.detail
            _memory = _Memory(finished=done, failed_at=step.key)
            raise ChainStepFailedError(
                f"Цепочка остановилась на шаге «{step.title}»: {error.detail} "
                "Остальные шаги не начинались. Повторный запуск продолжит с этого шага."
            ) from error
        except Exception:
            step.status = ChainStepStatus.FAILED
            step.finished_at = datetime.now(UTC)
            step.note = "шаг прервала ошибка; подробности в журнале сервера"
            _memory = _Memory(finished=done, failed_at=step.key)
            logger.exception("autopilot.chain.crashed", step=step.key)
            raise
        step.status = ChainStepStatus.DONE
        step.finished_at = datetime.now(UTC)
        step.note = None
        done.add(step.key)
        report.extend(step.report)

    _memory = None
    return report


async def _step_crawl(step: ChainStep) -> list[str]:
    """Walk the enabled sources, and wait for the walk this chain started.

    Through ``app.services.pipeline`` rather than the runner, so a crawl started
    from the panel and a crawl started by the chain are the same job with the
    same "only one at a time" rule — and so the chain waits for a crawl already
    running instead of refusing beside it.
    """
    known = pipeline_service.list_jobs(limit=pipeline_service.JOB_HISTORY)
    # The crawl that is actually in flight, not simply the newest one: a dry run
    # accepted a moment ago would be the newest and would end at once, and the
    # chain would walk on believing the crawl behind it had finished.
    running = next(
        (job for job in known.jobs if job.status in _CRAWL_LIVE and not job.dry_run), None
    )
    job = running if running is not None else await pipeline_service.request_run()
    step.note = (
        "жду обход, запущенный раньше"
        if running is not None
        else "идёт обход источников — это самая долгая часть, от часа до трёх"
    )
    while True:
        current = pipeline_service.read_job(job.id)
        if current.status in _CRAWL_TERMINAL:
            break
        await asyncio.sleep(CRAWL_POLL_SECONDS)
    if current.status is not PipelineJobStatus.SUCCESS:
        raise ChainStepFailedError(current.error or "обход закончился неуспешно.")
    if current.report is None:  # pragma: no cover - a success always carries one
        return ["Обход закончен."]
    return [
        f"Найдено: {current.report.found}, новых: {current.report.new}, "
        f"дублей схлопнуто: {current.report.duplicates}."
    ]


#: A crawl that has not finished. Its complement is what the wait above ends on.
_CRAWL_LIVE: Final[frozenset[PipelineJobStatus]] = frozenset(
    {PipelineJobStatus.QUEUED, PipelineJobStatus.RUNNING}
)

_CRAWL_TERMINAL: Final[frozenset[PipelineJobStatus]] = frozenset(
    {PipelineJobStatus.SUCCESS, PipelineJobStatus.FAILED, PipelineJobStatus.CANCELLED}
)


async def _step_embed(step: ChainStep) -> list[str]:
    """Compute the vectors the scoring step needs, descriptions then titles."""
    written = 0
    last: EmbeddingOutcome | None = None
    for _ in range(MAX_EMBED_PASSES):
        async with session_factory() as session:
            last = await embed_pending(session)
        if last.stopped == "unavailable":
            raise ChainStepFailedError(
                "модель эмбеддингов недоступна — установите её (uv sync --extra embeddings) "
                "или включите EMBEDDING_PROVIDER=fake."
            )
        written += last.embedded
        step.note = f"описания: посчитано {written}"
        if last.stopped != "budget" or last.embedded == 0:
            break

    titles = 0
    last_titles: EmbeddingOutcome | None = None
    for _ in range(MAX_EMBED_PASSES):
        async with session_factory() as session:
            last_titles = await embed_pending_titles(session)
        titles += last_titles.embedded
        step.note = f"названия: посчитано {titles}"
        if last_titles.stopped != "budget" or last_titles.embedded == 0:
            break
    return [
        f"Векторов посчитано: описаний {written}, названий {titles}.",
        f"Описания: {_left(last)} Названия: {_left(last_titles)}",
    ]


def _left(outcome: EmbeddingOutcome | None) -> str:
    """What is left after the passes, said from why the last one stopped.

    Zero written means four different things, and on the live run of 18 Sep 2026
    it meant the fourth: the step reported «посчитано 0» and the scoring step
    right after it reported 666 vacancies with no description vector. A number
    with no reason beside it reads as "nothing to do".

    The sentences are this module's own rather than imported from
    ``app.services.operations``: that module imports this one to run the chain,
    so the dependency only goes one way, and these are four short strings rather
    than a rule worth sharing.
    """
    if outcome is None or outcome.stopped == "drained":
        return "всё посчитано."
    if outcome.stopped == "budget":
        return "время прохода вышло, остаток есть — следующий запуск продолжит с того же места."
    if outcome.stopped == "starved":
        return (
            "без вектора остались строки, которые выборка не отдаёт; повторный запуск не "
            "поможет — это чинится в коде."
        )
    return "модель эмбеддингов недоступна."


async def _step_match(step: ChainStep) -> list[str]:
    """Score the corpus against the active resume."""
    async with session_factory() as session:
        note = await profile_vectors.ensure_profile_embedding(session, allowed=True)
        try:
            outcome = await score_corpus(session)
        except ProfileNotReadyError as error:
            raise ChainStepFailedError(str(error)) from error
        await session.commit()
    lines = [f"Рассмотрено вакансий: {outcome.considered}, записано оценок: {outcome.written}."]
    if outcome.without_embedding:
        lines.append(f"Без вектора описания осталось: {outcome.without_embedding}.")
    if note:
        lines.append(note)
    return lines


async def _step_letters(step: ChainStep) -> list[str]:
    """Re-read the candidates' pages, then write letters for the ones that passed.

    The order inside this step is the whole of its design. Re-reading first
    means no letter is written for a posting that was archived a week ago — on
    17 Sep 2026 that was the entire corpus, nine days stale — and it is also
    what makes "прошедшие отбор" a list this step can actually name: after the
    pass, a vacancy set aside for having no letter is one that has cleared every
    other check.
    """
    async with session_factory() as session:
        stale = await _stale_candidates(session)
        if stale:
            step.note = f"перечитываю страницы вакансий: {len(stale)}"
            outcome = await freshness.refresh(session, stale)
            lines = [_refresh_line(outcome)]
        else:
            lines = ["Перечитывать было нечего: все страницы свежие."]

    async with session_factory() as session:
        selection = await agent_queue.triage(session, limit=SELECTION_LIMIT, require_letter=False)
    # Ready-but-letterless, which with ``require_letter=False`` is exactly "has
    # cleared every check except the one about a letter it does not have yet".
    waiting = [ready.vacancy_id for ready in selection.ready if ready.item.letter is None][
        :LETTER_LIMIT
    ]
    if not waiting:
        lines.append("Писать не для чего: у всех прошедших отбор письма уже есть.")
        return lines

    step.note = f"пишу письма: 0 из {len(waiting)}"
    written = 0
    async with session_factory() as session:
        profile = await letters_store.load_profile_facts(session, None)
        if profile is None:
            raise ChainStepFailedError("активного резюме нет — загрузите его на «Мои данные».")
        pool = await letters_store.load_examples(session, profile_id=profile.profile_id)
        bench = await letters_service.load_workshop(session)
        for number, vacancy_id in enumerate(waiting, start=1):
            letter = await letters_service.write_letter(
                session, vacancy_id, profile, pool=pool, workshop=bench
            )
            written += 1 if letter.saved else 0
            step.note = f"пишу письма: {number} из {len(waiting)}"
            if letter.skipped:
                lines.append(f"{letter.title}: {_SKIPPED.get(letter.skipped, letter.skipped)}")
        await session.commit()
    lines.append(f"Написано писем: {written} из {len(waiting)}.")
    return lines


async def _step_queue(step: ChainStep) -> list[str]:
    """Build the queue and say what passed and what went to a person.

    The last thing the chain does, and it is a measurement rather than an
    action: how many applications are ready to be confirmed, how many vacancies
    were set aside, and why. It re-reads any page that is still stale — the
    letters step covers the candidates it knew about, and a letter written
    during it can move a vacancy into the ready list whose page nobody read.
    """
    async with session_factory() as session:
        stale = await _stale_candidates(session)
        if stale:
            step.note = f"перечитываю оставшиеся страницы: {len(stale)}"
            await freshness.refresh(session, stale)

    async with session_factory() as session:
        selection = await agent_queue.triage(session, limit=SELECTION_LIMIT)
    lines = [
        f"Готово к отправке: {len(selection.ready)}. "
        f"В «посмотреть руками»: {len(selection.set_aside)}."
    ]
    counts: dict[SetAsideKind, int] = {}
    for item in selection.set_aside:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    lines.extend(
        f"{_KIND_TITLES[kind]}: {count}"
        for kind, count in sorted(counts.items(), key=lambda pair: -pair[1])
    )
    lines.append("Ничего не отправлено: отправка начинается только с подтверждения пачки.")
    return lines


async def _stale_candidates(session: AsyncSession) -> list[UUID]:
    """Vacancies worth re-reading: the ready ones, and those stale only by page age.

    Both halves. The set-aside rows of kind ``stale_page`` are the ones whose
    *only* remaining problem is that nobody has looked — see the ordering in
    ``app.services.agent_queue._refusal`` — and the ready ones are re-read
    because "ready" is a claim this project is about to act on.

    **Capped, and the cap is the point rather than a precaution.** Every id in
    this list becomes one page fetched from hh at the connector's declared rate,
    about four seconds apart, and the chain does this twice. Uncapped it is
    bounded only by how many rows the triage pass happened to read — which after
    the selection stopped hiding letterless rows is several hundred — so a chain
    could spend half an hour re-reading pages of vacancies no batch could ever
    include. A batch cannot exceed ``AGENT_BATCH_LIMIT``, so reading past
    :data:`SELECTION_LIMIT` pages cannot change what goes out.

    Highest score first inside each half, because that is the order the triage
    pass returns and therefore the order in which a re-read is most likely to
    put something into today's batch.
    """
    selection = await agent_queue.triage(session, limit=SELECTION_LIMIT, require_letter=False)
    ready = [ready.vacancy_id for ready in selection.ready]
    stale = [
        item.vacancy_id for item in selection.set_aside if item.kind is SetAsideKind.STALE_PAGE
    ]
    return list(dict.fromkeys([*stale, *ready]))[:SELECTION_LIMIT]


def _refresh_line(outcome: freshness.RefreshOutcome) -> str:
    """What one re-reading pass did, in one line for the report."""
    parts = [
        f"Перечитано страниц: {outcome.checked}, из них закрыто или в архиве: {outcome.closed}."
    ]
    if outcome.unreadable:
        parts.append(f"Не прочитано: {outcome.unreadable}.")
    if outcome.unsupported:
        parts.append(f"Источник не умеет перечитывать: {outcome.unsupported}.")
    if outcome.stopped:
        parts.append(outcome.stopped)
    return " ".join(parts)


#: The chain, in order. The panel draws these titles; the keys are what a resume
#: point is written against, so renaming one starts the next chain from the top
#: rather than silently skipping a different step.
CHAIN_STEPS: Final[tuple[tuple[str, str], ...]] = (
    ("crawl", "Сбор вакансий"),
    ("embed", "Эмбеддинги"),
    ("match", "Подбор"),
    ("letters", "Письма"),
    ("queue", "Очередь"),
)

#: What runs one step: it takes its own :class:`ChainStep` so it can say what it
#: is doing while it does it, and returns the lines that step reports.
type ChainRunner = Callable[[ChainStep], Awaitable[list[str]]]

_RUNNERS: Final[dict[str, ChainRunner]] = {
    "crawl": _step_crawl,
    "embed": _step_embed,
    "match": _step_match,
    "letters": _step_letters,
    "queue": _step_queue,
}


def fresh_steps() -> list[ChainStep]:
    """The chain's steps, all pending. One list per started operation."""
    return [ChainStep(key=key, title=title) for key, title in CHAIN_STEPS]


# ── the schedule ──────────────────────────────────────────────────────


def next_run(after: datetime, at: time) -> datetime:
    """The next time of day ``at`` strictly after ``after``, in local time.

    Local rather than UTC, because the owner sets it by their own clock and a
    chain is about their day. Strictly after, so a process started at exactly
    03:00 with ``AUTOPILOT_DAILY_AT=03:00`` waits for tomorrow instead of
    starting a chain the moment it boots.
    """
    today = after.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    return today if today > after else today + timedelta(days=1)


async def schedule_daily(
    start: "Callable[[], Awaitable[None]]",
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(),
    rounds: int | None = None,
) -> None:
    """Start the chain once a day at ``AUTOPILOT_DAILY_AT``, for as long as we run.

    **It starts the chain and nothing else, and that is the whole rule.** The
    chain collects and prepares; an application leaves only after somebody reads
    the batch and confirms it. There is no setting that lets this send, and
    adding one would be the thing the brief forbids in as many words.

    In this process, like every other job here. The dashboard is a program the
    owner starts on their own machine, so "раз в сутки" only holds while it is
    running — the README's Windows task is the other half, and it runs
    ``python -m wwao chain``, the same chain through the same endpoint.

    A failed chain is logged and the loop stays: tomorrow's collection should
    not be cancelled by today's hh challenge. Two chains cannot overlap either
    way, because :func:`app.services.operations.start` refuses a second one.
    """
    configured = settings.autopilot_daily_at
    if configured is None:
        return
    at = time.fromisoformat(configured)
    logger.info("autopilot.schedule.armed", at=configured)
    done = 0
    while rounds is None or done < rounds:
        done += 1
        moment = now()
        wait = (next_run(moment, at) - moment).total_seconds()
        await sleep(wait)
        try:
            await start()
        except Exception:
            # Including a refusal because a chain is already running: the owner
            # pressed the button five minutes ago, which is not an error and is
            # certainly not a reason to stop scheduling.
            logger.warning("autopilot.schedule.start_failed", exc_info=True)


# ── the batch ─────────────────────────────────────────────────────────


async def plan(session: AsyncSession) -> BatchPlan:
    """Everything the "отправить все" screen shows, in one read.

    The items are the queue's own, card digest and all, so the screen shows
    exactly what the agent would be handed and a confirmation given here is a
    confirmation of that text. The set-aside list travels with them because the
    question a person asks about a short list is "what happened to the rest".
    """
    selection = await agent_queue.triage(session, limit=SELECTION_LIMIT)
    last_sent = await _last_sent_at(session)
    limit, reason, first = _ceiling(last_sent)
    return BatchPlan(
        items=[
            BatchItem(
                vacancy_id=ready.vacancy_id,
                item=ready.item,
                card_digest=agent_queue.card_digest(ready.item),
                confirmed=ready.item.confirmation is not None,
                confirmed_at=(
                    ready.item.confirmation.confirmed_at
                    if ready.item.confirmation is not None
                    else None
                ),
            )
            for ready in selection.ready
        ],
        set_aside=selection.set_aside,
        limit=limit,
        limit_reason=reason,
        last_sent_at=last_sent,
        first_batch=first,
    )


async def confirm(session: AsyncSession, request: BatchConfirmRequest) -> BatchConfirmResult:
    """Record one "yes" per ticked row, each bound to its own card. Sends nothing.

    Every row goes through ``app.services.confirmations.confirm``, the same
    function the single-vacancy modal calls, so a batch cannot confirm anything
    the one-at-a-time flow would refuse: a card that changed while the screen was
    open, a vacancy that has since been applied to, a letter that was
    regenerated. A refusal is reported per row and the rest of the batch stands.

    The ceiling is checked before any of them is written, not while writing:
    half a batch confirmed and half refused for hitting a limit would be the
    worst of both answers.

    **It is counted over everything that would then be outstanding**, not over
    this request alone. A ceiling on one press is not a ceiling at all — three
    presses of three confirmations would hand the agent nine, which is the
    number the limit exists to prevent — so the confirmations already standing
    for other vacancies count towards it.
    """
    last_sent = await _last_sent_at(session)
    limit, reason, _ = _ceiling(last_sent)
    asked = {row.vacancy_id for row in request.items}
    standing = {
        ready.vacancy_id
        for ready in (await agent_queue.triage(session, limit=SELECTION_LIMIT)).ready
        if ready.item.confirmation is not None and ready.vacancy_id not in asked
    }
    if len(asked) + len(standing) > limit:
        already = (
            f" Уже подтверждено и не отмечено сейчас: {len(standing)} — их можно отозвать в"
            " карточке вакансии."
            if standing
            else ""
        )
        raise BatchRefusedError(
            f"За один раз можно подтвердить не больше {limit}. {reason} "
            f"Вы отметили {len(asked)}.{already}"
        )

    outcomes: list[BatchConfirmOutcome] = []
    confirmed = 0
    for row in request.items:
        try:
            await confirmations.confirm(session, row.vacancy_id, row.card_digest)
        except AppError as error:
            outcomes.append(
                BatchConfirmOutcome(vacancy_id=row.vacancy_id, confirmed=False, detail=error.detail)
            )
            continue
        confirmed += 1
        outcomes.append(BatchConfirmOutcome(vacancy_id=row.vacancy_id, confirmed=True))
    logger.info(
        "autopilot.batch.confirmed",
        asked=len(request.items),
        confirmed=confirmed,
        limit=limit,
    )
    return BatchConfirmResult(
        confirmed=confirmed, outcomes=outcomes, limit=limit, limit_reason=reason
    )


async def _last_sent_at(session: AsyncSession) -> datetime | None:
    """When an application last actually left, as the tracker recorded it."""
    found = await session.scalar(select(func.max(Application.sent_at)))
    return found if isinstance(found, datetime) else None


def _ceiling(last_sent: datetime | None) -> tuple[int, str, bool]:
    """How many one confirmation may cover, why, and whether it is a first batch.

    Two rules, and the smaller one wins. The ordinary ceiling is
    ``AGENT_BATCH_LIMIT``. After ``AGENT_QUIET_PERIOD_DAYS`` without a single
    application — or before the first one ever — it drops to
    ``AGENT_FIRST_BATCH_LIMIT``, because a mistake in the selection is
    discovered by an employer reading it, and the first batch after a break is
    the one that must not reach twenty of them at once.
    """
    ordinary = settings.agent_batch_limit
    quiet = timedelta(days=settings.agent_quiet_period_days)
    resting = last_sent is None or datetime.now(UTC) - last_sent > quiet
    if not resting:
        return ordinary, f"Обычный размер пачки — {ordinary} (AGENT_BATCH_LIMIT).", False
    first = min(settings.agent_first_batch_limit, ordinary)
    since = (
        "отклики ещё не отправлялись"
        if last_sent is None
        else f"последний отклик ушёл {last_sent:%d.%m.%Y}"
    )
    return (
        first,
        f"Первая пачка после перерыва — не больше {first} ({since}), "
        "чтобы ошибка в отборе не разошлась сразу по всем работодателям.",
        True,
    )


_KIND_TITLES: Final[dict[SetAsideKind, str]] = {
    SetAsideKind.NO_LETTER: "письма ещё нет",
    SetAsideKind.ARCHIVED: "в архиве",
    SetAsideKind.CLOSED: "закрыта для откликов",
    SetAsideKind.EXPERIENCE_GAP: "просят больше опыта",
    SetAsideKind.LANGUAGE: "язык выше вашего уровня",
    SetAsideKind.STALE_PAGE: "страницу давно не перечитывали",
    SetAsideKind.LETTER_RULES: "письмо нарушает правила мастерской",
    SetAsideKind.LETTER_AUDIT: "письмо не прошло ATS-аудит",
    SetAsideKind.UNSERVABLE: "ссылка не совпадает с номером вакансии",
}

_SKIPPED: Final[dict[str, str]] = {
    "vacancy_not_found": "вакансия не найдена",
    "letter_exists": "письмо уже есть",
    "dry_run": "пробный прогон",
    "letter_unwritable": "не удалось написать письмо, которое проходит проверки",
    "letter_failed_audit": "письмо не прошло ATS-аудит и не сохранено",
}
