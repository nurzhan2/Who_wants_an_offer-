"""Shapes for the metrics endpoint."""

from pydantic import BaseModel, Field


class UsageSlice(BaseModel):
    """Summed usage for one provider, task, model or accounting bucket."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: float = 0.0
    #: True when some call in this slice used a model with no configured price,
    #: so the cost is a floor rather than the total.
    has_unpriced: bool = False


class MetricsResponse(BaseModel):
    """LLM usage since the process started.

    The two headline figures are deliberately separate. Adding quota to money
    would give a number that is neither, and it would be believed.
    """

    invoiced_usd: float = Field(description="Money an API will bill for.")
    subscription_usd: float = Field(
        description="Quota consumed from a paid plan, priced as if sold. Not a bill."
    )
    by_accounting: dict[str, UsageSlice] = Field(default_factory=dict)
    by_provider: dict[str, UsageSlice] = Field(default_factory=dict)
    by_task: dict[str, UsageSlice] = Field(default_factory=dict)
    by_model: dict[str, UsageSlice] = Field(default_factory=dict)
