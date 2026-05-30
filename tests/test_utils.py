"""Tests for agentshore.utils — now_iso() — and agentshore.agents.costs — estimate_cost()."""

from __future__ import annotations

from datetime import datetime

from agentshore.agents.costs import estimate_cost
from agentshore.config.models import AgentConfig
from agentshore.utils import now_iso


def _cfg(**kwargs: object) -> AgentConfig:
    return AgentConfig(**kwargs)  # type: ignore[arg-type]


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
    cfg = _cfg(cost_per_1k_input=0.003, cost_per_1k_output=0.015)
    assert estimate_cost(0, 0, cfg) == 0.0


def test_estimate_cost_output_only() -> None:
    cfg = _cfg(cost_per_1k_input=0.003, cost_per_1k_output=0.015)
    cost = estimate_cost(0, 1000, cfg)
    assert abs(cost - 0.015) < 1e-9


def test_estimate_cost_input_only() -> None:
    cfg = _cfg(cost_per_1k_input=0.003, cost_per_1k_output=0.015)
    cost = estimate_cost(1000, 0, cfg)
    assert abs(cost - 0.003) < 1e-9


def test_estimate_cost_cached_input_cheaper() -> None:
    cfg = _cfg(
        cost_per_1k_input=0.003,
        cost_per_1k_cached_input=0.0003,
        cost_per_1k_output=0.015,
    )
    full_cost = estimate_cost(1000, 0, cfg)
    cached_cost = estimate_cost(1000, 0, cfg, cached_tokens_in=1000)
    assert cached_cost < full_cost


def test_estimate_cost_nonnegative_always() -> None:
    cfg = _cfg(cost_per_1k_input=0.003, cost_per_1k_output=0.015)
    assert estimate_cost(-100, -50, cfg) >= 0.0


def test_estimate_cost_cached_capped_at_total_input() -> None:
    cfg = _cfg(cost_per_1k_input=0.003, cost_per_1k_cached_input=0.0003, cost_per_1k_output=0.015)
    # cached > total_input should be clamped — must not raise or go negative
    cost = estimate_cost(500, 0, cfg, cached_tokens_in=1000)
    assert cost >= 0.0


def test_estimate_cost_cache_write_rate_used() -> None:
    cfg = _cfg(
        cost_per_1k_input=0.003,
        cost_per_1k_cache_write_input=0.00375,
        cost_per_1k_output=0.015,
    )
    cost_with_write = estimate_cost(1000, 0, cfg, cache_write_tokens_in=1000)
    cost_without = estimate_cost(1000, 0, cfg)
    # cache_write rate (0.00375) > normal (0.003) so cost increases
    assert cost_with_write > cost_without
