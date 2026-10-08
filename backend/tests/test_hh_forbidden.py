"""A 403 on one posting skips it; a run of them stops the walk (8 Oct 2026).

Measured live: 137721276 answered 403 on every attempt while 137625078 answered
200 from the same address a second later. Raising on the first 403 had stopped
the whole night crawl after 28 minutes and 161 postings.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.exceptions import SourceError
from app.sources.hh import FORBIDDEN_IN_A_ROW_STOPS, HHSite, HHSource, SitemapEntry

SITE = HHSite(host="almaty.hh.kz", city="Алматы", country="KZ")


class Http:
    """Answers each request with the next status in line; ``200`` is a page."""

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = list(statuses)

    async def get_text(self, url: str, **_: Any) -> str:
        status = self.statuses.pop(0)
        if status == 200:
            return "<html></html>"
        raise SourceError(f"hh: HTTP {status} на {url}", source_slug="hh", response_status=status)


def entry(n: int) -> SitemapEntry:
    return SitemapEntry(
        external_id=str(n),
        url=f"https://almaty.hh.kz/vacancy/{n}",
        lastmod=datetime(2026, 10, 8, tzinfo=UTC),
    )


def source(statuses: list[int]) -> HHSource:
    src = HHSource()
    src.bind(Http(statuses))  # type: ignore[arg-type]
    src._state = lambda *_: None  # type: ignore[method-assign]
    return src


def fetch(src: HHSource, n: int) -> object:
    return asyncio.run(src._fetch(SITE, entry(n)))


def test_one_forbidden_posting_is_skipped_not_fatal() -> None:
    assert fetch(source([403]), 1) is None


def test_a_run_of_forbidden_postings_stops_the_walk() -> None:
    src = source([403] * FORBIDDEN_IN_A_ROW_STOPS)
    for n in range(FORBIDDEN_IN_A_ROW_STOPS - 1):
        assert fetch(src, n) is None
    with pytest.raises(SourceError):
        fetch(src, 99)


def test_a_page_that_answers_resets_the_count() -> None:
    statuses = (
        [403] * (FORBIDDEN_IN_A_ROW_STOPS - 1) + [200] + [403] * (FORBIDDEN_IN_A_ROW_STOPS - 1)
    )
    src = source(statuses)
    for n in range(len(statuses)):
        assert fetch(src, n) is None


def test_other_refusals_still_stop_at_once() -> None:
    with pytest.raises(SourceError):
        fetch(source([500]), 1)
