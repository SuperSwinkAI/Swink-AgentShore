"""Shared budget policy constants and helpers."""

from __future__ import annotations

MIN_ENABLED_BUDGET_USD = 20.0
BUDGET_DRAIN_RESERVE_USD = 5.0


def budget_reserve_threshold(total_budget: float) -> float:
    """Return the spend level where AgentShore should stop assigning new work."""
    return max(0.0, total_budget - BUDGET_DRAIN_RESERVE_USD)


def budget_reserve_reached(*, spent: float, total_budget: float) -> bool:
    """Return True when known spend is inside the final reserve window."""
    return spent >= budget_reserve_threshold(total_budget)
