"""Match persistence: batch upserts and the top-N read the dashboard needs."""

from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

from sqlalchemy import column, func, select, true
from sqlalchemy import delete as sa_delete
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.base import uuid7
from app.db.models import Match, Vacancy
from app.schemas.match import MatchCreate

#: Rewritten on every rescore. profile_id and vacancy_id identify the row and
#: are therefore not in this list.
REFRESHABLE_COLUMNS: tuple[str, ...] = (
    "score",
    "rule_score",
    "semantic_score",
    "llm_score",
    "bucket",
    "component_scores",
    "matched_skills",
    "missing_required",
    "missing_nice",
    "red_flags",
    "experience_gap_years",
    "verdict",
    "application_angle",
)


class MatchRepository:
    """Reads and writes for scoring results."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def bulk_upsert(self, matches: Sequence[MatchCreate]) -> int:
        """Write a whole rescore in one statement.

        A rescore touches every vacancy for a profile — thousands of rows —
        so this is a single multi-values INSERT ... ON CONFLICT rather than a
        loop. Returns the number of rows written.
        """
        if not matches:
            return 0

        # (profile_id, vacancy_id) is unique, and ON CONFLICT cannot update a
        # row the same statement just inserted. Collapse duplicates first.
        deduped: dict[tuple[UUID, UUID], MatchCreate] = {
            (match.profile_id, match.vacancy_id): match for match in matches
        }

        insert = pg_insert(Match).values(
            [
                {
                    "id": uuid7(),
                    **match.model_dump(mode="json"),
                    "scored_at": func.now(),
                }
                for match in deduped.values()
            ]
        )
        stmt = insert.on_conflict_do_update(
            index_elements=[Match.profile_id, Match.vacancy_id],
            set_={
                **{column: insert.excluded[column] for column in REFRESHABLE_COLUMNS},
                "scored_at": func.now(),
                "updated_at": func.now(),
            },
        )
        result = cast("CursorResult[Any]", await self.session.execute(stmt))
        await self.session.flush()
        return int(result.rowcount or 0)

    async def get(self, profile_id: UUID, vacancy_id: UUID) -> Match | None:
        """The match for one profile and one vacancy."""
        stmt = select(Match).where(Match.profile_id == profile_id, Match.vacancy_id == vacancy_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def top_for_profile(
        self, profile_id: UUID, *, limit: int = 30, min_score: float | None = None
    ) -> list[Match]:
        """Best matches for a profile, highest score first.

        Serves the LLM re-rank shortlist, which is why it eager-loads the
        vacancy: the caller needs the posting text and must not trigger an
        N+1 on a lazy="raise" relationship.
        """
        stmt = (
            select(Match)
            .where(Match.profile_id == profile_id)
            .order_by(Match.score.desc(), Match.vacancy_id.desc())
            .limit(limit)
            .options(selectinload(Match.vacancy).selectinload(Vacancy.skills))
        )
        if min_score is not None:
            stmt = stmt.where(Match.score >= min_score)
        return list((await self.session.execute(stmt)).scalars().all())

    async def delete_for_profile(self, profile_id: UUID) -> int:
        """Drop every match of a profile, e.g. before a full rescore."""
        stmt = sa_delete(Match).where(Match.profile_id == profile_id)
        result = cast("CursorResult[Any]", await self.session.execute(stmt))
        return int(result.rowcount or 0)

    async def missing_skill_counts(
        self, profile_id: UUID, *, min_score: float = 55.0, limit: int = 20
    ) -> list[tuple[str, int]]:
        """Which skills block the most otherwise-reachable vacancies.

        This is the skill-gap analytics query: unnest the JSONB arrays of
        missing required skills and count them. Returned as pairs so the
        analytics service can turn them into whatever it needs.
        """
        # The column needs an explicit JSONB type: a bare table_valued("value")
        # produces an untyped expression that cannot be subscripted.
        skill = (
            func.jsonb_array_elements(Match.missing_required)
            .table_valued(column("value", JSONB))
            .alias("missing")
        )
        stmt = (
            select(
                skill.c.value["canonical_name"].astext.label("canonical_name"),
                func.count().label("hits"),
            )
            .select_from(Match)
            .join(skill, onclause=true())
            .where(Match.profile_id == profile_id, Match.score >= min_score)
            .group_by("canonical_name")
            .order_by(func.count().desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(str(row.canonical_name), int(row.hits)) for row in rows]
