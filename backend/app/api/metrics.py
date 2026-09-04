"""What this process has spent on LLM calls since it started.

The endpoint reports three separate totals and never one combined figure.
``measured`` is money that will appear on an invoice; ``subscription`` is quota
from a plan that is already paid for, priced as if it were being sold; and
``estimated`` is a guess. Adding them produces a number that is neither the
bill nor the usage, and it would be trusted anyway because it looks like money.
"""

from fastapi import APIRouter

from app.llm.usage import UsageTotals, ledger
from app.schemas.metrics import MetricsResponse, UsageSlice

router = APIRouter(tags=["metrics"])


def _slice(totals: UsageTotals) -> UsageSlice:
    """One row of the report."""
    return UsageSlice(
        calls=totals.calls,
        input_tokens=totals.input_tokens,
        output_tokens=totals.output_tokens,
        cache_read_tokens=totals.cache_read_tokens,
        cache_write_tokens=totals.cache_write_tokens,
        cost_usd=round(totals.cost_usd, 6),
        duration_ms=round(totals.duration_ms, 1),
        has_unpriced=totals.has_unpriced,
    )


@router.get(
    "/metrics",
    response_model=MetricsResponse,
    summary="LLM usage and cost since this process started",
)
async def read_metrics() -> MetricsResponse:
    """Report LLM usage, sliced and kept in separate currencies."""
    return MetricsResponse(
        invoiced_usd=round(ledger.invoiced_usd, 6),
        subscription_usd=round(ledger.subscription_usd, 6),
        by_accounting={key: _slice(value) for key, value in ledger.by_accounting.items()},
        by_provider={key: _slice(value) for key, value in ledger.by_provider.items()},
        by_task={key: _slice(value) for key, value in ledger.by_task.items()},
        by_model={key: _slice(value) for key, value in ledger.by_model.items()},
    )
