"""Vacancy persistence: idempotent upserts, keyset listing, facet counts.

Repositories know about SQL and nothing about scoring, sources or HTTP. They
take and return schema objects or ORM instances, never raw rows.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    Select,
    String,
    and_,
    exists,
    false,
    func,
    literal,
    literal_column,
    or_,
    select,
)
from sqlalchemy import (
    cast as sql_cast,
)
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import uuid7
from app.db.enums import MatchBucket
from app.db.models import Application, Match, Vacancy, VacancySource
from app.db.repositories.cursor import Cursor, SortableColumn, keyset_order_by, keyset_where
from app.schemas.common import CursorPage, Facets, SortField
from app.schemas.vacancy import VacancyCreate, VacancyFilter, VacancyListItem

#: Columns refreshed every time a posting is seen again. Anything not listed —
#: the id, the fingerprint, first_seen_at — is written once and never moves.
REFRESHABLE_COLUMNS: tuple[str, ...] = (
    "title",
    "company",
    "company_url",
    "description_raw",
    "description_md",
    "seniority",
    "min_years",
    "city",
    "country",
    "remote",
    "salary_min",
    "salary_max",
    "currency",
    "is_gross",
    "period",
    "employment_type",
    "language",
    "published_at",
    "expires_at",
)

#: Which column each sort option actually orders by, and the label the selected
#: value carries so the cursor can read it back off the row.
SORT_COLUMNS: dict[SortField, SortableColumn] = {
    SortField.SCORE: Match.score,
    SortField.PUBLISHED_AT: Vacancy.published_at,
    # Never Vacancy.salary_min: comparing advertised amounts across currencies
    # ranks 500000 KZT above 4000 USD.
    SortField.SALARY: Vacancy.salary_min_normalized,
}

SORT_VALUE_FIELDS: dict[SortField, str] = {
    SortField.SCORE: "score",
    SortField.PUBLISHED_AT: "published_at",
    SortField.SALARY: "salary_min_normalized",
}

#: (source_slug, external_id) — the natural key of a vacancy_source row.
type SourceKey = tuple[str, str]
#: One element of a bulk_upsert batch.
type UpsertItem = tuple[VacancyCreate, str, str, str, dict[str, Any]]

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


@dataclass(frozen=True, slots=True)
class UpsertResult:
    """Outcome of upserting one posting."""

    vacancy_id: UUID
    created: bool


@dataclass(frozen=True, slots=True)
class BulkUpsertResult:
    """Outcome of upserting a batch."""

    created: int
    updated: int
    vacancy_ids: tuple[UUID, ...]

    @property
    def total(self) -> int:
        """How many postings the batch touched."""
        return self.created + self.updated


class VacancyRepository:
    """All vacancy reads and writes."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ── writes ────────────────────────────────────────────────────────

    async def upsert_by_external_id(
        self,
        vacancy: VacancyCreate,
        *,
        source_slug: str,
        external_id: str,
        url: str,
        raw: dict[str, Any] | None = None,
    ) -> UpsertResult:
        """Insert or refresh one posting together with its source link.

        Deliberately no SELECT first: a read-then-write races two connectors
        crawling the same cross-posted job, and the unique constraints on
        ``fingerprint`` and ``(source_slug, external_id)`` already decide the
        winner. Running a connector twice over the same payload therefore
        leaves exactly one vacancy row and one source row.
        """
        result = await self.bulk_upsert([(vacancy, source_slug, external_id, url, raw or {})])
        return UpsertResult(vacancy_id=result.vacancy_ids[0], created=result.created == 1)

    async def bulk_upsert(
        self,
        items: Sequence[UpsertItem],
    ) -> BulkUpsertResult:
        """Upsert a whole batch in two statements, not two per item.

        A connector page is a hundred postings; a per-item loop would be two
        hundred round trips. ``xmax = 0`` separates a freshly inserted row from
        an updated one, which is how a pipeline run reports "new" against
        "updated" without counting anything twice.
        """
        if not items:
            return BulkUpsertResult(created=0, updated=0, vacancy_ids=())

        # Both statements below need their own deduplication, on their own key.
        # ON CONFLICT cannot update a row the same statement just inserted, so a
        # repeated key raises CardinalityViolationError and loses the whole
        # batch. Both cases are ordinary connector behaviour: a cross-posted job
        # repeats the fingerprint, and an overlapping page or a retry repeats
        # (source_slug, external_id).
        #
        # The two keys must stay separate. Deduplicating the sources by
        # fingerprint instead would silently collapse a cross-posted job's two
        # source links into one, which is data loss rather than a crash.
        by_fingerprint: dict[str, VacancyCreate] = {
            vacancy.fingerprint: vacancy for vacancy, *_ in items
        }
        by_external_id: dict[
            tuple[str, str], tuple[VacancyCreate, str, str, str, dict[str, Any]]
        ] = {
            (source_slug, external_id): (vacancy, source_slug, external_id, url, raw)
            for vacancy, source_slug, external_id, url, raw in items
        }

        insert_vacancy = pg_insert(Vacancy).values(
            [
                {"id": uuid7(), **vacancy.model_dump(), "last_seen_at": func.now()}
                for vacancy in by_fingerprint.values()
            ]
        )
        upsert_vacancy: Any = insert_vacancy.on_conflict_do_update(
            index_elements=[Vacancy.fingerprint],
            set_={
                **{column: insert_vacancy.excluded[column] for column in REFRESHABLE_COLUMNS},
                "last_seen_at": func.now(),
                "updated_at": func.now(),
                "is_active": True,
            },
        ).returning(
            Vacancy.id,
            Vacancy.fingerprint,
            literal_column("(xmax = 0)").label("created"),
        )

        vacancy_rows = (await self.session.execute(upsert_vacancy)).all()
        id_by_fingerprint = {row.fingerprint: row.id for row in vacancy_rows}
        created = sum(1 for row in vacancy_rows if row.created)

        insert_source = pg_insert(VacancySource).values(
            [
                {
                    "id": uuid7(),
                    "vacancy_id": id_by_fingerprint[vacancy.fingerprint],
                    "source_slug": source_slug,
                    "external_id": external_id,
                    "url": url,
                    "raw": raw,
                }
                for vacancy, source_slug, external_id, url, raw in by_external_id.values()
            ]
        )
        await self.session.execute(
            insert_source.on_conflict_do_update(
                index_elements=[VacancySource.source_slug, VacancySource.external_id],
                set_={
                    "vacancy_id": insert_source.excluded.vacancy_id,
                    "url": insert_source.excluded.url,
                    "raw": insert_source.excluded.raw,
                    "updated_at": func.now(),
                },
            )
        )
        await self.session.flush()

        return BulkUpsertResult(
            created=created,
            updated=len(vacancy_rows) - created,
            vacancy_ids=tuple(row.id for row in vacancy_rows),
        )

    async def mark_inactive(self, vacancy_ids: Sequence[UUID]) -> int:
        """Retire postings a source stopped returning. Returns rows touched."""
        if not vacancy_ids:
            return 0
        stmt = (
            sa_update(Vacancy)
            .where(Vacancy.id.in_(vacancy_ids))
            .values(is_active=False, updated_at=func.now())
        )
        result = cast("CursorResult[Any]", await self.session.execute(stmt))
        return int(result.rowcount or 0)

    # ── reads ─────────────────────────────────────────────────────────

    async def get(self, vacancy_id: UUID) -> Vacancy | None:
        """Full vacancy with its sources and skills.

        No loader options: both relationships are declared ``lazy="selectin"``
        on the model. Repeating that here would suggest the eager load lives in
        the repository, and a future reader would wonder which one keeps the
        async serializer from raising MissingGreenlet.
        """
        stmt = select(Vacancy).where(Vacancy.id == vacancy_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_fingerprint(self, fingerprint: str) -> Vacancy | None:
        """Look a posting up by its deduplication key."""
        stmt = select(Vacancy).where(Vacancy.fingerprint == fingerprint)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_filtered(
        self,
        filters: VacancyFilter,
        *,
        profile_id: UUID | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        with_total: bool = False,
        with_facets: bool = False,
    ) -> CursorPage[VacancyListItem]:
        """One keyset-paginated page of the dashboard table.

        No OFFSET anywhere — see app/db/repositories/cursor.py for why, and for
        the NULL handling the ordering depends on.
        """
        limit = max(1, min(limit, MAX_PAGE_SIZE))
        sort_column = SORT_COLUMNS[filters.sort]

        stmt = self._apply_filters(self._base_select(profile_id), filters)
        if cursor is not None:
            stmt = stmt.where(
                keyset_where(sort_column, Vacancy.id, Cursor.decode(cursor), filters.direction)
            )
        stmt = stmt.order_by(*keyset_order_by(sort_column, Vacancy.id, filters.direction))

        # One row more than asked for: its presence is what says "there is a
        # next page", with no extra COUNT query.
        rows = (await self.session.execute(stmt.limit(limit + 1))).all()
        has_more = len(rows) > limit
        page_rows = rows[:limit]

        next_cursor: str | None = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = Cursor.from_row(
                row_id=last.id,
                value=getattr(last, SORT_VALUE_FIELDS[filters.sort]),
            ).encode()

        return CursorPage[VacancyListItem](
            items=[VacancyListItem.model_validate(row) for row in page_rows],
            next_cursor=next_cursor,
            total=await self.count(filters, profile_id=profile_id) if with_total else None,
            facets=await self.facets(filters, profile_id=profile_id) if with_facets else None,
        )

    async def count(self, filters: VacancyFilter, *, profile_id: UUID | None = None) -> int:
        """How many vacancies the filter matches, ignoring pagination."""
        inner = self._apply_filters(
            select(Vacancy.id).select_from(Vacancy).outerjoin(Match, self._match_on(profile_id)),
            filters,
        ).subquery()
        return int(await self.session.scalar(select(func.count()).select_from(inner)) or 0)

    async def facets(self, filters: VacancyFilter, *, profile_id: UUID | None = None) -> Facets:
        """Sidebar counts by source, bucket and city.

        One statement rather than one per dimension: the filtered id set is
        computed once as a CTE and the three groupings read from it.
        """
        filtered = self._apply_filters(
            select(Vacancy.id.label("vacancy_id"), Vacancy.city, Match.bucket)
            .select_from(Vacancy)
            .outerjoin(Match, self._match_on(profile_id)),
            filters,
        ).cte("filtered")

        by_bucket = select(
            sql_cast(literal("bucket"), String).label("kind"),
            sql_cast(filtered.c.bucket, String).label("key"),
            func.count().label("hits"),
        ).group_by(filtered.c.bucket)

        by_city = select(
            sql_cast(literal("city"), String).label("kind"),
            sql_cast(filtered.c.city, String).label("key"),
            func.count().label("hits"),
        ).group_by(filtered.c.city)

        by_source = (
            select(
                sql_cast(literal("source"), String).label("kind"),
                sql_cast(VacancySource.source_slug, String).label("key"),
                func.count(func.distinct(filtered.c.vacancy_id)).label("hits"),
            )
            .select_from(filtered)
            .join(VacancySource, VacancySource.vacancy_id == filtered.c.vacancy_id)
            .group_by(VacancySource.source_slug)
        )

        rows = (await self.session.execute(by_bucket.union_all(by_city, by_source))).all()

        facets = Facets()
        buckets: dict[str, dict[str, int]] = {
            "bucket": facets.buckets,
            "city": facets.cities,
            "source": facets.sources,
        }
        for row in rows:
            if row.key is None:
                continue
            buckets[row.kind][str(row.key)] = int(row.hits)
        return facets

    # ── query construction ────────────────────────────────────────────

    @staticmethod
    def _match_on(profile_id: UUID | None) -> ColumnElement[bool]:
        """LEFT JOIN condition tying match rows to one profile.

        With no profile the condition is constant false, so every vacancy gets
        a NULL score instead of quietly picking up another profile's match.
        """
        if profile_id is None:
            return false()
        return and_(Match.vacancy_id == Vacancy.id, Match.profile_id == profile_id)

    def _base_select(self, profile_id: UUID | None) -> Select[Any]:
        """Exactly the columns the dashboard table renders, and nothing else."""
        source_slugs = (
            select(func.array_agg(VacancySource.source_slug))
            .where(VacancySource.vacancy_id == Vacancy.id)
            .correlate(Vacancy)
            .scalar_subquery()
            .label("source_slugs")
        )
        is_applied = (
            select(1).where(Application.vacancy_id == Vacancy.id).correlate(Vacancy).exists()
        )
        return (
            select(
                Vacancy.id,
                Vacancy.title,
                Vacancy.company,
                source_slugs,
                Vacancy.city,
                Vacancy.country,
                Vacancy.remote,
                Vacancy.salary_min,
                Vacancy.salary_max,
                Vacancy.currency,
                Vacancy.salary_min_normalized,
                Match.score.label("score"),
                Match.bucket.label("bucket"),
                func.coalesce(func.jsonb_array_length(Match.missing_required), 0).label(
                    "missing_required_count"
                ),
                Vacancy.published_at,
                is_applied.label("is_applied"),
            )
            .select_from(Vacancy)
            .outerjoin(Match, self._match_on(profile_id))
        )

    def _apply_filters(self, stmt: Select[Any], filters: VacancyFilter) -> Select[Any]:
        """Translate a VacancyFilter into WHERE clauses."""
        conditions: list[ColumnElement[bool]] = [Vacancy.is_active.is_(True)]

        if not filters.include_filtered:
            conditions.append(or_(Match.bucket.is_(None), Match.bucket != MatchBucket.FILTERED))
        if filters.score_min is not None:
            conditions.append(Match.score >= filters.score_min)
        if filters.score_max is not None:
            conditions.append(Match.score <= filters.score_max)
        if filters.bucket:
            conditions.append(Match.bucket.in_(filters.bucket))
        if filters.remote:
            conditions.append(Vacancy.remote.in_(filters.remote))
        if filters.seniority:
            conditions.append(Vacancy.seniority.in_(filters.seniority))
        if filters.city:
            conditions.append(Vacancy.city.ilike(filters.city))
        if filters.country:
            conditions.append(Vacancy.country == filters.country)
        if filters.company:
            conditions.append(Vacancy.company.ilike(f"%{filters.company}%"))
        if filters.currency:
            conditions.append(Vacancy.currency == filters.currency)
        if filters.salary_min is not None:
            conditions.append(Vacancy.salary_min_normalized >= filters.salary_min)
        if filters.has_salary is True:
            conditions.append(or_(Vacancy.salary_min.is_not(None), Vacancy.salary_max.is_not(None)))
        if filters.has_salary is False:
            conditions.append(and_(Vacancy.salary_min.is_(None), Vacancy.salary_max.is_(None)))
        if filters.posted_within_days is not None:
            cutoff = datetime.now(UTC) - timedelta(days=filters.posted_within_days)
            conditions.append(Vacancy.published_at >= cutoff)
        if filters.missing_skills_max is not None:
            conditions.append(
                func.coalesce(func.jsonb_array_length(Match.missing_required), 0)
                <= filters.missing_skills_max
            )
        if filters.source:
            conditions.append(
                exists(
                    select(1)
                    .select_from(VacancySource)
                    .where(
                        VacancySource.vacancy_id == Vacancy.id,
                        VacancySource.source_slug.in_(filters.source),
                    )
                    .correlate(Vacancy)
                )
            )
        if filters.q:
            conditions.append(
                Vacancy.search_vector.op("@@")(func.plainto_tsquery("simple", filters.q))
            )
        if filters.exclude_applied:
            conditions.append(
                ~exists(
                    select(1)
                    .select_from(Application)
                    .where(Application.vacancy_id == Vacancy.id)
                    .correlate(Vacancy)
                )
            )

        return stmt.where(and_(*conditions))
