"""Startup consistency checks between the code and the live schema.

The one that matters is the embedding width. ``Vector(1024)`` is a literal in
the migration while ``EMBEDDING_DIM`` is configuration, and nothing forces them
to agree. If they drift, every read works and every write of an embedding fails
with a confusing ``expected 1024 dimensions, not 768`` — from the resume upload
path, weeks after the migration that caused it. Comparing them at startup turns
that into one clear message before the first request.
"""

import re

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import SchemaMismatchError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Every column declared as a pgvector column, as (table, column).
VECTOR_COLUMNS: tuple[tuple[str, str], ...] = (
    ("vacancy", "embedding"),
    ("candidate_profile", "embedding"),
)

#: format_type renders a pgvector column as "vector(1024)".
_VECTOR_TYPE = re.compile(r"^vector\((\d+)\)$")

_DECLARED_TYPE_SQL = text(
    """
    SELECT format_type(a.atttypid, a.atttypmod) AS declared_type
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = current_schema()
      AND c.relname = :table_name
      AND a.attname = :column_name
      AND a.attnum > 0
      AND NOT a.attisdropped
    """
)


async def column_vector_dimension(
    session: AsyncSession, table_name: str, column_name: str
) -> int | None:
    """Width a pgvector column is actually declared with.

    Returns ``None`` when the column does not exist — a database that has not
    been migrated yet is not a mismatch.
    """
    declared = await session.scalar(
        _DECLARED_TYPE_SQL, {"table_name": table_name, "column_name": column_name}
    )
    if declared is None:
        return None
    match = _VECTOR_TYPE.match(str(declared))
    if match is None:
        raise SchemaMismatchError(
            f"{table_name}.{column_name} is {declared!r}, not a pgvector column"
        )
    return int(match.group(1))


async def verify_embedding_dimension(session: AsyncSession) -> None:
    """Fail fast when the schema and EMBEDDING_DIM disagree.

    Three outcomes, deliberately different:

    * dimensions agree — silence;
    * the table or column is missing — a warning, because migrations simply
      have not run yet and refusing to boot would break ``alembic upgrade``
      workflows;
    * the database is unreachable — a warning, because /health already reports
      that and the process should stay up to say so;
    * dimensions disagree — :class:`SchemaMismatchError`, which stops startup.
    """
    expected = settings.embedding_dim

    for table_name, column_name in VECTOR_COLUMNS:
        try:
            actual = await column_vector_dimension(session, table_name, column_name)
        except SQLAlchemyError as exc:
            logger.warning(
                "embedding_dimension_check_skipped",
                reason=type(exc).__name__,
                table=table_name,
                column=column_name,
            )
            return

        if actual is None:
            logger.warning(
                "embedding_dimension_check_skipped",
                reason="column_missing",
                table=table_name,
                column=column_name,
                hint="run `alembic upgrade head`",
            )
            continue

        if actual != expected:
            raise SchemaMismatchError(
                f"{table_name}.{column_name} is vector({actual}) but EMBEDDING_DIM "
                f"is {expected}. Either set EMBEDDING_DIM={actual} or write a "
                f"migration that alters the column to vector({expected}); the "
                f"embeddings already stored are only valid for one of the two."
            )

        logger.debug("embedding_dimension_ok", table=table_name, column=column_name, dim=actual)
