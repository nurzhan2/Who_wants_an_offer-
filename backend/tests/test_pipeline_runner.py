"""The decisions a crawl makes before, during and after fetching.

The orchestration itself is thin; what is worth defending is the judgement
around it.

**A source that breaks must not break the run.** The run finishes ``partial``,
not ``failed``, because ``last_successful`` counts partial runs as a watermark —
marking the whole crawl failed would reset it and force a full re-crawl of every
source next time, punishing the ones that worked.

**Waiting is not failing.** Cooling down and out-of-credits are ordinary states
with a time attached, not errors, and ``force`` may skip a short cooldown but
never a long one: "не отключать rate limiting «чтобы быстрее»" applies to a
person in a hurry as much as to a loop.

**Minimal normalisation is still normalisation.** A fingerprint is required by
the schema, so it is computed here, and it is computed timidly: a city goes into
the key when the source stated one in a structured field and never when it has
to be guessed out of a free-text line, because a wrong city merges two different
jobs into one row and the second job is then simply gone.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from app.db.enums import PipelineRunStatus, VacancyCompleteness
from app.db.repositories.source_quota import SourceQuotaRepository
from app.normalize.fingerprint import VERSION as FINGERPRINT_VERSION
from app.normalize.fingerprint import fingerprint
from app.pipeline.embedding import EmbeddingOutcome, text_hash
from app.pipeline.runner import RunReport, SourceOutcome, _waiting_reason, to_vacancy
from app.sources.base import BaseSource, RateLimit, RawPosting, SearchQuery, SourceUnavailable
from app.sources.query_planner import QueryPlan

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def posting(
    external_id: str,
    *,
    title: str = "Backend Engineer",
    company: str | None = "Acme",
    description: str | None = "Python, FastAPI.",
    derived: dict[str, object] | None = None,
) -> RawPosting:
    """One posting from a fake source.

    ``derived`` fills ``raw["_derived"]``, the block a connector writes what it
    worked out into. It is the only channel a city reaches the runner through.
    """
    return RawPosting(
        source_slug="fake",
        external_id=external_id,
        url=f"https://example.test/{external_id}",
        title=title,
        company=company,
        description=description,
        raw={"_derived": derived} if derived is not None else {},
    )


class FakeSource(BaseSource):
    """A source that yields what it was handed."""

    slug = "fake"
    name = "Fake"
    # The ceiling the model allows; a fake source should never be the thing
    # that slows a test down.
    rate_limit = RateLimit(requests_per_second=50.0, burst=100)

    def __init__(self, postings: list[RawPosting] | None = None) -> None:
        super().__init__()
        self._postings = postings or []
        self.calls = 0

    async def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]:
        """Yield the canned postings once per query."""
        self.calls += 1
        for item in self._postings:
            yield item


class Cooling(FakeSource):
    """A source whose terms cap it at six hours between runs."""

    slug = "cooling"
    name = "Cooling"
    min_interval = timedelta(hours=6)


class Short(FakeSource):
    """A source with a cooldown short enough that a person may override it."""

    slug = "short"
    name = "Short"
    min_interval = timedelta(minutes=30)


class Metered(FakeSource):
    """A source billed per request."""

    slug = "metered"
    name = "Metered"
    daily_quota = 100


# ── minimal normalisation ─────────────────────────────────────────────


def test_a_posting_gets_the_fingerprint_the_column_demands() -> None:
    """vacancy.fingerprint is NOT NULL and UNIQUE, so nothing can be written
    without one — which is why this cannot wait for phase 4."""
    created = to_vacancy(posting("1"))

    assert created.fingerprint == fingerprint(company="Acme", title="Backend Engineer", city=None)
    assert len(created.fingerprint) == 40
    assert created.fingerprint_version == FINGERPRINT_VERSION


def test_a_source_that_states_no_city_gets_no_city() -> None:
    """A guessed city changes the fingerprint, and a wrong fingerprint either
    splits one job in two — annoying, repairable — or merges two different jobs
    into one row, where the second one's title, salary and link are overwritten
    and no later pass can tell anything was lost."""
    created = to_vacancy(posting("1"))
    assert created.city is None


def test_a_stated_city_reaches_both_the_column_and_the_key() -> None:
    """A connector that reads a city off a structured field is not guessing, so
    its value is used — in the column and in the fingerprint alike. A row keyed
    apart by a city its own column does not show is a row nobody can explain."""
    created = to_vacancy(posting("1", derived={"city": "Алматы"}))

    assert created.city == "Алматы"
    assert created.fingerprint == fingerprint(
        company="Acme", title="Backend Engineer", city="Алматы"
    )


def test_one_employer_advertising_in_two_cities_is_two_vacancies() -> None:
    """The loss version 2 of the fingerprint exists to stop: without the city
    these two hashed together, the second overwrote the first in place, and the
    vacancy count read low with nothing recording that a posting had gone."""
    almaty = to_vacancy(posting("a", company="Магнум", derived={"city": "Алматы"}))
    astana = to_vacancy(posting("b", company="Магнум", derived={"city": "Астана"}))

    assert almaty.fingerprint != astana.fingerprint


def test_a_free_text_location_is_not_treated_as_a_city() -> None:
    """JSearch derives a ``location`` line that runs city, region and country
    together. Cutting a city out of it is phase 4's job; hashing the whole line
    would key one job differently on every source that spells its place its own
    way."""
    created = to_vacancy(posting("1", derived={"location": "Алматы, Казахстан"}))

    assert created.city is None
    assert created.fingerprint == fingerprint(company="Acme", title="Backend Engineer", city=None)


def test_the_same_job_from_two_publishers_hashes_the_same() -> None:
    """This is what collapses a cross-posted job into one vacancy with two
    provenance rows instead of two rows competing in the dashboard."""
    indeed = to_vacancy(posting("a", title="Senior Engineer", company="CPI Card Group"))
    monster = to_vacancy(posting("b", title="Senior Engineer", company="CPI Card Group"))

    assert indeed.fingerprint == monster.fingerprint


def test_a_posting_with_no_description_is_recorded_as_a_stub() -> None:
    """Scoring a headline against a full posting compares an advertisement with
    a paragraph, so matching has to be able to tell them apart."""
    assert to_vacancy(posting("1")).completeness is VacancyCompleteness.FULL
    assert to_vacancy(posting("2", description=None)).completeness is VacancyCompleteness.STUB


# ── the status a run reports ──────────────────────────────────────────


def test_a_source_that_worked_reports_success() -> None:
    """The baseline the other two are read against."""
    assert SourceOutcome(slug="fake", found=5).status is PipelineRunStatus.SUCCESS


def test_a_source_that_broke_after_fetching_reports_partial() -> None:
    """Partial rather than failed, because last_successful() treats a partial
    run as a watermark: calling it failed would reset the incremental window and
    force a full re-crawl next time."""
    outcome = SourceOutcome(slug="fake", found=5, errors=[{"stage": "crawl"}])
    assert outcome.status is PipelineRunStatus.PARTIAL


def test_a_source_that_broke_with_nothing_to_show_reports_failed() -> None:
    """Nothing arrived, so there is no watermark to preserve."""
    outcome = SourceOutcome(slug="fake", errors=[{"stage": "crawl"}])
    assert outcome.status is PipelineRunStatus.FAILED


def test_the_report_adds_up_across_sources() -> None:
    """The run-level counters are what a person reads to decide whether the
    crawl was worth making."""
    report = RunReport(plan=QueryPlan())
    report.sources = [
        SourceOutcome(slug="a", found=10, new=6, duplicates=2),
        SourceOutcome(slug="b", found=5, new=1, duplicates=1),
    ]
    assert (report.found, report.new, report.duplicates) == (15, 7, 3)


# ── who is allowed to run ─────────────────────────────────────────────


class _Quotas(SourceQuotaRepository):
    """A ledger with a fixed answer, so scheduling is testable without a database."""

    def __init__(self, used: int = 0) -> None:
        self._used = used

    async def remaining(self, source_slug, quota, *, day=None):  # type: ignore[no-untyped-def]
        """How many requests are left under the fake ledger."""
        return None if quota is None else max(0, quota - self._used)


class _Run:
    """Just enough of a PipelineRun row for the scheduler to read."""

    def __init__(self, started_at: datetime) -> None:
        self.started_at = started_at


async def test_a_source_inside_its_cooldown_is_told_to_wait() -> None:
    """Waiting is an ordinary state with a time attached, not a failure — and
    remotive's own terms cap us at roughly four requests a day."""
    reason = await _waiting_reason(
        Cooling(), _Run(NOW - timedelta(hours=1)), _Quotas(), force=False, now=NOW
    )

    assert reason is not None
    assert reason.code is SourceUnavailable.COOLING_DOWN
    assert reason.retry_after is not None


async def test_a_source_past_its_cooldown_may_run() -> None:
    """The other half: the interval has to actually expire."""
    assert (
        await _waiting_reason(
            Cooling(), _Run(NOW - timedelta(hours=7)), _Quotas(), force=False, now=NOW
        )
        is None
    )


async def test_a_source_that_has_never_run_may_run() -> None:
    """No history is not a reason to wait."""
    assert await _waiting_reason(Cooling(), None, _Quotas(), force=False, now=NOW) is None


async def test_force_skips_a_short_cooldown_but_not_a_long_one() -> None:
    """A person in a hurry may override our own politeness margin. They may not
    override a limit the vendor published — that is the rule about not turning
    off rate limiting to go faster, and it does not have an exception for
    someone clicking the button."""
    recent = _Run(NOW - timedelta(minutes=1))

    assert await _waiting_reason(Short(), recent, _Quotas(), force=True, now=NOW) is None
    assert await _waiting_reason(Cooling(), recent, _Quotas(), force=True, now=NOW) is not None


async def test_an_exhausted_allowance_stops_a_source_without_failing_the_run() -> None:
    """Out of credits is a state, not an error: the run carries on with the
    other sources and this one comes back tomorrow."""
    reason = await _waiting_reason(Metered(), None, _Quotas(used=100), force=False, now=NOW)

    assert reason is not None
    assert reason.code is SourceUnavailable.QUOTA_EXHAUSTED


async def test_force_does_not_buy_credits() -> None:
    """There is nothing to override: the allowance belongs to the vendor."""
    reason = await _waiting_reason(Metered(), None, _Quotas(used=100), force=True, now=NOW)
    assert reason is not None
    assert reason.code is SourceUnavailable.QUOTA_EXHAUSTED


# ── the embedding step's change detection ─────────────────────────────


def test_the_text_hash_moves_only_when_the_text_does() -> None:
    """This is the whole basis of not re-encoding: every re-crawl rewrites
    updated_at whether or not a word changed, so "the row was touched" cannot be
    the question."""
    assert text_hash("Role: Backend") == text_hash("Role: Backend")
    assert text_hash("Role: Backend") != text_hash("Role: Backend Engineer")


def test_an_unavailable_model_is_reported_rather_than_raised() -> None:
    """sentence-transformers is an optional extra CI does not install, and a
    crawl that succeeded must not be reported as failed because the vectors
    could not be computed — they are recomputed on the next run."""
    outcome = EmbeddingOutcome(
        considered=10, unchanged=0, embedded=0, skipped_reason="runtime not installed"
    )
    assert outcome.embedded == 0
    assert outcome.skipped_reason
