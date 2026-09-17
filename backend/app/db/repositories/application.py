"""Tracker reads: the applications board, its counters and its letters.

Reads only. Nothing here writes, and that is the whole point of the module
existing beside ``app/services/agent_queue.py`` rather than inside it: the queue
service writes what the agent reports, this answers what a person looking at a
screen asks, and the two must not be one object with a send in it. The dashboard
cannot send an application — see ``app/api/v1/tracker.py`` — and the shortest way
to keep that true is to give it a reader that has no writes to call.

Every query joins ``vacancy`` explicitly. ``Application.vacancy`` is an ordinary
relationship on a model whose collections are ``lazy="raise"``, and an async
session would raise on a lazy load anyway; saying what the screen needs is
cheaper than discovering that one row at a time.

The tracker is one person's, so these queries are unbounded by design — tens of
rows, not millions, and the module docstring of ``Application`` says why no
index is warranted either. The one bound is :data:`MAX_ROWS`, which exists to
stop a runaway rather than to page.
"""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, nulls_last, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import ScalarSelect

from app.db.enums import ApplicationStatus
from app.db.models import Application, Match, Vacancy, VacancySource
from app.schemas.dashboard import ApplicationCounts, BoardCard, LetterBrief, LetterDocument

#: Ceiling on any one of these reads. Far above the real number of rows on this
#: account, and low enough that a bug cannot render a hundred thousand letters
#: into a browser.
MAX_ROWS = 2_000


def _url_of_vacancy() -> ScalarSelect[str | None]:
    """The address the crawler actually read, for one vacancy.

    Correlated rather than joined, because a vacancy carries one row per posting
    it was deduplicated from and joining would multiply the tracker rows by
    that. ``min`` picks deterministically; every URL in the group points at the
    same job, and rebuilding one from an id would not, since hh's addresses are
    on regional subdomains.
    """
    return (
        select(func.min(VacancySource.url))
        .where(VacancySource.vacancy_id == Vacancy.id)
        .correlate(Vacancy)
        .scalar_subquery()
    )


def _queued() -> ColumnElement[bool]:
    """A letter waiting for the owner: the agent queued it, or nobody touched it yet.

    ``app.services.tracker._stage_of`` in SQL. Letters written by the generators
    carry no ``agent_status``, and counting only the agent's own word left this
    number at zero with a queue full of letters.
    """
    untouched = and_(
        Application.agent_status.is_(None),
        Application.cover_letter.is_not(None),
        Application.sent_at.is_(None),
        Application.status == ApplicationStatus.SAVED,
        Application.applied_at.is_(None),
    )
    return or_(Application.agent_status == "queued", untouched)


def _send_confirmed() -> ColumnElement[bool]:
    """A send hh itself has confirmed. ``app.schemas.dashboard.send_confirmed`` in SQL."""
    return and_(
        Application.sent_at.is_not(None),
        or_(
            func.coalesce(Application.hh_negotiations_total, 0) >= 1,
            func.coalesce(func.trim(Application.hh_last_state), "") != "",
        ),
    )


class ApplicationRepository:
    """Everything the dashboard reads out of the tracker."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def counts(self) -> ApplicationCounts:
        """The five numbers on the overview screen, in one statement.

        ``sent`` is ``sent_at IS NOT NULL`` and nothing else. A row a person
        dragged across their own kanban is not evidence that an application went
        out — only the process that did the typing writes ``sent_at`` — and
        widening this would make the one countable fact uncountable.

        ``queued`` and ``needs_manual`` read ``agent_status``, which is the
        agent's own state machine and the only thing that knows a row is waiting
        on a person rather than on a run.
        """
        stmt = select(
            func.count().filter(Application.sent_at.is_not(None)).label("sent"),
            func.count().filter(_send_confirmed()).label("sent_confirmed"),
            func.count().filter(_queued()).label("queued"),
            func.count().filter(Application.agent_status == "needs_manual").label("needs_manual"),
            func.count().filter(Application.cover_letter.is_not(None)).label("with_letter"),
            func.count().filter(Application.hh_last_state.is_not(None)).label("answered"),
        ).select_from(Application)
        row = (await self.session.execute(stmt)).one()
        return ApplicationCounts(
            sent=row.sent,
            sent_confirmed=row.sent_confirmed,
            queued=row.queued,
            needs_manual=row.needs_manual,
            with_letter=row.with_letter,
            answered=row.answered,
        )

    async def board(self, *, limit: int = MAX_ROWS) -> list[BoardCard]:
        """Every tracker row with what the send recorded, newest activity first.

        Sorted by when something last happened to the row rather than when it
        was created: a letter written this morning for a vacancy crawled last
        week belongs at the top of the screen, and ``created_at`` would bury it.
        """
        last_activity = func.greatest(
            func.coalesce(Application.sent_at, Application.created_at),
            func.coalesce(Application.applied_at, Application.created_at),
            Application.updated_at,
        )
        stmt = (
            select(
                Application.id,
                Application.vacancy_id,
                Vacancy.title,
                Vacancy.company,
                _url_of_vacancy().label("url"),
                Application.status,
                Application.agent_status,
                Application.agent_reason,
                Application.applied_at,
                Application.sent_at,
                Application.sent_letter,
                Application.cover_letter,
                Application.match_score,
                Application.match_bucket,
                Application.vacancy_key_skills,
                Application.hh_warning,
                Application.hh_blocking_warning,
                Application.hh_negotiations_total,
                Application.hh_last_state,
                Application.hh_last_state_at,
                _send_confirmed().label("send_confirmed"),
                Application.confirmed_at,
                Vacancy.published_at.label("vacancy_published_at"),
                Vacancy.last_seen_at.label("vacancy_last_seen_at"),
                Vacancy.is_active.label("vacancy_active"),
            )
            .join(Vacancy, Vacancy.id == Application.vacancy_id)
            .order_by(last_activity.desc(), Application.id)
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [BoardCard.model_validate(row, from_attributes=True) for row in rows]

    async def letters(self, *, limit: int = MAX_ROWS) -> list[LetterDocument]:
        """Every row that holds a letter, newest first.

        ``match_score`` comes from the snapshot the send took when there is one,
        and from the live ``match`` row when there is not. The two are different
        facts — one is what we believed when we sent, the other what we believe
        now — and the snapshot wins because for a sent letter that is the number
        the outcome should be read against. Falling back is what gives an unsent
        letter a score at all.
        """
        live = (
            select(Match.score)
            .where(Match.vacancy_id == Application.vacancy_id)
            .where(Match.profile_id == Application.profile_id)
            .correlate(Application)
            .limit(1)
            .scalar_subquery()
        )
        stmt = (
            select(
                Application.id.label("application_id"),
                Application.vacancy_id,
                Vacancy.title,
                Vacancy.company,
                _url_of_vacancy().label("url"),
                Application.cover_letter.label("text"),
                func.length(Application.cover_letter).label("characters"),
                Application.letter_rules_version,
                Application.updated_at.label("written_at"),
                Application.sent_at,
                Application.sent_letter,
                Application.hh_last_state.label("outcome"),
                Application.hh_last_state_at.label("outcome_at"),
                func.coalesce(Application.match_score, live).label("match_score"),
            )
            .join(Vacancy, Vacancy.id == Application.vacancy_id)
            .where(Application.cover_letter.is_not(None))
            .order_by(nulls_last(Application.sent_at.desc()), Application.updated_at.desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            LetterDocument(
                application_id=row.application_id,
                vacancy_id=row.vacancy_id,
                title=row.title,
                company=row.company,
                url=row.url,
                text=row.text or "",
                characters=row.characters or 0,
                rules_version=row.letter_rules_version,
                # ``problems`` is left empty here on purpose: what today's rules
                # make of this text is a judgement, and a repository has none.
                # ``app/services/documents.py`` fills it in.
                written_at=row.written_at,
                sent_at=row.sent_at,
                sent_letter=row.sent_letter,
                outcome=row.outcome,
                outcome_at=row.outcome_at,
                match_score=_decimal(row.match_score),
            )
            for row in rows
        ]

    async def letter_brief(self, vacancy_id: UUID) -> LetterBrief | None:
        """Whether this vacancy has a letter, and what became of it.

        The oldest row for the vacancy, which is the one ``letters/store.py``
        writes to. A vacancy may legitimately carry two tracker rows — two
        attempts at the same job — and picking a different one here would show a
        card the letter screen does not.
        """
        stmt = (
            select(
                Application.id.label("application_id"),
                func.coalesce(func.length(Application.cover_letter), 0).label("characters"),
                Application.sent_at,
                Application.agent_status,
                Application.hh_last_state.label("outcome"),
            )
            .where(Application.vacancy_id == vacancy_id)
            .order_by(Application.created_at, Application.id)
            .limit(1)
        )
        row = (await self.session.execute(stmt)).one_or_none()
        if row is None:
            return None
        return LetterBrief.model_validate(row, from_attributes=True)


def _decimal(value: object) -> Decimal | None:
    """A score column that may have come from either of two branches."""
    return value if isinstance(value, Decimal) else None
