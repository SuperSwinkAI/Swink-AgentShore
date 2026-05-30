"""Agent cost-estimation helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentshore.config import AgentConfig


_DEFAULT_CACHE_READ_MULTIPLIER = 0.1
_DEFAULT_CACHE_WRITE_MULTIPLIER = 1.25


def estimate_cost(
    tokens_in: int,
    tokens_out: int,
    cfg: AgentConfig,
    *,
    cached_tokens_in: int = 0,
    cache_write_tokens_in: int = 0,
) -> float:
    """Estimate dollar cost from token counts using per-agent pricing config."""
    total_input = max(tokens_in, 0)
    cached_input = min(max(cached_tokens_in, 0), total_input)
    cache_write_input = min(max(cache_write_tokens_in, 0), max(total_input - cached_input, 0))
    uncached_input = max(total_input - cached_input - cache_write_input, 0)

    cached_input_rate = (
        cfg.cost_per_1k_cached_input
        if cfg.cost_per_1k_cached_input is not None
        else cfg.cost_per_1k_input * _DEFAULT_CACHE_READ_MULTIPLIER
    )
    cache_write_input_rate = (
        cfg.cost_per_1k_cache_write_input
        if cfg.cost_per_1k_cache_write_input is not None
        else cfg.cost_per_1k_input * _DEFAULT_CACHE_WRITE_MULTIPLIER
    )

    return (
        (uncached_input / 1000) * cfg.cost_per_1k_input
        + (cached_input / 1000) * cached_input_rate
        + (cache_write_input / 1000) * cache_write_input_rate
        + (max(tokens_out, 0) / 1000) * cfg.cost_per_1k_output
    )
