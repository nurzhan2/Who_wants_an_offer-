"""The ledger that must never add quota to money.

Three cost figures pass through this project and only one of them is a bill.
``measured`` is what an API will invoice. ``subscription`` is quota from a plan
that is already paid for — the CLI reports ``total_cost_usd`` because Claude
Code prices the tokens it used, not because anyone is charging for them.
``estimated`` is a guess.

Sum any two of those and the result is neither the bill nor the usage, and it
will be believed anyway because it is printed with a dollar sign. That is the
failure these tests exist to catch: a single "total cost" appearing in
``UsageLedger`` or in ``GET /metrics``, plausible and wrong, quietly steering a
routing decision or a budget conversation.

The second rule here is that an unpriced call must never look free. A model
with no configured price yields ``cost_usd=None``, which means unknown; the
slice records the calls, keeps the cost as a floor, and raises ``has_unpriced``
so the number is read as "at least this much".

No database and no network: the ledger is an in-process dataclass. The
``/metrics`` tests go through the ASGI app in-process.
"""

from collections.abc import Iterator

import pytest
from httpx import AsyncClient

from app.api import metrics as metrics_module
from app.llm.base import Accounting, LLMTask, LLMUsage
from app.llm.usage import UsageLedger, UsageTotals, ledger, project, record


@pytest.fixture(autouse=True)
def clean_ledger() -> Iterator[None]:
    """Empty the process-wide ledger around every test in this module.

    ``ledger`` is a module-level singleton, so without this one test's calls
    would show up in another's totals and the file would pass or fail
    depending on collection order.
    """
    ledger.reset()
    yield
    ledger.reset()


def usage(
    *,
    provider: str = "anthropic_api",
    model: str = "claude-sonnet-4-5",
    task: LLMTask = LLMTask.RERANK,
    cost_usd: float | None = 0.25,
    accounting: Accounting = "measured",
    input_tokens: int = 100,
    output_tokens: int = 20,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    duration_ms: float = 10.0,
) -> LLMUsage:
    """One call's usage, with only the fields a given test cares about.

    Costs default to values that are exact in binary so the assertions can
    compare with ``==`` and still mean what they say.
    """
    return LLMUsage(
        provider=provider,
        model=model,
        task=task,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        accounting=accounting,
    )


# ── recording ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_record_folds_one_call_into_every_slice() -> None:
    """/metrics reports four cuts of the same calls. A call that lands in one
    slice but not another makes the cuts disagree, and the first symptom is a
    per-provider total that does not match the per-task total."""
    fresh = UsageLedger()

    fresh.record(
        usage(
            provider="claude_cli",
            model="claude-opus-4-6",
            task=LLMTask.COVER_LETTER,
            accounting="subscription",
        )
    )

    assert set(fresh.by_accounting) == {"subscription"}
    assert set(fresh.by_provider) == {"claude_cli"}
    assert set(fresh.by_task) == {"cover_letter"}
    assert set(fresh.by_model) == {"claude-opus-4-6"}
    for totals in (
        fresh.by_accounting["subscription"],
        fresh.by_provider["claude_cli"],
        fresh.by_task["cover_letter"],
        fresh.by_model["claude-opus-4-6"],
    ):
        assert totals.calls == 1
        assert totals.input_tokens == 100
        assert totals.output_tokens == 20
        assert totals.cost_usd == 0.25
        assert totals.duration_ms == 10.0


@pytest.mark.unit
def test_by_task_is_keyed_by_a_plain_string_not_the_enum_member() -> None:
    """``by_task`` is declared ``dict[str, UsageTotals]`` and callers index it
    with literals. ``LLMTask`` is a ``StrEnum``, so a member compares equal to
    its value and hashes the same — meaning an equality assertion cannot tell
    the two apart and neither can a passing test suite. The type can: keying by
    the member leaks an ``LLMTask`` into anything that iterates the slice, and
    into every consumer that expected the annotation to be true."""
    fresh = UsageLedger()

    fresh.record(usage(task=LLMTask.TELEGRAM_PARSE))

    key = next(iter(fresh.by_task))
    assert key == "telegram_parse"
    assert type(key) is str


@pytest.mark.unit
def test_repeated_calls_accumulate_rather_than_overwrite() -> None:
    """Totals are running totals. If ``record`` replaced a slice instead of
    folding into it, /metrics would report the last call rather than the day."""
    fresh = UsageLedger()

    fresh.record(usage(cost_usd=0.25, input_tokens=100, output_tokens=20, duration_ms=10.0))
    fresh.record(usage(cost_usd=0.5, input_tokens=300, output_tokens=40, duration_ms=30.0))

    totals = fresh.by_provider["anthropic_api"]
    assert totals.calls == 2
    assert totals.cost_usd == 0.75
    assert totals.input_tokens == 400
    assert totals.output_tokens == 60
    assert totals.duration_ms == 40.0


@pytest.mark.unit
def test_cache_tokens_are_counted_separately_from_fresh_input() -> None:
    """Cache reads are billed at a fraction of input tokens. Folding them into
    ``input_tokens`` would make a well-cached workload look nine times more
    expensive than it is."""
    fresh = UsageLedger()

    fresh.record(usage(input_tokens=100, cache_read_tokens=9_000, cache_write_tokens=500))

    totals = fresh.by_task["rerank"]
    assert totals.input_tokens == 100
    assert totals.cache_read_tokens == 9_000
    assert totals.cache_write_tokens == 500


@pytest.mark.unit
def test_record_returns_the_usage_it_counted() -> None:
    """Callers wrap a result in ``record(...)`` inline. If it returned None the
    wrapping would silently discard the usage it was meant to pass through."""
    one = usage()

    assert record(one) is one
    assert ledger.by_provider["anthropic_api"].calls == 1


@pytest.mark.unit
def test_a_model_free_call_still_counts_everywhere_else() -> None:
    """Some providers report no model name. Such a call is still a call and
    still costs money, so dropping it from ``by_model`` must not drop it from
    the totals that a bill is reconciled against."""
    fresh = UsageLedger()

    fresh.record(usage(model="", provider="ollama", accounting="estimated"))

    assert fresh.by_model == {}
    assert fresh.by_provider["ollama"].calls == 1
    assert fresh.by_accounting["estimated"].calls == 1


# ── the rule: two currencies, never one total ─────────────────────────


@pytest.mark.unit
def test_invoiced_and_subscription_totals_never_contain_each_other() -> None:
    """The assertion this module exists for.

    A CLI call priced at $4 is quota from a plan already paid for; an API call
    priced at $1 is a line on next month's invoice. ``invoiced_usd`` must be
    exactly the $1 and ``subscription_usd`` exactly the $4. The failure mode is
    not a crash — it is $5 appearing somewhere as "spend", which is neither the
    bill nor the quota and which nobody would question."""
    record(usage(provider="anthropic_api", accounting="measured", cost_usd=1.0))
    record(
        usage(
            provider="claude_cli",
            model="claude-opus-4-6",
            task=LLMTask.COVER_LETTER,
            accounting="subscription",
            cost_usd=4.0,
        )
    )

    assert ledger.invoiced_usd == 1.0
    assert ledger.subscription_usd == 4.0
    # Neither figure is the sum, and neither has borrowed from the other.
    assert ledger.invoiced_usd != 5.0
    assert ledger.subscription_usd != 5.0
    assert not hasattr(ledger, "total_usd")


@pytest.mark.unit
def test_estimated_costs_are_neither_invoiced_nor_subscription() -> None:
    """A local model's cost is a guess. Counting a guess as invoiced dollars
    would put an imaginary number on a real budget."""
    record(usage(provider="ollama", model="qwen3:8b", accounting="estimated", cost_usd=2.0))

    assert ledger.invoiced_usd == 0.0
    assert ledger.subscription_usd == 0.0
    assert ledger.by_accounting["estimated"].cost_usd == 2.0


@pytest.mark.unit
def test_headline_totals_are_zero_on_an_untouched_ledger() -> None:
    """Before any call is made the answer is zero, not a KeyError. /metrics is
    routinely scraped on a process that has done no LLM work yet."""
    assert ledger.invoiced_usd == 0.0
    assert ledger.subscription_usd == 0.0


# ── unpriced calls are unknown, not free ──────────────────────────────


@pytest.mark.unit
def test_a_call_with_no_price_raises_has_unpriced() -> None:
    """``cost_usd=None`` means the model has no configured price, so the cost is
    unknown. Reporting $0.00 with no flag would present an unmeasured workload
    as a free one."""
    fresh = UsageLedger()

    fresh.record(usage(model="some-new-model", cost_usd=None))

    totals = fresh.by_model["some-new-model"]
    assert totals.calls == 1
    assert totals.cost_usd == 0.0
    assert totals.has_unpriced is True


@pytest.mark.unit
def test_one_unpriced_call_makes_the_whole_slice_a_floor() -> None:
    """The flag is sticky. A slice holding nine priced calls and one unpriced
    one has a cost that is a lower bound, and a later priced call must not
    clear the flag and turn the floor back into a total."""
    fresh = UsageLedger()

    fresh.record(usage(cost_usd=0.25))
    fresh.record(usage(cost_usd=None))
    fresh.record(usage(cost_usd=0.5))

    totals = fresh.by_provider["anthropic_api"]
    assert totals.calls == 3
    assert totals.cost_usd == 0.75
    assert totals.has_unpriced is True


@pytest.mark.unit
def test_fully_priced_slices_are_not_flagged() -> None:
    """The flag has to mean something. If it were set on every slice, the one
    slice that is genuinely a floor would be indistinguishable."""
    fresh = UsageLedger()

    fresh.record(usage(cost_usd=0.25))
    fresh.record(usage(cost_usd=0.5))

    assert fresh.by_provider["anthropic_api"].has_unpriced is False


@pytest.mark.unit
def test_totals_are_immutable_so_a_slice_cannot_be_edited_in_place() -> None:
    """``UsageTotals`` is frozen and ``plus`` returns a new value. A mutable
    total is a total some other code can quietly adjust."""
    totals = UsageTotals()

    with pytest.raises((AttributeError, TypeError)):
        totals.calls = 7  # type: ignore[misc]

    assert totals.plus(usage()) is not totals
    assert totals.calls == 0


# ── reset ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_reset_clears_every_slice() -> None:
    """A fresh pipeline run starts from zero. A slice that survives ``reset``
    carries the previous run's spend into this one's report."""
    record(usage(provider="claude_cli", accounting="subscription", cost_usd=4.0))
    record(usage(provider="anthropic_api", accounting="measured", cost_usd=1.0))

    ledger.reset()

    assert ledger.by_accounting == {}
    assert ledger.by_provider == {}
    assert ledger.by_task == {}
    assert ledger.by_model == {}
    assert ledger.invoiced_usd == 0.0
    assert ledger.subscription_usd == 0.0


# ── projection ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_project_on_an_unobserved_task_is_zero_not_a_guess() -> None:
    """With no measurement there is no average to scale. Inventing one would
    make a routing decision out of nothing."""
    estimate = project(LLMTask.COVER_LETTER, 100)

    assert estimate.task is LLMTask.COVER_LETTER
    assert estimate.calls == 100
    assert estimate.invoiced_usd == 0.0
    assert estimate.subscription_usd == 0.0


@pytest.mark.unit
def test_project_scales_the_observed_per_call_average() -> None:
    """The question this answers is "cheap at volume?", not "cheap once?". Two
    observed API calls averaging $0.375 project to $37.50 over a hundred."""
    record(usage(task=LLMTask.VACANCY_PARSE, accounting="measured", cost_usd=0.25))
    record(usage(task=LLMTask.VACANCY_PARSE, accounting="measured", cost_usd=0.5))

    estimate = project(LLMTask.VACANCY_PARSE, 100)

    assert estimate.invoiced_usd == pytest.approx(37.5)
    assert estimate.subscription_usd == 0.0


@pytest.mark.unit
def test_project_reports_subscription_work_as_quota_not_as_invoiced_dollars() -> None:
    """A projection is where the two currencies are most tempting to merge: the
    number is about to be compared against another provider's. A cover letter
    run on the already-paid-for CLI must project quota and zero dollars, or the
    comparison rejects the free option for costing money."""
    record(
        usage(
            provider="claude_cli",
            model="claude-opus-4-6",
            task=LLMTask.COVER_LETTER,
            accounting="subscription",
            cost_usd=0.5,
        )
    )

    estimate = project(LLMTask.COVER_LETTER, 20)

    assert estimate.invoiced_usd == 0.0
    assert estimate.subscription_usd == pytest.approx(10.0)


@pytest.mark.unit
def test_project_over_zero_calls_costs_nothing() -> None:
    """Routing asks about batch sizes that can be empty. The answer is zero in
    both currencies, not the per-call average."""
    record(usage(task=LLMTask.RERANK, cost_usd=0.25))

    estimate = project(LLMTask.RERANK, 0)

    assert estimate.invoiced_usd == 0.0
    assert estimate.subscription_usd == 0.0


@pytest.mark.unit
def test_project_treats_a_free_observation_as_free() -> None:
    """A local model priced at zero must not be projected into a cost. The
    guard against dividing by an empty ledger must not turn $0 into a fallback
    number."""
    record(usage(provider="ollama", model="qwen3:8b", accounting="estimated", cost_usd=0.0))

    estimate = project(LLMTask.RERANK, 500)

    assert estimate.invoiced_usd == 0.0
    assert estimate.subscription_usd == 0.0


# ── GET /metrics ──────────────────────────────────────────────────────


@pytest.mark.db
async def test_metrics_reports_both_currencies_side_by_side(async_client: AsyncClient) -> None:
    """The endpoint is the place a person actually reads these numbers, so the
    separation has to survive serialisation. One measured dollar and four
    subscription dollars must arrive as two fields, with no third field adding
    them up."""
    record(
        usage(
            provider="anthropic_api",
            model="claude-sonnet-4-5",
            task=LLMTask.RERANK,
            accounting="measured",
            cost_usd=1.0,
        )
    )
    record(
        usage(
            provider="claude_cli",
            model="claude-opus-4-6",
            task=LLMTask.COVER_LETTER,
            accounting="subscription",
            cost_usd=4.0,
        )
    )

    response = await async_client.get("/metrics")

    assert response.status_code == 200
    body = response.json()
    assert body["invoiced_usd"] == 1.0
    assert body["subscription_usd"] == 4.0
    assert 5.0 not in body.values()
    assert body["by_accounting"]["measured"]["cost_usd"] == 1.0
    assert body["by_accounting"]["subscription"]["cost_usd"] == 4.0
    assert body["by_provider"]["anthropic_api"]["calls"] == 1
    assert body["by_provider"]["claude_cli"]["calls"] == 1
    assert set(body["by_task"]) == {"rerank", "cover_letter"}
    assert set(body["by_model"]) == {"claude-sonnet-4-5", "claude-opus-4-6"}


@pytest.mark.db
async def test_metrics_marks_an_unpriced_slice_rather_than_reporting_it_free(
    async_client: AsyncClient,
) -> None:
    """``has_unpriced`` has to reach the reader. Without it the payload says
    $0.25 for two calls and looks like a complete answer.

    The unpriced call is recorded *first* on purpose: a flag that were merely
    copied from the newest call would still be set if the unknown one came
    last, and the test would pass while the endpoint reported a floor as a
    total for every slice that ends on a priced call."""
    record(usage(model="claude-sonnet-4-5", cost_usd=None))
    record(usage(model="claude-sonnet-4-5", cost_usd=0.25))

    response = await async_client.get("/metrics")

    slice_ = response.json()["by_model"]["claude-sonnet-4-5"]
    assert slice_["calls"] == 2
    assert slice_["cost_usd"] == 0.25
    assert slice_["has_unpriced"] is True


@pytest.mark.db
async def test_metrics_on_an_untouched_ledger_returns_zeros(async_client: AsyncClient) -> None:
    """A metrics endpoint is scraped on a process that has done nothing yet.
    Empty slices must render as zeros and empty maps, not as a 500."""
    response = await async_client.get("/metrics")

    assert response.status_code == 200
    assert response.json() == {
        "invoiced_usd": 0.0,
        "subscription_usd": 0.0,
        "by_accounting": {},
        "by_provider": {},
        "by_task": {},
        "by_model": {},
    }


@pytest.mark.db
async def test_metrics_reads_the_shared_ledger_the_providers_write_to(
    async_client: AsyncClient,
) -> None:
    """``app.api.metrics`` imports ``ledger`` by name at module load. If it ever
    took a copy, or a provider recorded into its own instance, the endpoint
    would keep reporting zeros while calls were being made."""
    record(usage(cost_usd=0.25))

    assert (await async_client.get("/metrics")).json()["invoiced_usd"] == 0.25

    record(usage(cost_usd=0.5))

    assert (await async_client.get("/metrics")).json()["invoiced_usd"] == 0.75
    assert metrics_module.ledger is ledger
