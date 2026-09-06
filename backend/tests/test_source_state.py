"""Where a crawl got to, and the two ways storing that goes wrong.

The table exists for one source today, and for one reason: hh's corpus is
roughly fourteen thousand pages per city, so a run covers a slice and has to be
able to say where the next run should carry on from. Everything below is about
that sentence being true after a crash, and after two sources write at once.

**A position is per key, and the key belongs to the connector.** hh keys one
row per sitemap file, because a sitemap file is what its timestamps are grouped
by. A store that quietly shared one row between files would let a busy file's
position hide every entry of a quiet one — and the symptom is not an error, it
is a city that stops producing vacancies.

**A write replaces rather than merges**, because the connector owns the value
and a merge would carry a field its author dropped forward for as long as the
row lived.

What these tests do NOT establish is the other half of that sentence in the
repository's own docstring: that the write is a single statement, so two slices
of one crawl recording neighbouring keys cannot lose one another. Every test
below is a sequential await on one session, and a read-modify-write
implementation would satisfy all of them. That property is visible in the code —
one ``INSERT ... ON CONFLICT DO UPDATE`` — and proving it needs two concurrent
sessions, which is a different test than any of these and is not written yet.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SourceState
from app.db.repositories.source_state import SourceStateRepository

pytestmark = pytest.mark.db

KEY = "sitemap:almaty.hh.kz:vacancy0"
OTHER = "sitemap:almaty.hh.kz:vacancy1"


@pytest.fixture
def positions(db_session: AsyncSession) -> SourceStateRepository:
    """The crawl-position store."""
    return SourceStateRepository(db_session)


async def test_a_key_that_was_never_written_reads_as_nothing(
    positions: SourceStateRepository,
) -> None:
    """A first run has no position, and saying so must not need a row to exist."""
    assert await positions.get("hh", KEY) is None


async def test_a_position_survives_the_round_trip(positions: SourceStateRepository) -> None:
    """Including the shape hh actually stores: a timestamp and the ids tied to it."""
    value = {"lastmod": "2026-09-06T10:42:14+03:00", "ids_at_lastmod": ["136773120"]}

    await positions.set("hh", KEY, value)

    assert await positions.get("hh", KEY) == value


async def test_a_second_write_replaces_rather_than_merges(
    positions: SourceStateRepository,
) -> None:
    """The connector owns the value, so it decides what a position no longer has.

    A merge would carry a dropped field forward for as long as the row lived,
    and the next reader would believe it.
    """
    await positions.set("hh", KEY, {"lastmod": "2026-09-01T00:00:00+03:00", "extra": 1})
    await positions.set("hh", KEY, {"lastmod": "2026-09-06T10:42:14+03:00"})

    assert await positions.get("hh", KEY) == {"lastmod": "2026-09-06T10:42:14+03:00"}


async def test_two_keys_of_one_source_do_not_share_a_row(
    positions: SourceStateRepository, db_session: AsyncSession
) -> None:
    """Per sitemap file, never global — the property the incremental crawl rests on."""
    await positions.set("hh", KEY, {"lastmod": "2026-09-06T10:00:00+03:00"})
    await positions.set("hh", OTHER, {"lastmod": "2026-09-01T00:00:00+03:00"})

    assert (await positions.get("hh", KEY))["lastmod"].startswith("2026-09-06")
    assert (await positions.get("hh", OTHER))["lastmod"].startswith("2026-09-01")
    rows = (await db_session.execute(select(SourceState))).scalars().all()
    assert len(rows) == 2


async def test_two_sources_do_not_share_a_key(positions: SourceStateRepository) -> None:
    """The slug is half the primary key, so a shared key name cannot collide."""
    await positions.set("hh", KEY, {"lastmod": "2026-09-06T10:00:00+03:00"})
    await positions.set("telegram", KEY, {"last_message_id": 42})

    assert await positions.get("telegram", KEY) == {"last_message_id": 42}
    assert await positions.get("hh", KEY) == {"lastmod": "2026-09-06T10:00:00+03:00"}


async def test_an_empty_value_is_stored_and_read_back_as_empty(
    positions: SourceStateRepository,
) -> None:
    """Not None: "written, and it says nothing" is different from "never written".

    The connector treats an unreadable or absent position as "start from the
    beginning", so the two have to stay distinguishable at this level or that
    decision is made here by accident.
    """
    await positions.set("hh", KEY, {})

    assert await positions.get("hh", KEY) == {}
