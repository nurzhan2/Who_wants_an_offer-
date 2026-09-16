"""Telling the rows ``scripts/seed.py`` invented apart from real postings.

The seed writes source rows for hh, telegram, jsearch and remotive, and on
2026-09-16 they were the only rows telegram and jsearch had at all: 15 and 18,
all created in one second on 2026-09-03. Counted as data, they made a connector
that does not exist look like it worked and a source that had never run look
like one that ran and found little.

The marker is the whole address, not the id. Every seed URL is
``https://example.test/<slug>/dev-NNN``, and ``.test`` is reserved (RFC 2606):
no real posting can live there. The id alone is weaker — the seed's
``<slug>-dev-NNN`` shape also occurs in real arbeitnow slugs, one of which is in
the database. The host alone is too broad — the test factories put their own
postings on ``example.test`` too, and those stand for real ones.

Rows are marked rather than deleted: the seed is how a fresh checkout gets a
dashboard to look at, and the tests import it.
"""

import json
import re
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Text, and_, case, cast, exists, func, not_, select
from sqlalchemy.orm import QueryableAttribute

from app.db.models import VacancySource

#: Where every seeded URL starts. ``scripts/seed.py`` builds its URLs with it.
SEED_URL_PREFIX = "https://example.test/"
#: The full shape of a seeded URL: the prefix, a slug, and ``dev-`` with the
#: seed's three-digit index. Written once, for Python and PostgreSQL alike —
#: both read this subset of the regex syntax the same way.
SEED_URL_PATTERN = r"^https://example\.test/[a-z0-9_]+/dev-[0-9]{3}$"

_SEED_URL = re.compile(SEED_URL_PATTERN)

#: A vacancy id as a query sees it: a mapped attribute or any column expression.
type VacancyIdColumn = ColumnElement[UUID] | QueryableAttribute[UUID]


def is_seed_url(url: str) -> bool:
    """Whether a source row's address is one the seed made up."""
    return _SEED_URL.match(url) is not None


def seed_row() -> ColumnElement[bool]:
    """SQL: this ``vacancy_source`` row is a seed row."""
    return VacancySource.url.regexp_match(SEED_URL_PATTERN)


def seed_only(vacancy_id: VacancyIdColumn) -> ColumnElement[bool]:
    """SQL: every source row of this vacancy is a seed row.

    "Every", because a real posting that the fingerprint happened to collapse
    into a seeded vacancy makes that vacancy real enough to act on.
    """
    return not_(
        exists(
            select(VacancySource.id).where(
                and_(VacancySource.vacancy_id == vacancy_id, not_(seed_row()))
            )
        )
    )


def fullest_first() -> tuple[ColumnElement[int], ColumnElement[int]]:
    """ORDER BY terms putting the source row with the most data first.

    Real rows before seed rows, then the larger stored payload. The payload is
    the only measure every source shares: hh keeps the page's whole boot state,
    an aggregator keeps its own record of the posting, and a longer one is the
    one a person learns more from. Callers add ``created_at`` and ``id`` after
    these so the choice is stable.
    """
    return (
        case((seed_row(), 1), else_=0),
        -func.octet_length(cast(VacancySource.raw, Text)),
    )


def payload_size(raw: dict[str, Any]) -> int:
    """Python's reading of the size :func:`fullest_first` orders by.

    PostgreSQL renders ``jsonb::text`` with the same ``", "`` and ``": "``
    separators ``json.dumps`` uses, so the two agree on which row is fuller.
    """
    return len(json.dumps(raw, ensure_ascii=False).encode())
