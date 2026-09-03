"""Token accounting and cost, priced from configuration.

Every LLM call reports what it cost. Phase 8 multiplies these numbers by
thousands of vacancies per run, and a per-call figure discovered on the invoice
is discovered too late.
"""

from dataclasses import dataclass

from app.core.config import ModelPricing, settings

MILLION = 1_000_000


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Tokens a single call consumed."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        """Every billable token, cached and not."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        """Accumulate usage across the calls of one operation."""
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


def pricing_for(model: str) -> ModelPricing | None:
    """Price sheet for a model, or None when it is not in the table."""
    return settings.llm_pricing.get(model)


def cost_usd(model: str, usage: TokenUsage) -> float | None:
    """What a call cost, or None when the model has no configured price.

    None rather than zero on purpose: an unpriced model must show up as
    unknown, not as free.
    """
    price = pricing_for(model)
    if price is None:
        return None
    return (
        usage.input_tokens * price.input_usd_per_mtok
        + usage.output_tokens * price.output_usd_per_mtok
        + usage.cache_read_tokens * price.cache_read_usd_per_mtok
        + usage.cache_write_tokens * price.cache_write_usd_per_mtok
    ) / MILLION
