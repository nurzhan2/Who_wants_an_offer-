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
    Numeric,
    Select,
    String,
    Table,
    and_,
    bindparam,
    case,
    exists,
    false,
    func,
    literal,
    literal_column,
    null,
    or_,
    select,
)
from sqlalchemy import (
    cast as sql_cast,
)
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import uuid7
from app.db.enums import MatchBucket
from app.db.models import Application, Match, Vacancy, VacancySkill, VacancySource
from app.db.repositories.cursor import Cursor, SortableColumn, keyset_order_by, keyset_where
from app.db.seed_rows import fullest_first, seed_only
from app.schemas.common import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    CursorPage,
    Facets,
    MatchMode,
    SortField,
)
from app.schemas.dashboard import HarvestedVacancy, VacancyCounts
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
    # Travels with the fingerprint. A conflict means the incoming posting hashed
    # to the same key, so the row is valid under the incoming algorithm too and
    # recording that is what lets phase 4 tell recomputed rows from stale ones.
    "fingerprint_version",
)

#: Which column each sort option actually orders by, and the label the selected
#: value carries so the cursor can read it back off the row.
SORT_COLUMNS: dict[SortField, SortableColumn] = {
    # The combined score; ``list_filtered`` swaps in :func:`mode_score` for the
    # mode the list is ranked by.
    SortField.SCORE: Match.score,
    SortField.PUBLISHED_AT: Vacancy.published_at,
    # Never Vacancy.salary_min: comparing advertised amounts across currencies
    # ranks 500000 KZT above 4000 USD.
    SortField.SALARY: Vacancy.salary_min_normalized,
}

SORT_VALUE_FIELDS: dict[SortField, str] = {
    # Not "score": under a mode other than the combined one the list is ranked
    # by the mode's number, and a cursor carrying the combined score would
    # compare it against the wrong column and skip rows.
    SortField.SCORE: "mode_score",
    SortField.PUBLISHED_AT: "published_at",
    SortField.SALARY: "salary_min_normalized",
}

#: The ``match.component_scores`` key each single-signal mode ranks by. The
#: scoring pass writes all three for every vacancy it scores, whichever formula
#: it ran, so a mode is a read of a stored number and never a computation.
MODE_COMPONENTS: dict[MatchMode, str] = {
    MatchMode.TITLE: "title_similarity",
    MatchMode.DESCRIPTION: "semantic_similarity",
    MatchMode.SKILLS: "skill_coverage_required",
}


def mode_score(mode: MatchMode) -> SortableColumn:
    """The stored number a mode ranks by, as a nullable NUMERIC expression.

    The skills mode has one case the stored component cannot tell apart on its
    own. ``MatchComponentScores`` writes an unmeasured coverage as 0.00, so a
    posting that names no requirements at all — 341 of 1355 ranked on 17 Sep
    2026 — would sit among the postings whose requirements the profile covers
    none of. Those are different facts: one is silence, the other is a miss.
    The two stored requirement lists tell them apart, so a posting with both
    empty ranks as unmeasured, after everything that was measured.
    """
    if mode is MatchMode.COMBINED:
        return Match.score
    value = sql_cast(Match.component_scores.op("->>")(MODE_COMPONENTS[mode]), Numeric(5, 2))
    if mode is not MatchMode.SKILLS:
        return value
    stated = func.jsonb_array_length(Match.matched_skills) + func.jsonb_array_length(
        Match.missing_required
    )
    return case((stated > 0, value), else_=null())


#: (source_slug, external_id) — the natural key of a vacancy_source row.
type SourceKey = tuple[str, str]
#: One element of a bulk_upsert batch.
type UpsertItem = tuple[VacancyCreate, str, str, str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class EmbeddingCandidate:
    """A row that may need a vector, with everything needed to build its text."""

    id: UUID
    title: str
    company: str | None
    city: str | None
    description: str | None
    #: Hash of the text the current vector was computed from, if any.
    stored_hash: str | None


@dataclass(frozen=True, slots=True)
class EmbeddedVacancy:
    """A computed vector, ready to be written back."""

    id: UUID
    vector: Sequence[float]
    text_hash: str


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


def _never_embedded() -> ColumnElement[bool]:
    """Rows that have no vector at all, as opposed to one that may be stale.

    The narrower half of :func:`_needs_embedding`, and the only half whose
    answer does not depend on the text. A row matching this needs work whatever
    its hash turns out to be, so counting these is how the step tells "the
    window is hiding rows that genuinely have no vector" from "a re-crawl
    touched a lot of rows and every one of them is already up to date" — two
    situations the wider predicate reports identically.

    This is exactly the partial index ``ix_pg_vacancy_needs_embedding`` covers,
    so the count is an index-only scan rather than a table sweep.
    """
    return or_(Vacancy.embedding.is_(None), Vacancy.embedded_at.is_(None))


def _needs_embedding() -> ColumnElement[bool]:
    """The one definition of "this row's vector is suspect".

    A vector is suspect when there is none, when nothing recorded computing one,
    or when the row has been written since the vector was computed. Shared by
    the windowed selection and the count so the two can never disagree about
    which rows they are talking about.
    """
    return or_(
        Vacancy.embedding.is_(None),
        Vacancy.embedded_at.is_(None),
        Vacancy.updated_at > Vacancy.embedded_at,
    )


def _needs_title_embedding() -> ColumnElement[bool]:
    """Rows whose title vector is missing, or was computed from another title.

    Exact in SQL, unlike :func:`_needs_embedding`: a title is short enough to
    hash in the database, so no row that is already current is ever offered and
    the selection cannot be starved by re-crawl churn. The hash is over the
    stored title byte for byte, the same text the vector is computed from.
    """
    return or_(
        Vacancy.title_embedding.is_(None),
        Vacancy.title_embedding_hash.is_(None),
        Vacancy.title_embedding_hash
        != func.encode(func.sha256(func.convert_to(Vacancy.title, "UTF8")), "hex"),
    )


@dataclass(frozen=True, slots=True)
class TitleCandidate:
    """A row whose title needs a vector."""

    id: UUID
    title: str


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
                # Completeness only ever improves. PostgreSQL orders an enum by
                # declaration, and vacancy_completeness is declared best-first
                # ('full', 'snippet', 'stub'), so LEAST is "the more complete of
                # the two". A plain overwrite would let a source that carries
                # only headlines downgrade a posting we already hold in full,
                # and the description would be gone with nothing recording that
                # it had ever been there.
                "completeness": func.least(
                    Vacancy.completeness, insert_vacancy.excluded.completeness
                ),
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

    async def count_needing_embedding(self) -> int:
        """How many rows :meth:`needs_embedding` would offer if it had no window.

        The windowed selection cannot answer "how much is left": it stops at its
        limit, so a full window means "at least this many" and nothing more. The
        embedding step reports a remainder to the operator, and a remainder that
        is really a page size is worse than no number at all — it is the reason
        a database holding 466 vacancies and no vectors looked healthy.

        Same predicate as :meth:`needs_embedding`, deliberately shared rather
        than retyped: two copies of it would drift, and the drift would show up
        as a count that never reaches zero while the selection is empty.
        """
        stmt = select(func.count()).select_from(Vacancy).where(_needs_embedding())
        return int((await self.session.execute(stmt)).scalar_one())

    async def count_never_embedded(self) -> int:
        """How many rows have no vector at all.

        Answers the one question the wider count cannot: whether a window full
        of already-current rows is hiding real work behind it. A row with no
        vector needs one no matter what its text hash says, so a non-zero answer
        here — after a pass that hashed a full window and found nothing to do —
        means the selection's ordering really is starving the backlog. A zero
        means the remainder is re-crawl churn and there is nothing to warn about.
        """
        stmt = select(func.count()).select_from(Vacancy).where(_never_embedded())
        return int((await self.session.execute(stmt)).scalar_one())

    async def needs_embedding(self, *, limit: int = 500) -> list[EmbeddingCandidate]:
        """Rows whose vector may be missing or out of date.

        Two stages on purpose. This one is the cheap SQL narrowing: a vector is
        suspect when there is none, or when the row has been written since the
        vector was computed. The exact answer needs the text itself, so the
        caller hashes it and drops the rows whose stored hash still matches —
        which is the common case, because every re-crawl bumps ``updated_at``
        whether or not the description actually moved.

        Ordered by ``last_seen_at`` so that when the limit bites, it is the
        postings still being advertised that get vectors first.
        """
        stmt = (
            select(
                Vacancy.id,
                Vacancy.title,
                Vacancy.company,
                Vacancy.city,
                Vacancy.description_raw,
                Vacancy.embedding_text_hash,
            )
            .where(_needs_embedding())
            .order_by(Vacancy.last_seen_at.desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            EmbeddingCandidate(
                id=row.id,
                title=row.title,
                company=row.company,
                city=row.city,
                description=row.description_raw,
                stored_hash=row.embedding_text_hash,
            )
            for row in rows
        ]

    async def set_embeddings(self, items: Sequence[EmbeddedVacancy]) -> int:
        """Write a batch of vectors with the hash of the text they came from.

        One executemany rather than one UPDATE per row: a run embeds hundreds,
        and the round trips would dominate the work the model just did.
        """
        if not items:
            return 0
        # Against the Table, not the mapped class. Handed the entity, SQLAlchemy
        # routes an executemany UPDATE through its "bulk update by primary key"
        # path, which demands the primary key under its own column name and
        # tries to synchronise the identity map — neither of which this needs.
        # The Core statement writes the rows and leaves the session alone.
        table = cast("Table", Vacancy.__table__)
        stmt = (
            sa_update(table)
            .where(table.c.id == bindparam("row_id"))
            .values(
                embedding=bindparam("vector"),
                embedding_text_hash=bindparam("text_hash"),
                embedded_at=func.now(),
            )
        )
        await self.session.execute(
            stmt,
            [
                {
                    "row_id": item.id,
                    "vector": list(item.vector),
                    "text_hash": item.text_hash,
                }
                for item in items
            ],
        )
        await self.session.flush()
        return len(items)

    async def needs_title_embedding(self, *, limit: int) -> list[TitleCandidate]:
        """Rows whose title vector is missing or stale, still-advertised first."""
        stmt = (
            select(Vacancy.id, Vacancy.title)
            .where(_needs_title_embedding())
            .order_by(Vacancy.last_seen_at.desc(), Vacancy.id)
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [TitleCandidate(id=row.id, title=row.title) for row in rows]

    async def count_needing_title_embedding(self) -> int:
        """How many rows :meth:`needs_title_embedding` would offer with no limit."""
        stmt = select(func.count()).select_from(Vacancy).where(_needs_title_embedding())
        return int((await self.session.execute(stmt)).scalar_one())

    async def set_title_embeddings(self, items: Sequence[EmbeddedVacancy]) -> int:
        """Write a batch of title vectors with the hash of the title behind each.

        Against the Table for the reason :meth:`set_embeddings` gives.
        """
        if not items:
            return 0
        table = cast("Table", Vacancy.__table__)
        stmt = (
            sa_update(table)
            .where(table.c.id == bindparam("row_id"))
            .values(
                title_embedding=bindparam("vector"),
                title_embedding_hash=bindparam("text_hash"),
            )
        )
        await self.session.execute(
            stmt,
            [
                {"row_id": item.id, "vector": list(item.vector), "text_hash": item.text_hash}
                for item in items
            ],
        )
        await self.session.flush()
        return len(items)

    async def known_external_ids(self, source_slug: str, external_ids: Sequence[str]) -> set[str]:
        """Which of these postings this source has already given us.

        Feeds the early exit from pagination: a source with no usable date
        filter returns old postings mixed with new ones, so the only way to stop
        paying for pages of things we already hold is to recognise them. The
        answer has to come before the upsert, which is why it cannot be read off
        ``BulkUpsertResult``.

        Chunked because the caller passes a whole page-set at once and an
        unbounded ``IN`` clause on a long run becomes a query with several
        thousand bind parameters. One index-only scan per chunk on the existing
        unique index over ``(source_slug, external_id)``.
        """
        if not external_ids:
            return set()

        # Deduplicated first: an overlapping page repeats ids, and there is no
        # point sending the same one twice within a single lookup.
        unique = list(dict.fromkeys(external_ids))
        size = settings.external_id_lookup_batch
        known: set[str] = set()
        for start in range(0, len(unique), size):
            chunk = unique[start : start + size]
            stmt = select(VacancySource.external_id).where(
                VacancySource.source_slug == source_slug,
                VacancySource.external_id.in_(chunk),
            )
            known.update((await self.session.execute(stmt)).scalars().all())
        return known

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

    async def counts(self, *, profile_id: UUID | None = None) -> VacancyCounts:
        """How much corpus there is, and how much of it is usable, in one pass.

        One statement over ``vacancy`` with filtered aggregates, plus two small
        ones for the tables it cannot reach from there. Five separate COUNTs
        would each see a slightly different database on a corpus a crawl is
        still writing into, and the overview screen shows them side by side as
        though they were taken at one moment — so they are.

        ``needs_embedding`` uses the same predicate the embedding step selects
        rows with, imported rather than restated: a screen that disagreed with
        the step about which rows need work would report a backlog that never
        drains, or none while one exists.
        """
        totals = (
            await self.session.execute(
                select(
                    func.count().label("total"),
                    func.count().filter(Vacancy.is_active.is_(True)).label("active"),
                    func.count().filter(Vacancy.embedding.is_not(None)).label("embedded"),
                    func.count().filter(_needs_embedding()).label("needs_embedding"),
                ).select_from(Vacancy)
            )
        ).one()
        skill_rows = int(
            await self.session.scalar(select(func.count()).select_from(VacancySkill)) or 0
        )
        scored = 0
        if profile_id is not None:
            scored = int(
                await self.session.scalar(
                    select(func.count()).select_from(Match).where(Match.profile_id == profile_id)
                )
                or 0
            )
        return VacancyCounts(
            total=totals.total,
            active=totals.active,
            embedded=totals.embedded,
            needs_embedding=totals.needs_embedding,
            scored=scored,
            skill_rows=skill_rows,
        )

    async def first_seen_since(
        self,
        since: datetime,
        *,
        profile_id: UUID | None = None,
        limit: int = 50,
    ) -> tuple[int, list[HarvestedVacancy]]:
        """Postings this crawl actually bought, by name, newest first.

        Returns the whole count and a page of titles, because the two answer
        different halves of one question: how much a run brought back, and
        whether it was worth bringing. On a source that cannot be asked a query
        — hh's sitemap carries a URL and a date and nothing else — only the
        titles say whether the budget went on postings for this candidate or on
        somebody else's.

        ``first_seen_at`` and not ``created_at``: a posting seen again by a
        later run keeps the date it was first bought, which is exactly the
        distinction between what a run *found* and what it *paid for*.
        """
        where = Vacancy.first_seen_at >= since
        total = int(
            await self.session.scalar(select(func.count()).select_from(Vacancy).where(where)) or 0
        )
        source = (
            select(VacancySource.source_slug)
            .where(VacancySource.vacancy_id == Vacancy.id)
            .correlate(Vacancy)
            .order_by(VacancySource.source_slug)
            .limit(1)
            .scalar_subquery()
        )
        url = (
            select(VacancySource.url)
            .where(VacancySource.vacancy_id == Vacancy.id)
            .correlate(Vacancy)
            .order_by(VacancySource.source_slug)
            .limit(1)
            .scalar_subquery()
        )
        rows = (
            await self.session.execute(
                select(
                    Vacancy.id,
                    Vacancy.title,
                    Vacancy.company,
                    Vacancy.first_seen_at,
                    source.label("source_slug"),
                    url.label("url"),
                    Match.score,
                    Match.bucket,
                )
                .select_from(Vacancy)
                .outerjoin(Match, self._match_on(profile_id))
                .where(where)
                .order_by(Vacancy.first_seen_at.desc(), Vacancy.id)
                .limit(limit)
            )
        ).all()
        return total, [HarvestedVacancy.model_validate(row, from_attributes=True) for row in rows]

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
        sort_column: SortableColumn = SORT_COLUMNS[filters.sort]
        if filters.sort is SortField.SCORE:
            sort_column = mode_score(filters.mode)

        stmt = self._apply_filters(self._base_select(profile_id, filters.mode), filters)
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

    def _base_select(
        self, profile_id: UUID | None, mode: MatchMode = MatchMode.COMBINED
    ) -> Select[Any]:
        """Exactly the columns the dashboard table renders, and nothing else.

        The sources come fullest first, so the first slug and ``source_url`` name
        the same row: the one a person learns most from when they open it.
        """
        order = (*fullest_first(), VacancySource.created_at, VacancySource.id)
        source_slugs = (
            select(func.array_agg(aggregate_order_by(VacancySource.source_slug, *order)))
            .where(VacancySource.vacancy_id == Vacancy.id)
            .correlate(Vacancy)
            .scalar_subquery()
            .label("source_slugs")
        )
        source_url = (
            select(VacancySource.url)
            .where(VacancySource.vacancy_id == Vacancy.id)
            .correlate(Vacancy)
            .order_by(*order)
            .limit(1)
            .scalar_subquery()
            .label("source_url")
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
                source_url,
                seed_only(Vacancy.id).label("is_seed"),
                Vacancy.city,
                Vacancy.country,
                Vacancy.remote,
                Vacancy.salary_min,
                Vacancy.salary_max,
                Vacancy.currency,
                Vacancy.salary_min_normalized,
                Match.score.label("score"),
                mode_score(mode).label("mode_score"),
                Match.bucket.label("bucket"),
                func.coalesce(func.jsonb_array_length(Match.missing_required), 0).label(
                    "missing_required_count"
                ),
                Vacancy.published_at,
                Vacancy.last_seen_at,
                Vacancy.is_active,
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
            # ``>= x`` is false for NULL, so this clause on its own drops every
            # posting that advertises nothing — which here is most of them. The
            # OR is what keeps a salary floor from being a "has a salary" filter
            # nobody asked for; VacancyFilter.include_unpriced argues it.
            priced = Vacancy.salary_min_normalized >= filters.salary_min
            conditions.append(
                or_(priced, Vacancy.salary_min_normalized.is_(None))
                if filters.include_unpriced
                else priced
            )
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
