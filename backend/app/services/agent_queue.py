"""What the local apply agent is handed, and what comes back from it.

All the logic for ``app/api/v1/applications.py`` lives here; the router
validates, calls one of these two functions and answers (CLAUDE.md rule 2).

**Nothing in this module imports ``agent``, and nothing in ``agent`` imports
this.** The two packages meet over HTTP and nowhere else: ``backend/`` is
anonymous, read-only and can run on a server, while ``agent/`` drives a browser
under the owner's own login and sends things. ``agent/tests/test_isolation.py``
enforces that by parsing the import graph.

**Why the queue writes nothing.**

An ``application`` row means "the candidate acted on this vacancy". Creating one
when a vacancy is *offered* would fill the tracker with jobs nobody looked at:
the agent shows each item to a person, and a person says no to most of them. It
would also make ``GET /queue`` a write, which is the one thing a request the
agent retries must not be.

The row that matters already exists by the time an item can be served at all.
``app/letters/store.save_letter`` creates it when the cover letter is written,
and an item without a letter is one the agent refuses to send — so the ordinary
lifecycle is: letters create the row, the queue reads it, the result updates it.
:func:`record_results` creates a row only when none exists, which is the moment
something actually happened in the world (an application the owner sent by hand,
a letter stored some other way). Creation at the point of the event, not at the
point of the offer.

**Idempotency.** ``application`` has no unique constraint on ``vacancy_id`` —
deliberately, since a person may track two attempts at the same job — so
"upsert" here means *update the oldest row for this vacancy*, which is the rule
``app/letters/store.py`` already follows for the same reason. A transaction-
scoped advisory lock closes the read-then-insert window, so two results posted
at the same instant cannot both decide the row is missing.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import uuid7
from app.db.enums import ApplicationStatus, MatchBucket
from app.db.models import Application, CandidateProfile, Match, Vacancy, VacancySource
from app.schemas.agent import (
    CONTRACT_VERSION,
    AgentStatus,
    ApplicationResult,
    MatchExplanation,
    QueueItem,
    QueueResponse,
    ResultAck,
    ResultsResponse,
)
from app.schemas.match import MatchComponentScores, MatchedSkill, MissingSkill

logger = get_logger(__name__)

#: Where the connector keeps everything it worked out about a posting. Same key
#: ``app/letters/store.py`` reads; see ``app/sources/hh.py`` for what is in it.
DERIVED_KEY = "_derived"

#: The block :func:`record_results` owns inside ``application.notes``. Anything
#: the owner wrote above it is left alone; everything from this line down is
#: rewritten on every result, so posting the same result twice leaves the same
#: text rather than appending a second copy.
NOTES_MARKER = "--- агент ---"

#: Ceiling on the rendered explanation, and on how many names it lists. The
#: card the agent prints is a terminal, not a page.
MAX_EXPLANATION = 400
MAX_LISTED = 6

#: How many rows to read before de-duplication and validation trim the answer.
#: A vacancy can carry more than one posting from the same source (two hh ads
#: deduplicated onto one fingerprint), and an item whose stored URL disagrees
#: with its id is dropped rather than served.
OVERFETCH = 2


async def build_queue(
    session: AsyncSession,
    *,
    limit: int,
    profile_id: UUID | None = None,
    min_score: Decimal | None = None,
    require_letter: bool = True,
) -> QueueResponse:
    """Vacancies worth an application, best score first.

    Every item carries the URL the crawler actually read. It is never rebuilt
    from the id: for hh that address is a regional subdomain, because the
    connector walks ``almaty.hh.kz``'s own sitemap, and it is both the page the
    agent opens and the link the human reads on the confirmation card. An item
    whose stored URL does not name its own id is dropped with a warning instead
    of served — the agent rejects the whole batch on that mismatch
    (``agent/queue.py``), so one bad row must not cost the run.
    """
    profile = profile_id if profile_id is not None else await active_profile_id(session)
    if profile is None:
        logger.info("agent.queue.no_profile")
        return QueueResponse(version=CONTRACT_VERSION, items=[])

    threshold = min_score if min_score is not None else Decimal(settings.agent_queue_min_score)
    rows = (
        await session.execute(
            _queue_statement(
                profile_id=profile,
                min_score=threshold,
                require_letter=require_letter,
                limit=limit * OVERFETCH + OVERFETCH,
            )
        )
    ).all()

    items: list[QueueItem] = []
    seen: set[UUID] = set()
    for row in rows:
        if len(items) >= limit:
            break
        if row.vacancy_id in seen:
            continue
        item = _to_item(row)
        if item is None:
            continue
        seen.add(row.vacancy_id)
        items.append(item)

    logger.info(
        "agent.queue.served",
        profile_id=str(profile),
        count=len(items),
        source=settings.agent_source_slug,
    )
    return QueueResponse(version=CONTRACT_VERSION, items=items)


async def record_results(
    session: AsyncSession, results: list[ApplicationResult]
) -> ResultsResponse:
    """Write what the agent saw back onto the tracker rows.

    Only ``sent`` moves an application forward, and only ever to ``applied``:
    a row a person has already dragged to ``interview`` is not demoted because
    a later run reported a failure against the same vacancy. Everything else
    the agent saw — its own status, both of hh's warnings, the negotiation
    count, the last state — is recorded in the notes block; see
    :data:`NOTES_MARKER` and the module docstring's note about where that
    really belongs.
    """
    acks: list[ResultAck] = []
    unknown: list[str] = []
    accepted = 0

    for result in results:
        vacancy_id = await _resolve(session, result.vacancy_id)
        if vacancy_id is None:
            unknown.append(result.vacancy_id)
            acks.append(
                ResultAck(
                    vacancy_id=result.vacancy_id,
                    accepted=False,
                    detail=(
                        f"Источник {settings.agent_source_slug} не знает вакансию "
                        f"{result.vacancy_id}; запись в трекере не создана."
                    ),
                )
            )
            logger.warning(
                "agent.results.unknown_vacancy",
                external_id=result.vacancy_id,
                source=settings.agent_source_slug,
            )
            continue

        application, created = await _upsert(session, vacancy_id, result)
        accepted += 1
        acks.append(
            ResultAck(
                vacancy_id=result.vacancy_id,
                accepted=True,
                application_id=application.id,
                created=created,
            )
        )
        logger.info(
            "agent.results.recorded",
            external_id=result.vacancy_id,
            application_id=str(application.id),
            created=created,
            status=result.status.value,
            # hh's sentences are not logged: they quote a page about this
            # person's own application and belong in the row, not in a log file.
            has_blocking_warning=result.hh_blocking_warning is not None,
            has_soft_warning=result.hh_warning is not None,
        )

    await session.commit()
    return ResultsResponse(
        version=CONTRACT_VERSION, accepted=accepted, unknown=unknown, results=acks
    )


async def active_profile_id(session: AsyncSession) -> UUID | None:
    """The profile the dashboard is currently working from.

    Same rule as ``app/letters/store.load_profile_facts``: the newest active
    one. Duplicated rather than imported because that module's queries are
    about writing letters and this one is not.
    """
    found = await session.scalar(
        select(CandidateProfile.id)
        .where(CandidateProfile.is_active.is_(True))
        .order_by(CandidateProfile.created_at.desc())
        .limit(1)
    )
    # Checked rather than cast: SQLAlchemy types a scalar select of a UUID
    # column as Any, and an isinstance costs less than a suppression comment.
    return found if isinstance(found, UUID) else None


# ── the queue ─────────────────────────────────────────────────────────


def _queue_statement(
    *, profile_id: UUID, min_score: Decimal, require_letter: bool, limit: int
) -> Select[Any]:
    """One statement, explicit about its columns.

    ``Vacancy.matches``, ``Vacancy.applications`` and ``Match.vacancy`` are all
    ``lazy="raise"`` — the models turn an accidental N+1 into an error — so
    everything this needs is named here.
    """
    letter = (
        select(Application.cover_letter)
        .where(Application.vacancy_id == Vacancy.id)
        .where(Application.cover_letter.is_not(None))
        .order_by(Application.created_at, Application.id)
        .limit(1)
        .scalar_subquery()
    )
    # Anything past "saved" means a person or a previous run already acted on
    # this vacancy. Offering it again is how a second application gets sent.
    acted_on = (
        select(Application.id)
        .where(Application.vacancy_id == Vacancy.id)
        .where(
            (Application.status != ApplicationStatus.SAVED) | (Application.applied_at.is_not(None))
        )
        .exists()
    )

    statement = (
        select(
            Vacancy.id.label("vacancy_id"),
            Vacancy.title,
            Vacancy.company,
            Vacancy.is_active,
            VacancySource.external_id,
            VacancySource.url,
            VacancySource.raw,
            Match.score,
            Match.bucket,
            Match.verdict,
            Match.application_angle,
            Match.component_scores,
            Match.matched_skills,
            Match.missing_required,
            Match.missing_nice,
            Match.red_flags,
            Match.experience_gap_years,
            letter.label("letter"),
        )
        .select_from(Match)
        .join(Vacancy, Vacancy.id == Match.vacancy_id)
        .join(
            VacancySource,
            (VacancySource.vacancy_id == Vacancy.id)
            & (VacancySource.source_slug == settings.agent_source_slug),
        )
        .where(Match.profile_id == profile_id)
        .where(Match.score >= min_score)
        .where(Vacancy.is_spam.is_(False))
        .where(~acted_on)
        .order_by(Match.score.desc(), Vacancy.id, VacancySource.external_id)
        .limit(limit)
    )
    if require_letter:
        statement = statement.where(letter.is_not(None))
    return statement


def _to_item(row: Any) -> QueueItem | None:
    """One row as the agent will read it, or None when it must not be served.

    ``Any`` for the row: SQLAlchemy's ``Row`` is not usefully typed for a
    select of eighteen labelled columns, and every field is validated into a
    Pydantic model immediately below — which is the boundary CLAUDE.md rule 3's
    exception is for.
    """
    external_id = str(row.external_id)
    url = str(row.url)
    if not _url_names(url, external_id):
        # The agent compares the two and rejects the entire batch when they
        # disagree, so serving this item would cost every item behind it.
        logger.warning(
            "agent.queue.url_id_mismatch",
            external_id=external_id,
            url=url,
            source=settings.agent_source_slug,
        )
        return None

    derived = _derived(row.raw)
    explanation = _explanation(row)
    return QueueItem(
        vacancy_id=external_id,
        url=url,
        title=row.title,
        company=row.company,
        letter=row.letter,
        closed_for_applicants=bool(derived.get("closed_for_applicants", False)),
        # The crawler marks a posting inactive when it stops answering, which is
        # what "archived" means to the agent. hh's own archive flag is read on
        # the page and never reaches _derived, so this is the honest source.
        archived=not bool(row.is_active),
        # Nothing this project stores says an application happens on the
        # employer's own site: hh's page does, and the agent reads it there.
        # Served as False rather than guessed, so the flag never lies.
        external_application=False,
        score=explanation.score,
        score_explanation=render_explanation(explanation),
        source=settings.agent_source_slug,
        match=explanation,
        anonymous=bool(derived.get("anonymous", False)),
        employer_on_additional_check=bool(derived.get("employer_on_additional_check", False)),
    )


def _explanation(row: Any) -> MatchExplanation:
    """The score together with the reason for it."""
    components = row.component_scores if isinstance(row.component_scores, dict) else {}
    return MatchExplanation(
        score=row.score,
        bucket=MatchBucket(row.bucket),
        verdict=row.verdict,
        application_angle=row.application_angle,
        components=MatchComponentScores.model_validate(components),
        matched_skills=_models(MatchedSkill, row.matched_skills),
        missing_required=_models(MissingSkill, row.missing_required),
        missing_nice=_models(MissingSkill, row.missing_nice),
        red_flags=[str(flag) for flag in _iterable(row.red_flags)],
        experience_gap_years=row.experience_gap_years,
    )


def render_explanation(explanation: MatchExplanation) -> str | None:
    """The reason for the score, as one line a person can read on the card.

    Russian, because it is shown to the owner; the structure it was built from
    travels alongside it in :attr:`QueueItem.match` for anything that would
    rather render than print.

    ``None`` when there is genuinely nothing to say. A sentence invented from an
    empty match would read as an explanation and be one, and the card the agent
    prints is the last thing between a generated letter and a real application.
    """
    parts: list[str] = []
    if explanation.verdict and explanation.verdict.strip():
        parts.append(explanation.verdict.strip())
    if explanation.matched_skills:
        parts.append(
            f"совпадает: {_names(skill.canonical_name for skill in explanation.matched_skills)}"
        )
    if explanation.missing_required:
        parts.append(
            f"не хватает обязательного: "
            f"{_names(skill.canonical_name for skill in explanation.missing_required)}"
        )
    if explanation.missing_nice:
        parts.append(
            f"не хватает желательного: "
            f"{_names(skill.canonical_name for skill in explanation.missing_nice)}"
        )
    if explanation.red_flags:
        parts.append(f"тревожные признаки: {_names(iter(explanation.red_flags))}")
    if explanation.experience_gap_years and explanation.experience_gap_years > 0:
        parts.append(f"разрыв по опыту, лет: {explanation.experience_gap_years:g}")
    if not parts:
        return None
    return "; ".join(parts)[:MAX_EXPLANATION]


def _names(values: Iterable[str]) -> str:
    """A short, comma-separated list. Long enough to be useful, short enough to
    stay on one line of a terminal card."""
    listed = [value.strip() for value in values if value.strip()]
    head = ", ".join(listed[:MAX_LISTED])
    return f"{head} и ещё {len(listed) - MAX_LISTED}" if len(listed) > MAX_LISTED else head


def _models[M: BaseModel](model: type[M], stored: object) -> list[M]:
    """Validate a JSONB list into models, dropping entries that do not fit.

    The annotation on a JSONB column is a promise the database does not keep,
    and these lists were last written by the scorer. One malformed entry from
    an older scoring run must not empty a queue.
    """
    built: list[M] = []
    for entry in _iterable(stored):
        if not isinstance(entry, dict):
            continue
        try:
            built.append(model.model_validate(entry))
        except ValueError:
            logger.warning("agent.queue.unreadable_match_detail", model=model.__name__)
    return built


def _iterable(stored: object) -> list[Any]:
    """A JSONB list, or nothing at all."""
    return list(stored) if isinstance(stored, list) else []


def _derived(raw: object) -> dict[str, Any]:
    """The connector's derived block out of a source payload."""
    if not isinstance(raw, dict):
        return {}
    block = raw.get(DERIVED_KEY)
    return block if isinstance(block, dict) else {}


def _url_names(url: str, external_id: str) -> bool:
    """Whether this https URL's path ends in exactly this posting's id.

    The check the agent performs, performed here first. Path only: a login
    redirect carries the address it interrupted in a query parameter, so an id
    found anywhere in the string proves nothing about where the URL leads.
    """
    if not url.startswith("https://"):
        return False
    parts = urlsplit(url)
    if not parts.hostname:
        return False
    tail = parts.path.rstrip("/").rsplit("/", 1)[-1]
    return tail == external_id


# ── the results ───────────────────────────────────────────────────────


async def _resolve(session: AsyncSession, external_id: str) -> UUID | None:
    """Our own vacancy id for a posting the agent named, or None."""
    found = await session.scalar(
        select(VacancySource.vacancy_id)
        .where(VacancySource.source_slug == settings.agent_source_slug)
        .where(VacancySource.external_id == external_id)
        .limit(1)
    )
    return found if isinstance(found, UUID) else None


async def _upsert(
    session: AsyncSession, vacancy_id: UUID, result: ApplicationResult
) -> tuple[Application, bool]:
    """Update this vacancy's tracker row, or create the first one."""
    await _lock(session, vacancy_id)
    application = await session.scalar(
        select(Application)
        .where(Application.vacancy_id == vacancy_id)
        .order_by(Application.created_at, Application.id)
        .limit(1)
    )
    created = application is None
    if application is None:
        application = Application(id=uuid7(), vacancy_id=vacancy_id)
        session.add(application)

    if result.status is AgentStatus.SENT:
        # The only forward move this endpoint makes, and only forward: a row
        # somebody already dragged to "interview" stays there.
        if application.status is ApplicationStatus.SAVED:
            application.status = ApplicationStatus.APPLIED
        if application.applied_at is None:
            # A real instant rather than func.now(): the attribute is read back
            # in the same transaction, and an unevaluated SQL expression sitting
            # on a mapped attribute is a value nothing downstream can compare.
            application.applied_at = datetime.now(UTC)

    application.notes = merge_notes(application.notes, render_outcome(result))
    await session.flush()
    return application, created


async def _lock(session: AsyncSession, vacancy_id: UUID) -> None:
    """Serialise results for one vacancy for the rest of this transaction.

    Without it two results posted at the same instant both read "no row" and
    both insert, which is the one way this endpoint could produce a duplicate.
    The lock is transaction-scoped, so it is released by the commit and there
    is nothing to unlock by hand.
    """
    high = ((vacancy_id.int >> 32) & 0xFFFFFFFF) - 0x8000_0000
    low = (vacancy_id.int & 0xFFFFFFFF) - 0x8000_0000
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:high, :low)"), {"high": high, "low": low}
    )


def render_outcome(result: ApplicationResult) -> str:
    """The agent's report as labelled lines, in the language the UI speaks.

    Prose because there is nowhere better today: ``application`` holds a status,
    a timestamp, a note and a letter, and hh's warning is none of those. It
    wants a column of its own — hh naming one unmet requirement is a fact worth
    querying, not just reading — and adding one is a model change plus a
    migration, which this task does not own. Written as one field per line with
    fixed labels so that the day the column exists, backfilling it is a parse
    rather than an archaeology.
    """
    lines = [NOTES_MARKER, f"статус: {result.status.value}"]
    if result.reason:
        lines.append(f"причина: {result.reason}")
    if result.hh_blocking_warning:
        lines.append(f"hh, блокирующее требование: {result.hh_blocking_warning}")
    if result.hh_warning:
        lines.append(f"hh, предупреждение: {result.hh_warning}")
    if result.negotiations_total is not None:
        lines.append(f"откликов по данным hh (negotiations.total): {result.negotiations_total}")
    if result.last_state:
        lines.append(f"состояние отклика (lastState): {result.last_state}")
    return "\n".join(lines)


def merge_notes(existing: str | None, block: str) -> str:
    """Put the agent's block into the notes, replacing its previous one.

    Whatever the owner typed stays above the marker untouched; everything from
    the marker down is this function's. Replacing rather than appending is what
    makes a re-posted result idempotent in the text as well as in the row.
    """
    kept = (existing or "").split(NOTES_MARKER)[0].rstrip()
    return f"{kept}\n\n{block}" if kept else block
