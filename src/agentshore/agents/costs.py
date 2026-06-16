"""Agent cost-estimation helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentshore.agents.pricing import PricingQuote


def estimate_cost(
    tokens_in: int,
    tokens_out: int,
    quote: PricingQuote,
    *,
    cached_tokens_in: int = 0,
    cache_write_tokens_in: int = 0,
) -> float:
    """Estimate dollar cost from token counts using a resolved pricing quote.

    ``quote`` carries the per-model :class:`~agentshore.agents.pricing.AgentPricing`
    and the cache read/write multipliers applied when the model omits an explicit
    cached / cache-write rate.
    """
    pricing = quote.pricing
    total_input = max(tokens_in, 0)
    cached_input = min(max(cached_tokens_in, 0), total_input)
    cache_write_input = min(max(cache_write_tokens_in, 0), max(total_input - cached_input, 0))
    uncached_input = max(total_input - cached_input - cache_write_input, 0)

    cached_input_rate = (
        pricing.cost_per_1k_cached_input
        if pricing.cost_per_1k_cached_input is not None
        else pricing.cost_per_1k_input * quote.cache_read_multiplier
    )
    cache_write_input_rate = (
        pricing.cost_per_1k_cache_write_input
        if pricing.cost_per_1k_cache_write_input is not None
        else pricing.cost_per_1k_input * quote.cache_write_multiplier
    )

    return (
        (uncached_input / 1000) * pricing.cost_per_1k_input
        + (cached_input / 1000) * cached_input_rate
        + (cache_write_input / 1000) * cache_write_input_rate
        + (max(tokens_out, 0) / 1000) * pricing.cost_per_1k_output
    )
