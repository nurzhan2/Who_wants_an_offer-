"""Keyset pagination cursors.

Why not OFFSET: the vacancy list is re-scored and re-crawled continuously, so
rows shift between requests. With OFFSET that silently drops or repeats rows.
A keyset cursor pins the position to a concrete (sort value, id) pair instead.

Two details that are easy to get wrong and impossible to notice on tidy test
data:

* **The tiebreaker is mandatory.** Hundreds of vacancies share a score. Without
  comparing ``(sort_value, id)`` as a pair, every row with a duplicate score
  after the page boundary is skipped.
* **NULLs need their own branch.** ``salary_min_normalized`` and
  ``published_at`` are nullable, and so is ``score`` (a vacancy with no match
  row). ``value < NULL`` is NULL, i.e. false, so a naive comparison drops the
  entire NULL tail. Ordering is therefore ``NULLS LAST`` and the cursor carries
  an explicit flag saying "we are already inside the NULL tail".
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import ColumnElement, and_, or_
from sqlalchemy.orm import InstrumentedAttribute

from app.schemas.common import SortDirection

#: SQLAlchemy's ORM attributes behave as column expressions at runtime but are
#: not declared as ColumnElement subclasses, so both spellings are accepted.
type SortableColumn = ColumnElement[Any] | InstrumentedAttribute[Any]

ValueKind = Literal["decimal", "datetime", "null"]


class InvalidCursorError(ValueError):
    """The cursor is not something this application produced."""


@dataclass(frozen=True, slots=True)
class Cursor:
    """Position of the last row of a page."""

    row_id: UUID
    value: Decimal | datetime | None
    kind: ValueKind

    @property
    def is_null(self) -> bool:
        """True when the last row's sort value was NULL."""
        return self.kind == "null"

    @classmethod
    def from_row(cls, row_id: UUID, value: Any) -> "Cursor":
        """Build a cursor from the last row of a page."""
        if value is None:
            return cls(row_id=row_id, value=None, kind="null")
        if isinstance(value, datetime):
            return cls(row_id=row_id, value=value, kind="datetime")
        if isinstance(value, Decimal):
            return cls(row_id=row_id, value=value, kind="decimal")
        return cls(row_id=row_id, value=Decimal(str(value)), kind="decimal")

    def encode(self) -> str:
        """Serialise to an opaque, URL-safe token."""
        raw: str | None
        if self.value is None:
            raw = None
        elif isinstance(self.value, datetime):
            raw = self.value.isoformat()
        else:
            raw = str(self.value)
        payload = json.dumps(
            {"id": str(self.row_id), "v": raw, "k": self.kind},
            separators=(",", ":"),
        )
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str) -> "Cursor":
        """Parse a token produced by :meth:`encode`.

        A cursor arrives from the outside world, so every failure mode here is
        a client error, not a server one.
        """
        padding = "=" * (-len(token) % 4)
        try:
            payload = json.loads(base64.urlsafe_b64decode(token + padding))
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidCursorError("cursor is not decodable") from exc
        if not isinstance(payload, dict):
            raise InvalidCursorError("cursor payload is not an object")

        kind = payload.get("k")
        if kind not in ("decimal", "datetime", "null"):
            raise InvalidCursorError(f"unknown cursor kind {kind!r}")
        try:
            row_id = UUID(str(payload["id"]))
        except (KeyError, ValueError) as exc:
            raise InvalidCursorError("cursor has no usable id") from exc

        raw = payload.get("v")
        value: Decimal | datetime | None
        if kind == "null":
            value = None
        elif kind == "datetime":
            try:
                value = datetime.fromisoformat(str(raw))
            except ValueError as exc:
                raise InvalidCursorError("cursor timestamp is malformed") from exc
        else:
            try:
                value = Decimal(str(raw))
            except InvalidOperation as exc:
                raise InvalidCursorError("cursor number is malformed") from exc

        return cls(row_id=row_id, value=value, kind=kind)


def keyset_order_by(
    sort_column: SortableColumn,
    id_column: SortableColumn,
    direction: SortDirection,
) -> list[ColumnElement[Any]]:
    """ORDER BY for a keyset scan: sort column with NULLS LAST, then the id.

    The id repeats the primary direction; mixing them would make the cursor
    comparison and the ordering disagree.
    """
    if direction is SortDirection.DESC:
        return [sort_column.desc().nullslast(), id_column.desc()]
    return [sort_column.asc().nullslast(), id_column.asc()]


def keyset_where(
    sort_column: SortableColumn,
    id_column: SortableColumn,
    cursor: Cursor,
    direction: SortDirection,
) -> ColumnElement[bool]:
    """Rows strictly after ``cursor`` in the ordering of :func:`keyset_order_by`.

    With NULLS LAST there are two regions. While the cursor still holds a
    value, the next page is "smaller values, or the same value with a smaller
    id, or anything in the NULL tail". Once the cursor is inside the NULL tail
    only the id matters — comparing against a NULL value would yield NULL and
    swallow every remaining row.
    """
    if cursor.is_null:
        if direction is SortDirection.DESC:
            return and_(sort_column.is_(None), id_column < cursor.row_id)
        return and_(sort_column.is_(None), id_column > cursor.row_id)

    if direction is SortDirection.DESC:
        return or_(
            sort_column < cursor.value,
            and_(sort_column == cursor.value, id_column < cursor.row_id),
            sort_column.is_(None),
        )
    return or_(
        sort_column > cursor.value,
        and_(sort_column == cursor.value, id_column > cursor.row_id),
        sort_column.is_(None),
    )
