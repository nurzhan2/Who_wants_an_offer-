"""Which way an application for a vacancy goes: through the agent, or by hand.

The agent applies on one source only, ``settings.agent_source_slug``. For a
vacancy listed there, a letter is the ticket into the agent queue: without one
the automatic application does not happen at all. For a vacancy listed only
elsewhere, the letter is a document the owner takes to the original page and
sends themselves. Both deserve a letter; the first deserves it first.

A vacancy is on the agent's channel when *any* of its source rows is the
agent's, not when the first one happens to be: a job cross-posted to hh and to
an aggregator is reachable by the agent, whichever row was stored first.

Everything here is a SQL expression over a vacancy id, so each caller keeps
its single query rather than paying one more per row.
"""

from enum import StrEnum
from uuid import UUID

from sqlalchemy import ColumnElement, ScalarSelect, case, select
from sqlalchemy.orm import QueryableAttribute

from app.core.config import settings
from app.db.models import VacancySource

#: A vacancy id as a query sees it: a mapped attribute or any column expression.
type VacancyIdColumn = ColumnElement[UUID] | QueryableAttribute[UUID]


class SourceScope(StrEnum):
    """Which vacancies a letter queue takes, by where they are listed."""

    #: Every source, the agent's first.
    ALL = "all"
    #: Only vacancies the agent can apply to.
    AGENT = "agent"
    #: Only vacancies the owner applies to by hand.
    OTHERS = "others"


def on_agent_source(vacancy_id: VacancyIdColumn) -> ColumnElement[bool]:
    """Whether the agent's source lists this vacancy."""
    return (
        select(VacancySource.id)
        .where(VacancySource.vacancy_id == vacancy_id)
        .where(VacancySource.source_slug == settings.agent_source_slug)
        .exists()
    )


def listed_on(vacancy_id: VacancyIdColumn, source_slug: str) -> ColumnElement[bool]:
    """Whether one named source lists this vacancy."""
    return (
        select(VacancySource.id)
        .where(VacancySource.vacancy_id == vacancy_id)
        .where(VacancySource.source_slug == source_slug)
        .exists()
    )


def primary_slug(vacancy_id: VacancyIdColumn) -> ScalarSelect[str]:
    """The source a person should act on: the agent's if it lists the vacancy."""
    return _first_source(vacancy_id, VacancySource.source_slug)


def primary_url(vacancy_id: VacancyIdColumn) -> ScalarSelect[str]:
    """The page of that same source — the original a person applies on."""
    return _first_source(vacancy_id, VacancySource.url)


def _first_source(
    vacancy_id: VacancyIdColumn, column: QueryableAttribute[str]
) -> ScalarSelect[str]:
    """One column of the vacancy's preferred source row.

    The agent's row first, then the oldest, then by id so the answer is stable
    between two reads of the same data.
    """
    agent_first = case((VacancySource.source_slug == settings.agent_source_slug, 0), else_=1)
    return (
        select(column)
        .where(VacancySource.vacancy_id == vacancy_id)
        .order_by(agent_first, VacancySource.created_at, VacancySource.id)
        .limit(1)
        .scalar_subquery()
    )


def scope_condition(
    vacancy_id: VacancyIdColumn, scope: SourceScope, source_slug: str | None
) -> ColumnElement[bool] | None:
    """The WHERE clause a scope and an optional named source add, or None."""
    if source_slug is not None:
        return listed_on(vacancy_id, source_slug)
    if scope is SourceScope.AGENT:
        return on_agent_source(vacancy_id)
    if scope is SourceScope.OTHERS:
        return ~on_agent_source(vacancy_id)
    return None
