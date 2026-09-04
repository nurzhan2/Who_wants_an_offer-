"""Where every LLM call is counted.

One rule shapes this module: **subscription quota and invoiced dollars are not
the same currency and are never added together.** A CLI call reports
``total_cost_usd`` because Claude Code prices the tokens it used, but nobody is
sending an invoice for it — it comes out of a plan that is already paid for.
Summing that with API spend produces a number that is neither the bill nor the
quota, and it will be believed anyway because it looks like money.

So the ledger keys on ``accounting`` and reports the three totals separately.
``/metrics`` shows them side by side, never combined.
"""

from dataclasses import dataclass, field

from app.llm.base import Accounting, LLMTask, LLMUsage


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """Summed usage for one slice of the ledger."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: float = 0.0
    #: True when at least one call in this slice had no configured price, so
    #: ``cost_usd`` is a floor rather than the total.
    has_unpriced: bool = False

    def plus(self, usage: LLMUsage) -> "UsageTotals":
        """This slice with one more call folded in."""
        return UsageTotals(
            calls=self.calls + 1,
            input_tokens=self.input_tokens + usage.input_tokens,
            output_tokens=self.output_tokens + usage.output_tokens,
            cache_read_tokens=self.cache_read_tokens + usage.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + usage.cache_write_tokens,
            cost_usd=self.cost_usd + (usage.cost_usd or 0.0),
            duration_ms=self.duration_ms + usage.duration_ms,
            has_unpriced=self.has_unpriced or usage.cost_usd is None,
        )


@dataclass
class UsageLedger:
    """Running totals for the process, sliced every way /metrics needs.

    Deliberately in memory and deliberately not persisted. It answers "what has
    this process spent since it started", which is what a metrics endpoint is
    for; durable per-run accounting belongs in ``pipeline_run``.
    """

    by_accounting: dict[Accounting, UsageTotals] = field(default_factory=dict)
    by_provider: dict[str, UsageTotals] = field(default_factory=dict)
    by_task: dict[str, UsageTotals] = field(default_factory=dict)
    by_model: dict[str, UsageTotals] = field(default_factory=dict)
    #: (task, accounting) -> totals. Kept because the two-dimensional split is
    #: the only one that can answer "what would this task cost at volume, and in
    #: which currency". Projecting from the process-wide accounting shares looks
    #: right and is not: a ledger holding one subscription call and one API call
    #: would report every task as half quota and half invoice, whatever the task
    #: actually used.
    by_task_accounting: dict[tuple[str, Accounting], UsageTotals] = field(default_factory=dict)

    def record(self, usage: LLMUsage) -> None:
        """Fold one call into every slice."""
        self.by_accounting[usage.accounting] = self.by_accounting.get(
            usage.accounting, UsageTotals()
        ).plus(usage)
        self.by_provider[usage.provider] = self.by_provider.get(usage.provider, UsageTotals()).plus(
            usage
        )
        self.by_task[usage.task.value] = self.by_task.get(usage.task.value, UsageTotals()).plus(
            usage
        )
        key = (usage.task.value, usage.accounting)
        self.by_task_accounting[key] = self.by_task_accounting.get(key, UsageTotals()).plus(usage)
        if usage.model:
            self.by_model[usage.model] = self.by_model.get(usage.model, UsageTotals()).plus(usage)

    def reset(self) -> None:
        """Forget everything. For tests, and for a fresh pipeline run."""
        self.by_accounting.clear()
        self.by_provider.clear()
        self.by_task.clear()
        self.by_task_accounting.clear()
        self.by_model.clear()

    @property
    def invoiced_usd(self) -> float:
        """Dollars someone will actually bill for.

        Only ``measured``. Estimates are guesses and subscription usage is
        quota; either one added here would make this number wrong in the
        direction that matters.
        """
        return self.by_accounting.get("measured", UsageTotals()).cost_usd

    @property
    def subscription_usd(self) -> float:
        """Quota consumed, priced as if it were being sold. Not a bill."""
        return self.by_accounting.get("subscription", UsageTotals()).cost_usd


@dataclass(frozen=True, slots=True)
class TaskCostEstimate:
    """What one task would cost at a given volume, per accounting bucket."""

    task: LLMTask
    calls: int
    invoiced_usd: float
    subscription_usd: float


#: The process-wide ledger. Providers do not touch it; the caller that owns the
#: operation records the usage it got back, so a single logical operation made
#: of several calls is still attributable to that operation.
ledger = UsageLedger()


def record(usage: LLMUsage) -> LLMUsage:
    """Count a call and hand the usage straight back, so callers can chain."""
    ledger.record(usage)
    return usage


def project(task: LLMTask, calls: int) -> TaskCostEstimate:
    """What this task's observed average would come to over ``calls`` calls.

    The arithmetic that decides routing. A CLI call carries about 50,000 tokens
    of fixed overhead, so "cheap per call" and "cheap at volume" are different
    questions and this answers the second one.
    """
    observed = ledger.by_task.get(task.value, UsageTotals())
    if observed.calls == 0:
        return TaskCostEstimate(task=task, calls=calls, invoiced_usd=0.0, subscription_usd=0.0)

    # This task's own mix, never the ledger's. Reading the process-wide shares
    # here would answer a different question in the same units, which is the
    # currency-mixing this module exists to prevent — and it would do it in the
    # one function whose output decides where a task gets routed.
    def per_call(accounting: Accounting) -> float:
        totals = ledger.by_task_accounting.get((task.value, accounting), UsageTotals())
        return totals.cost_usd / totals.calls if totals.calls else 0.0

    return TaskCostEstimate(
        task=task,
        calls=calls,
        invoiced_usd=per_call("measured") * calls,
        subscription_usd=per_call("subscription") * calls,
    )
