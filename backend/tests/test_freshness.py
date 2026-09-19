"""Re-reading a posting's page, and what the answer is allowed to change.

The queue refuses to send against a page nobody has read recently
(``backend/tests/test_autopilot.py``); this is the other half — what reading one
does to the database. Three properties, and each of them is a way the check
could quietly become useless:

* an open posting has its ``last_seen_at`` moved, because that is the timestamp
  the freshness rule is written against;
* a closed, archived or vanished posting is marked so, and a vanished one does
  **not** get a fresh timestamp — "it was not there" is not a sighting;
* a page that could not be read changes nothing at all, and a challenge stops
  the pass rather than walking the rest of the list into it.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Vacancy, VacancySource
from app.db.repositories.vacancy import VacancyRepository
from app.services import freshness
from app.sources.base import BaseSource, PostingState, SearchQuery
from app.sources.http import HHChallengedError
from factories import make_vacancy

pytestmark = pytest.mark.db

EXTERNAL_ID = "137500001"
URL = f"https://almaty.hh.kz/vacancy/{EXTERNAL_ID}"
LONG_AGO = datetime(2026, 9, 8, 19, 17, tzinfo=UTC)


class FakeSource(BaseSource):
    """A connector that answers ``recheck`` with whatever the test decided.

    Subclassed rather than mocked so the test exercises the real optional
    capability on ``BaseSource``: a connector that does not implement it returns
    ``None``, and that path is one of the cases below.
    """

    slug = "hh"
    name = "Fake hh"

    def __init__(self, answers: dict[str, PostingState | Exception | None]) -> None:
        super().__init__(http=None)
        self.answers = answers
        self.asked: list[str] = []

    def search(self, query: SearchQuery) -> "object":  # pragma: no cover - never called
        raise NotImplementedError

    async def recheck(self, url: str, external_id: str) -> PostingState | None:
        self.asked.append(external_id)
        answer = self.answers.get(external_id, None)
        if isinstance(answer, Exception):
            raise answer
        return answer


async def _posting(
    session: AsyncSession,
    vacancies: VacancyRepository,
    *,
    external_id: str = EXTERNAL_ID,
) -> None:
    url = f"https://almaty.hh.kz/vacancy/{external_id}"
    result = await vacancies.upsert_by_external_id(
        make_vacancy(f"freshness-{external_id}"),
        source_slug="hh",
        external_id=external_id,
        url=url,
        raw={"_derived": {"external_id": external_id, "url": url}},
    )
    await session.execute(
        Vacancy.__table__.update()
        .where(Vacancy.id == result.vacancy_id)
        .values(last_seen_at=LONG_AGO)
    )
    await session.flush()


async def _row(session: AsyncSession, external_id: str = EXTERNAL_ID) -> Vacancy:
    found = await session.scalar(
        select(Vacancy)
        .join(VacancySource, VacancySource.vacancy_id == Vacancy.id)
        .where(VacancySource.external_id == external_id)
    )
    assert found is not None
    return found


async def _ids(session: AsyncSession) -> list[object]:
    return list((await session.execute(select(Vacancy.id))).scalars().all())


async def test_an_open_posting_gets_a_fresh_timestamp(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    await _posting(db_session, vacancies)
    source = FakeSource({EXTERNAL_ID: PostingState(source_slug="hh", external_id=EXTERNAL_ID)})

    outcome = await freshness.refresh(db_session, await _ids(db_session), source=source)

    assert (outcome.checked, outcome.open_for_applications, outcome.closed) == (1, 1, 0)
    row = await _row(db_session)
    assert row.is_active is True
    assert row.last_seen_at > LONG_AGO


async def test_an_archived_posting_is_marked_inactive(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    await _posting(db_session, vacancies)
    source = FakeSource(
        {EXTERNAL_ID: PostingState(source_slug="hh", external_id=EXTERNAL_ID, archived=True)}
    )

    outcome = await freshness.refresh(db_session, await _ids(db_session), source=source)

    assert outcome.closed == 1
    assert outcome.notes == [f"{EXTERNAL_ID}: вакансия в архиве"]
    assert (await _row(db_session)).is_active is False


async def test_a_posting_closed_for_applicants_is_written_into_the_derived_block(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """The flag the queue's card reads, refreshed where the crawler wrote it."""
    await _posting(db_session, vacancies)
    source = FakeSource(
        {
            EXTERNAL_ID: PostingState(
                source_slug="hh", external_id=EXTERNAL_ID, closed_for_applicants=True
            )
        }
    )

    await freshness.refresh(db_session, await _ids(db_session), source=source)

    stored = await db_session.scalar(
        select(VacancySource.raw).where(VacancySource.external_id == EXTERNAL_ID)
    )
    assert stored is not None
    assert stored["_derived"]["closed_for_applicants"] is True
    assert "rechecked_at" in stored["_derived"]


async def test_a_page_that_is_gone_keeps_its_old_timestamp(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """ "It was not there" is not a sighting, and must not read as a fresh check."""
    await _posting(db_session, vacancies)
    source = FakeSource(
        {EXTERNAL_ID: PostingState(source_slug="hh", external_id=EXTERNAL_ID, gone=True)}
    )

    await freshness.refresh(db_session, await _ids(db_session), source=source)

    row = await _row(db_session)
    assert row.is_active is False
    assert row.last_seen_at == LONG_AGO


async def test_a_source_that_cannot_re_read_changes_nothing_and_says_so(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """The capability is optional, and "cannot answer" is not "it is open"."""
    await _posting(db_session, vacancies)
    source = FakeSource({EXTERNAL_ID: None})

    outcome = await freshness.refresh(db_session, await _ids(db_session), source=source)

    assert (outcome.unsupported, outcome.checked) == (1, 0)
    assert (await _row(db_session)).last_seen_at == LONG_AGO


async def test_a_challenge_stops_the_pass_instead_of_walking_into_it(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """hh has decided something about this crawler; the rest of the list waits."""
    await _posting(db_session, vacancies, external_id="137500002")
    await _posting(db_session, vacancies, external_id="137500003")
    source = FakeSource(
        {
            "137500002": HHChallengedError(
                "проверка на робота", host="almaty.hh.kz", path="/vacancy/137500002"
            ),
            "137500003": PostingState(source_slug="hh", external_id="137500003"),
        }
    )

    outcome = await freshness.refresh(db_session, await _ids(db_session), source=source)

    assert source.asked == ["137500002"]
    assert outcome.stopped is not None
    assert outcome.checked == 0
    assert (await _row(db_session, "137500003")).last_seen_at == LONG_AGO


async def test_an_unreadable_page_is_counted_and_the_pass_carries_on(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """One odd page must not cost the rest of the batch its re-read."""
    from app.core.exceptions import SourceError

    await _posting(db_session, vacancies, external_id="137500004")
    await _posting(db_session, vacancies, external_id="137500005")
    source = FakeSource(
        {
            "137500004": SourceError("разметка изменилась"),
            "137500005": PostingState(source_slug="hh", external_id="137500005"),
        }
    )

    outcome = await freshness.refresh(db_session, await _ids(db_session), source=source)

    assert (outcome.unreadable, outcome.checked) == (1, 1)
    assert (await _row(db_session, "137500004")).last_seen_at == LONG_AGO
    assert (await _row(db_session, "137500005")).last_seen_at > LONG_AGO


async def test_nothing_to_re_read_asks_nothing(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    source = FakeSource({})

    outcome = await freshness.refresh(db_session, [], source=source)

    assert (source.asked, outcome.checked) == ([], 0)


async def test_a_re_read_does_not_rewrite_what_the_crawl_stored(
    db_session: AsyncSession, vacancies: VacancyRepository
) -> None:
    """A check run to decide whether to apply must not touch the posting itself.

    The description is what the embeddings were computed from and the title is
    what somebody has already read on a card.
    """
    await _posting(db_session, vacancies)
    before = await _row(db_session)
    title, description = before.title, before.description_raw
    source = FakeSource({EXTERNAL_ID: PostingState(source_slug="hh", external_id=EXTERNAL_ID)})

    await freshness.refresh(db_session, await _ids(db_session), source=source)

    after = await _row(db_session)
    assert (after.title, after.description_raw) == (title, description)


def test_the_window_comes_from_configuration() -> None:
    """A number a person moves in ``.env``, not one spelled into the check."""
    from app.core.config import settings

    assert settings.agent_page_freshness_hours >= 1
    assert timedelta(hours=settings.agent_page_freshness_hours) <= timedelta(days=7)
