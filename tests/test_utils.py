"""Tests for agentshore.utils — now_iso() — and agentshore.agents.costs — estimate_cost()."""

from __future__ import annotations

from datetime import datetime

from agentshore.agents.costs import estimate_cost
from agentshore.agents.pricing import AgentPricing, PricingQuote
from agentshore.utils import now_iso


def _quote(
    *,
    cost_per_1k_input: float = 0.003,
    cost_per_1k_cached_input: float | None = None,
    cost_per_1k_cache_write_input: float | None = None,
    cost_per_1k_output: float = 0.015,
    cache_read_multiplier: float = 0.1,
    cache_write_multiplier: float = 1.25,
) -> PricingQuote:
    return PricingQuote(
        pricing=AgentPricing(
            max_context=200000,
            cost_per_1k_input=cost_per_1k_input,
            cost_per_1k_cached_input=cost_per_1k_cached_input,
            cost_per_1k_cache_write_input=cost_per_1k_cache_write_input,
            cost_per_1k_output=cost_per_1k_output,
        ),
        cache_read_multiplier=cache_read_multiplier,
        cache_write_multiplier=cache_write_multiplier,
    )


# ---------------------------------------------------------------------------
# now_iso
# ---------------------------------------------------------------------------


def test_now_iso_is_parseable() -> None:
    ts = now_iso()
    dt = datetime.fromisoformat(ts)
    assert dt is not None


def test_now_iso_has_utc_offset() -> None:
    ts = now_iso()
    assert "+00:00" in ts or ts.endswith("Z")


def test_now_iso_is_monotonic() -> None:
    t1 = now_iso()
    t2 = now_iso()
    assert datetime.fromisoformat(t1) <= datetime.fromisoformat(t2)


# ---------------------------------------------------------------------------
# estimate_cost
# ---------------------------------------------------------------------------


def test_estimate_cost_zero_tokens_is_zero() -> None:
    assert estimate_cost(0, 0, _quote()) == 0.0


def test_estimate_cost_output_only() -> None:
    cost = estimate_cost(0, 1000, _quote())
    assert abs(cost - 0.015) < 1e-9


def test_estimate_cost_input_only() -> None:
    cost = estimate_cost(1000, 0, _quote())
    assert abs(cost - 0.003) < 1e-9


def test_estimate_cost_cached_input_cheaper() -> None:
    quote = _quote(cost_per_1k_cached_input=0.0003)
    full_cost = estimate_cost(1000, 0, quote)
    cached_cost = estimate_cost(1000, 0, quote, cached_tokens_in=1000)
    assert cached_cost < full_cost


def test_estimate_cost_cached_multiplier_when_rate_absent() -> None:
    # No explicit cached rate → the quote's cache_read_multiplier (0.1) applies.
    quote = _quote(cost_per_1k_cached_input=None, cache_read_multiplier=0.1)
    full_cost = estimate_cost(1000, 0, quote)
    cached_cost = estimate_cost(1000, 0, quote, cached_tokens_in=1000)
    assert abs(cached_cost - full_cost * 0.1) < 1e-9


def test_estimate_cost_nonnegative_always() -> None:
    assert estimate_cost(-100, -50, _quote()) >= 0.0


def test_estimate_cost_cached_capped_at_total_input() -> None:
    quote = _quote(cost_per_1k_cached_input=0.0003)
    # cached > total_input should be clamped — must not raise or go negative
    cost = estimate_cost(500, 0, quote, cached_tokens_in=1000)
    assert cost >= 0.0


def test_estimate_cost_cache_write_rate_used() -> None:
    quote = _quote(cost_per_1k_cache_write_input=0.00375)
    cost_with_write = estimate_cost(1000, 0, quote, cache_write_tokens_in=1000)
    cost_without = estimate_cost(1000, 0, quote)
    # cache_write rate (0.00375) > normal (0.003) so cost increases
    assert cost_with_write > cost_without
