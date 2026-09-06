"""``0007_fingerprint_city`` applied to PostgreSQL, not reasoned about.

The companion to ``test_fingerprint.py``. That file drives the migration's
decisions against a fake connection, which is the right tool for enumerating
them; this one runs ``alembic upgrade``, ``alembic downgrade`` and ``alembic
upgrade`` again against a database seeded at the revision below, because three
of the migration's claims are claims about PostgreSQL and cannot be checked
anywhere else:

* that the unique constraint really does stop the upgrade when a crawl reached
  the table first. It does — the first run of this scenario produced
  ``duplicate key value violates unique constraint "uq_vacancy_fingerprint"``
  mid-transaction, which is what the refusal now replaces;
* that a city recovered out of ``vacancy_source.raw`` — real JSONB, real
  ``->>``, a real non-breaking space in the value — is reduced to exactly what
  ``stated_city`` would produce for the same posting, so the backfilled key is
  the key the next crawl computes;
* that the round trip is a round trip: the keys after upgrade → downgrade →
  upgrade are the keys after the first upgrade.

No ``pytestmark``: ``conftest.pytest_collection_modifyitems`` marks anything
that asks for ``scratch_database`` as ``db``, and this module asks for it
everywhere.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from app.normalize.fingerprint import fingerprint
from helpers import alembic_config

#: The revision this migration sits on top of. Seeding happens here, so the
#: rows are exactly what a database that has never run 0007 would hold.
DOWN_REVISION = "0006_source_state"

ALMATY = "Алматы"
CHAIN = "Магнум"
SHOP_JOB = "Продавец-кассир"

#: sha1 of the normalised company and title with an empty third part, which is
#: every key version 1 ever wrote.
VERSION_ONE_KEY = fingerprint(company=CHAIN, title=SHOP_JOB, city=None)
VERSION_TWO_KEY = fingerprint(company=CHAIN, title=SHOP_JOB, city=ALMATY)

#: A key no algorithm here produces. The dev seed invents its fingerprints from
#: an index, and the migration must leave such a row completely alone.
INVENTED_KEY = "0" * 40

#: As a Russian page delivers it. ``btrim`` does not remove U+00A0, so a
#: recovery done in SQL would store — and hash — a different string than the
#: crawler computes for the same posting.
PADDED_CITY = f"\u00a0{ALMATY}\u00a0"

_INSERT_VACANCY = text(
    "INSERT INTO vacancy (id, fingerprint, fingerprint_version, title, company, city, remote,"
    " is_active, completeness, first_seen_at, last_seen_at, created_at, updated_at) VALUES"
    " (:id, :fingerprint, :version, :title, :company, :city, 'no', true, 'stub',"
    " now(), now(), now(), now())"
)

# The city has to be cast explicitly: asyncpg cannot infer a parameter's type
# from jsonb_build_object's signature and refuses the statement without it.
_INSERT_SOURCE = text(
    "INSERT INTO vacancy_source (id, vacancy_id, source_slug, external_id, url, raw,"
    " created_at, updated_at) VALUES (:id, :vacancy_id, 'hh', :external_id, :url,"
    " jsonb_build_object('_derived', jsonb_build_object('city', cast(:city as text))),"
    " now(), now())"
)

_STORED = text(
    "SELECT company, title, city, fingerprint, fingerprint_version AS version"
    " FROM vacancy ORDER BY fingerprint_version, company"
)

_REVISION = text("SELECT version_num FROM alembic_version")


async def _alembic(database_url: str, target: str, *, down: bool = False) -> None:
    """Run one Alembic command from an async test.

    ``helpers.run_alembic`` only ever downgrades to ``base``; this migration has
    to be reversed exactly one revision, so the direction is a parameter.
    """
    action = command.downgrade if down else command.upgrade
    await asyncio.to_thread(action, alembic_config(database_url), target)


async def _seed_vacancy(
    connection: AsyncConnection,
    *,
    key: str,
    version: int,
    city: str | None = None,
    company: str | None = CHAIN,
    title: str = SHOP_JOB,
) -> uuid.UUID:
    """One vacancy row, exactly as the pipeline of that version would have left it."""
    vacancy_id = uuid.uuid4()
    await connection.execute(
        _INSERT_VACANCY,
        {
            "id": vacancy_id,
            "fingerprint": key,
            "version": version,
            "title": title,
            "company": company,
            "city": city,
        },
    )
    return vacancy_id


async def _seed_source(
    connection: AsyncConnection, vacancy_id: uuid.UUID, *, external_id: str, city: str
) -> None:
    """The provenance row a connector wrote, with its derived city in the payload."""
    await connection.execute(
        _INSERT_SOURCE,
        {
            "id": uuid.uuid4(),
            "vacancy_id": vacancy_id,
            "external_id": external_id,
            "url": f"https://example.test/{external_id}",
            "city": city,
        },
    )


async def _stored(connection: AsyncConnection) -> list[dict[str, Any]]:
    """Every vacancy row, in a stable order, as plain dicts."""
    return [dict(row) for row in (await connection.execute(_STORED)).mappings()]


@pytest_asyncio.fixture
async def before_0007(scratch_database: str) -> AsyncIterator[AsyncConnection]:
    """A database migrated to the revision below 0007, ready to be seeded.

    AUTOCOMMIT on purpose: Alembic opens its own connection, and rows left
    uncommitted here would be invisible to the migration under test.
    """
    await _alembic(scratch_database, DOWN_REVISION)
    engine = create_async_engine(scratch_database, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


async def test_the_backfill_re_keys_a_row_and_the_round_trip_returns_it(
    scratch_database: str, before_0007: AsyncConnection
) -> None:
    """The migration's whole purpose, applied and then reversed and re-applied.

    The seeded row is what version 1 left: a chain employer's posting keyed by
    company and title alone, with no city of its own, and a provenance row whose
    payload still holds the place hh read off the page. After the upgrade it
    sits on the key a re-crawl of that posting now computes, which is what stops
    the crawl inserting a second row beside it.

    The invented-key row beside it is the control. Nothing about it may move —
    not the fingerprint, and not the city either, because a row keyed without a
    city that nonetheless names one in its column is a row nobody can explain.
    """
    real = await _seed_vacancy(before_0007, key=VERSION_ONE_KEY, version=1)
    await _seed_source(before_0007, real, external_id="hh-1", city=PADDED_CITY)
    seeded = await _seed_vacancy(before_0007, key=INVENTED_KEY, version=1, company="Small Shop")
    await _seed_source(before_0007, seeded, external_id="hh-2", city=ALMATY)

    await _alembic(scratch_database, "head")

    assert await _stored(before_0007) == [
        {
            "company": "Small Shop",
            "title": SHOP_JOB,
            "city": None,
            "fingerprint": INVENTED_KEY,
            "version": 1,
        },
        {
            "company": CHAIN,
            "title": SHOP_JOB,
            # Reduced the way the running pipeline reduces it: the non-breaking
            # spaces are gone, which no btrim would have managed.
            "city": ALMATY,
            "fingerprint": VERSION_TWO_KEY,
            "version": 2,
        },
    ]

    await _alembic(scratch_database, DOWN_REVISION, down=True)

    back = await _stored(before_0007)
    assert [(row["fingerprint"], row["version"]) for row in back] == [
        (INVENTED_KEY, 1),
        (VERSION_ONE_KEY, 1),
    ]
    # The recovered city stays. It is data a crawl legitimately collected, and
    # dropping it would lose more than the downgrade restores.
    assert back[1]["city"] == ALMATY

    await _alembic(scratch_database, "head")

    assert [(row["fingerprint"], row["version"]) for row in await _stored(before_0007)] == [
        (INVENTED_KEY, 1),
        (VERSION_TWO_KEY, 2),
    ]


async def test_the_upgrade_refuses_when_a_crawl_reached_the_table_first(
    scratch_database: str, before_0007: AsyncConnection
) -> None:
    """The ordinary deployment order, which used to break the deploy.

    Code ships before the migration runs, so the crawl computes version-2 keys
    against an unmigrated table: the chain employer's Almaty postings it re-saw
    got a version-2 row of their own, while the ones that had expired out of the
    sitemap still hang off the old version-1 row with their city in the payload.
    Re-keying that row computes the fingerprint the new one already holds.

    Before the fix this reached PostgreSQL and came back as ``duplicate key
    value violates unique constraint "uq_vacancy_fingerprint"`` from inside
    ``UPDATE vacancy SET fingerprint = ...``. Now it is refused before the first
    write, with a count of what is in the way.
    """
    stale = await _seed_vacancy(before_0007, key=VERSION_ONE_KEY, version=1)
    await _seed_source(before_0007, stale, external_id="hh-expired", city=ALMATY)
    await _seed_vacancy(before_0007, key=VERSION_TWO_KEY, version=2, city=ALMATY)
    before = await _stored(before_0007)

    with pytest.raises(RuntimeError, match="1 version-1 vacancies would be re-keyed onto 1"):
        await _alembic(scratch_database, "head")

    # Refused, not half-applied: the transaction rolled back and the database is
    # still on the revision below.
    assert await _stored(before_0007) == before
    assert await before_0007.scalar(_REVISION) == DOWN_REVISION


async def test_the_downgrade_refuses_when_a_version_one_row_holds_the_key(
    scratch_database: str, before_0007: AsyncConnection
) -> None:
    """The collision that matters most, because ``upgrade`` leaves a mixed table.

    A row whose key version 1 did not write stays at version 1 for good. Drop
    the city off a version-2 row for the same employer and title and it aims
    straight at that untouched row's fingerprint — and unlike the upgrade, the
    downgrade's own docstring promises it will not silently drop a posting to
    make room.
    """
    await _alembic(scratch_database, "head")
    await _seed_vacancy(before_0007, key=VERSION_ONE_KEY, version=1)
    await _seed_vacancy(before_0007, key=VERSION_TWO_KEY, version=2, city=ALMATY)
    before = await _stored(before_0007)

    with pytest.raises(RuntimeError, match="would merge 1 vacancies onto 1"):
        await _alembic(scratch_database, DOWN_REVISION, down=True)

    assert await _stored(before_0007) == before
