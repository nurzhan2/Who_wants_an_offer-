"""Re-reading the pages of what is about to be applied to.

The queue's other checks read the database: the score, the letter, the tracker's
record of what has been acted on. This one reads the page — and it exists
because the database is a memory of a crawl and an application is sent today.

The measurement behind it: on 16 Sep 2026 a third of the first real apply queue
turned out to be archived by the time the agent opened each vacancy, one of them
(136105998) sitting in ``apply_now`` with a letter already written for it; and on
17 Sep every hh row in the corpus carried ``last_seen_at`` of 8 Sep, nine days
earlier, while the dashboard cheerfully offered them. So
``app.services.agent_queue`` refuses to serve anything whose page has not been
read within ``AGENT_PAGE_FRESHNESS_HOURS``, and this module is what reading it
means.

**It updates three things and nothing else.** ``vacancy.is_active``, the
posting's ``closed_for_applicants`` inside the connector's derived block, and
``vacancy.last_seen_at`` — the timestamp the freshness rule is written against.
It deliberately does not re-import the posting: a check run to decide whether to
apply must not be able to rewrite the description the embeddings were computed
from, or the title on a card somebody has already read.

**A source it cannot ask is not a source that said yes.** The capability is
optional on ``BaseSource`` (:meth:`~app.sources.base.BaseSource.recheck`), and a
connector that does not implement it — or one hh has just challenged — leaves
the row as stale as it found it. The vacancy then stays in "посмотреть руками"
with the reason that nobody could read its page, which is the honest outcome and
the one the whole autopilot is built to prefer.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any, final
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.core.exceptions import SourceError
from app.core.logging import get_logger
from app.db.models import Vacancy, VacancySource
from app.sources.base import BaseSource, PostingState
from app.sources.http import HHChallengedError
from app.sources.registry import bind_sources, get_source

logger = get_logger(__name__)

#: The key the connectors keep their derived block under. Same constant
#: ``app.services.agent_queue`` reads; spelled here rather than imported so that
#: this module does not depend on the queue it feeds.
DERIVED_KEY = "_derived"


@final
@dataclass(slots=True)
class RefreshOutcome:
    """What one pass of re-reading did, in numbers a report can print."""

    #: Pages actually requested.
    checked: int = 0
    #: Still open for applications after the read.
    open_for_applications: int = 0
    #: Found archived, closed or gone. Each one is a vacancy the queue would
    #: have offered on the strength of a nine-day-old belief.
    closed: int = 0
    #: Rows whose source cannot be re-read at all.
    unsupported: int = 0
    #: Pages that could not be read this time: a challenge, a timeout, markup
    #: that moved. Counted rather than absorbed — see :attr:`stopped`.
    unreadable: int = 0
    #: Set when the source stopped the pass altogether (hh showing a robot
    #: check). Everything after it is untouched and says so.
    stopped: str | None = None
    #: One line per vacancy whose state changed, for the operation's report.
    notes: list[str] = field(default_factory=list)


async def refresh(
    session: AsyncSession,
    vacancy_ids: Sequence[UUID],
    *,
    source_slug: str | None = None,
    source: BaseSource | None = None,
) -> RefreshOutcome:
    """Re-read each of these vacancies' pages and write down what they say now.

    ``source`` is injected by tests; production passes nothing and the registry
    binds the shared client, the same one the crawl uses — which is also what
    keeps this pass inside hh's rate limit rather than beside it.

    The pass stops at a challenge. hh has decided something about this crawler,
    and walking the remaining nineteen pages against that decision would be both
    useless and the rudest possible answer; what is already written stays
    written, and :attr:`RefreshOutcome.stopped` says why the rest is untouched.
    """
    slug = source_slug or settings.agent_source_slug
    outcome = RefreshOutcome()
    if not vacancy_ids:
        return outcome

    rows = (
        await session.execute(
            select(VacancySource.vacancy_id, VacancySource.external_id, VacancySource.url)
            .where(VacancySource.vacancy_id.in_(vacancy_ids))
            .where(VacancySource.source_slug == slug)
            .order_by(VacancySource.created_at, VacancySource.id)
        )
    ).all()
    if not rows:
        return outcome

    connector = source if source is not None else _bound(slug)
    for row in rows:
        try:
            state = await connector.recheck(str(row.url), str(row.external_id))
        except HHChallengedError as error:
            outcome.stopped = (
                f"{slug} показал проверку на робота — перечитывание остановлено, "
                "остальные страницы остались непроверенными"
            )
            logger.warning("freshness.challenged", source=slug, error=str(error))
            break
        except SourceError as error:
            outcome.unreadable += 1
            logger.warning(
                "freshness.unreadable",
                source=slug,
                external_id=str(row.external_id),
                error=str(error),
            )
            continue
        if state is None:
            outcome.unsupported += 1
            continue
        outcome.checked += 1
        await _write(session, row.vacancy_id, state)
        if state.open_for_applications:
            outcome.open_for_applications += 1
        else:
            outcome.closed += 1
            outcome.notes.append(f"{row.external_id}: {_why_closed(state)}")
    await session.commit()
    logger.info(
        "freshness.pass",
        source=slug,
        checked=outcome.checked,
        closed=outcome.closed,
        unreadable=outcome.unreadable,
    )
    return outcome


def _why_closed(state: PostingState) -> str:
    """The sentence for a posting that will not take an application any more."""
    if state.gone:
        return "страницы больше нет на hh"
    if state.archived:
        return "вакансия в архиве"
    return "hh закрыл вакансию для откликов"


def _bound(slug: str) -> BaseSource:
    """The connector with the shared HTTP client attached.

    Through the registry, never by importing a connector: the services hold the
    registry and only the registry (CLAUDE.md rule 5), so teaching a second site
    to be re-read is a change inside ``app/sources/``.
    """
    return bind_sources([get_source(slug)])[0]


async def _write(session: AsyncSession, vacancy_id: UUID, state: PostingState) -> None:
    """Record one page's answer: the two flags, and when it was read.

    ``last_seen_at`` moves only when the page was actually served and still
    holds this posting. A 404 is not a sighting, and moving the timestamp for
    one would mark a vacancy "checked just now, looks fine" on the strength of
    its absence.
    """
    values: dict[str, Any] = {"is_active": not (state.gone or state.archived)}
    if not state.gone:
        values["last_seen_at"] = state.checked_at
    await session.execute(update(Vacancy).where(Vacancy.id == vacancy_id).values(**values))

    row = (
        await session.execute(
            select(VacancySource)
            .where(VacancySource.vacancy_id == vacancy_id)
            .where(VacancySource.source_slug == state.source_slug)
            .where(VacancySource.external_id == state.external_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:  # pragma: no cover - the row was selected a moment ago
        return
    raw: dict[str, Any] = dict(row.raw) if isinstance(row.raw, dict) else {}
    block = raw.get(DERIVED_KEY)
    derived: dict[str, Any] = dict(block) if isinstance(block, dict) else {}
    derived["closed_for_applicants"] = state.closed_for_applicants
    # The moment the page was read, kept beside the flag it explains. The
    # crawl's own ``_derived`` block has no such key, so a row that has never
    # been re-read is visibly different from one that has.
    derived["rechecked_at"] = state.checked_at.astimezone(UTC).isoformat()
    raw[DERIVED_KEY] = derived
    row.raw = raw
    # JSONB reassigned wholesale still needs the attribute marked dirty when the
    # dict identity is the one SQLAlchemy already holds; cheap, and the
    # alternative is a write that silently does nothing.
    flag_modified(row, "raw")
