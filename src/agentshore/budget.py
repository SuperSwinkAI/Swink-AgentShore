"""Shared budget policy constants and helpers.

Two independent soft-cap dimensions guard a session:

* **Dollars** — ``total`` USD with a ``BUDGET_DRAIN_RESERVE_USD`` graceful-drain
  reserve. Stop assigning new work once spend enters the reserve window.
* **Wall-clock time** — ``total_minutes`` with a ``TIME_BUDGET_DRAIN_RESERVE_MINUTES``
  reserve. Stop assigning new work once elapsed enters the reserve window.

Whichever reserve is reached first triggers the same graceful drain; in-flight
agents finish, no new dispatch. A deadline hard-stop backstops each dimension.
"""

from __future__ import annotations

import re

MIN_ENABLED_BUDGET_USD = 20.0
BUDGET_DRAIN_RESERVE_USD = 5.0

# Wall-clock time budget: validated 1h–72h when enabled, 20-minute graceful drain.
MIN_TIME_BUDGET_MINUTES = 60
MAX_TIME_BUDGET_MINUTES = 4320
TIME_BUDGET_DRAIN_RESERVE_MINUTES = 20.0


def budget_reserve_threshold(total_budget: float) -> float:
    """Return the spend level where AgentShore should stop assigning new work."""
    return max(0.0, total_budget - BUDGET_DRAIN_RESERVE_USD)


def budget_reserve_reached(*, spent: float, total_budget: float) -> bool:
    """Return True when known spend is inside the final reserve window."""
    return spent >= budget_reserve_threshold(total_budget)


def time_budget_reserve_threshold(total_minutes: float) -> float:
    """Return the elapsed-minutes level where AgentShore should begin draining."""
    return max(0.0, total_minutes - TIME_BUDGET_DRAIN_RESERVE_MINUTES)


def time_budget_reserve_reached(*, elapsed_minutes: float, total_minutes: float) -> bool:
    """Return True when elapsed wall-clock time is inside the final reserve window."""
    return elapsed_minutes >= time_budget_reserve_threshold(total_minutes)


_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([hm]?)\s*$", re.IGNORECASE)


def parse_duration(text: str) -> int:
    """Parse a human duration into whole minutes, range-checked to 1h–72h.

    Accepts ``"1h"``, ``"24h"``, ``"72h"``, ``"90m"``, and bare minutes
    (``"120"``). Hours may be fractional (``"1.5h"``). Raises :class:`ValueError`
    for an unparseable string or a value outside ``MIN_TIME_BUDGET_MINUTES`` …
    ``MAX_TIME_BUDGET_MINUTES``.
    """
    match = _DURATION_RE.match(text or "")
    if match is None:
        raise ValueError(
            f"invalid duration {text!r}; use e.g. '24h', '90m', or a number of minutes"
        )
    value = float(match.group(1))
    unit = match.group(2).lower()
    minutes_f = value * 60.0 if unit == "h" else value
    minutes = int(round(minutes_f))
    if minutes < MIN_TIME_BUDGET_MINUTES or minutes > MAX_TIME_BUDGET_MINUTES:
        raise ValueError(
            f"time budget must be between {MIN_TIME_BUDGET_MINUTES} and "
            f"{MAX_TIME_BUDGET_MINUTES} minutes (1h–72h), got {minutes} minutes"
        )
    return minutes
