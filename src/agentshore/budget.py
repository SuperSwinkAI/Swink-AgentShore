"""Shared budget policy constants, parsing, validation, and serialization.

Two independent soft-cap dimensions guard a session:

* **Dollars** — ``total`` USD with a ``BUDGET_DRAIN_RESERVE_USD`` graceful-drain
  reserve. Stop assigning new work once spend enters the reserve window.
* **Wall-clock time** — ``total_minutes`` with a ``TIME_BUDGET_DRAIN_RESERVE_MINUTES``
  reserve. Stop assigning new work once elapsed enters the reserve window.

Whichever reserve is reached first triggers the same graceful drain; in-flight
agents finish, no new dispatch. A deadline hard-stop backstops each dimension.

This module is the single owner of budget parse / validate / serialize.
``config/_parsers.py``, ``sidecar/project.py``, ``sidecar/server.py``, and
``core/mixins/drain.py`` are all callers — they do not re-implement this logic.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentshore.config.models import BudgetConfig

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


def _parse_duration_core(text: str, *, min_minutes: int | None, max_minutes: int) -> int:
    """Parse a human duration string into whole minutes.

    Accepts ``"1h"``, ``"24h"``, ``"72h"``, ``"90m"``, and bare minutes
    (``"120"``). Hours may be fractional (``"1.5h"``).

    Args:
        text:        The raw string to parse.
        min_minutes: Lower bound (inclusive). ``None`` means no lower bound other
                     than > 0.
        max_minutes: Upper bound (inclusive).

    Raises :class:`ValueError` for unparseable strings or out-of-range values.
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
    if min_minutes is not None and minutes < min_minutes:
        raise ValueError(
            f"time budget must be between {min_minutes} and "
            f"{max_minutes} minutes (1h–72h), got {minutes} minutes"
        )
    if minutes > max_minutes:
        raise ValueError(
            f"time budget must be at most {max_minutes} minutes, got {minutes} minutes"
        )
    if minutes <= 0:
        raise ValueError(f"time delta must be a positive number of minutes, got {minutes}")
    return minutes


def parse_duration(text: str) -> int:
    """Parse a human duration into whole minutes, range-checked to 1h–72h.

    Accepts ``"1h"``, ``"24h"``, ``"72h"``, ``"90m"``, and bare minutes
    (``"120"``). Hours may be fractional (``"1.5h"``). Raises :class:`ValueError`
    for an unparseable string or a value outside ``MIN_TIME_BUDGET_MINUTES`` …
    ``MAX_TIME_BUDGET_MINUTES``.
    """
    return _parse_duration_core(
        text, min_minutes=MIN_TIME_BUDGET_MINUTES, max_minutes=MAX_TIME_BUDGET_MINUTES
    )


def parse_duration_delta(text: str) -> int:
    """Parse a human duration into a positive whole-minute additive delta.

    Like :func:`parse_duration` (same ``"2h"`` / ``"30m"`` / bare-minutes
    grammar, fractional hours allowed) but WITHOUT the 60-minute floor — it is
    an additive *extension* of an existing cap, so ``"30m"`` is valid. Only the
    upper bound (``MAX_TIME_BUDGET_MINUTES``) constrains a single delta; the
    orchestrator re-validates the resulting total against the full 1h–72h band.

    Raises :class:`ValueError` for an unparseable string, a non-positive value,
    or a delta larger than ``MAX_TIME_BUDGET_MINUTES``.
    """
    return _parse_duration_core(text, min_minutes=None, max_minutes=MAX_TIME_BUDGET_MINUTES)


# ---------------------------------------------------------------------------
# Parse / validate a raw YAML budget mapping → BudgetConfig
# ---------------------------------------------------------------------------


def parse_budget_raw(raw: dict[str, object]) -> BudgetConfig:
    """Parse and validate a raw ``budget:`` YAML mapping into a :class:`BudgetConfig`.

    This is the single validator used by ``config/_parsers.py``, ``sidecar/project.py``,
    and any other caller that holds an untrusted dict. Raises
    :class:`agentshore.errors.ConfigError` on invalid values.
    """
    # Import lazily so this module stays torch-free / import-light.
    from agentshore.config.models import BudgetConfig
    from agentshore.errors import ConfigError

    enabled = raw.get("enabled", False)
    total = raw.get("total", 0.0)
    warning = raw.get("warning_threshold", 0.20)
    if not isinstance(enabled, bool):
        raise ConfigError(f"budget.enabled must be a boolean, got {enabled!r}")
    if not isinstance(total, int | float):
        raise ConfigError(f"budget.total must be numeric, got {total!r}")
    if enabled and total < MIN_ENABLED_BUDGET_USD:
        msg = (
            "budget.total must be at least "
            f"{MIN_ENABLED_BUDGET_USD:.2f} when budget.enabled is true, got {total!r}"
        )
        raise ConfigError(msg)
    if not enabled and total < 0:
        raise ConfigError(f"budget.total must be non-negative, got {total!r}")
    if not isinstance(warning, int | float) or not (0.0 <= warning <= 1.0):
        raise ConfigError(
            f"budget.warning_threshold must be between 0.0 and 1.0, got {warning!r}"
        )
    time_enabled = raw.get("time_enabled", False)
    time_total_minutes = raw.get("time_total_minutes", 0)
    if not isinstance(time_enabled, bool):
        raise ConfigError(f"budget.time_enabled must be a boolean, got {time_enabled!r}")
    # ``bool`` is an ``int`` subclass — reject it so True/False can't pose as minutes.
    if isinstance(time_total_minutes, bool) or not isinstance(time_total_minutes, int):
        raise ConfigError(
            f"budget.time_total_minutes must be an integer, got {time_total_minutes!r}"
        )
    if time_enabled and not (
        MIN_TIME_BUDGET_MINUTES <= time_total_minutes <= MAX_TIME_BUDGET_MINUTES
    ):
        raise ConfigError(
            f"budget.time_total_minutes must be between {MIN_TIME_BUDGET_MINUTES} and "
            f"{MAX_TIME_BUDGET_MINUTES} (1h–72h) when budget.time_enabled is true, "
            f"got {time_total_minutes!r}"
        )
    if not time_enabled and time_total_minutes < 0:
        raise ConfigError(
            f"budget.time_total_minutes must be non-negative, got {time_total_minutes!r}"
        )
    return BudgetConfig(
        enabled=enabled,
        total=float(total),
        warning_threshold=float(warning),
        time_enabled=time_enabled,
        time_total_minutes=time_total_minutes,
    )


def validate_budget_payload(
    payload: object,
    *,
    exc_class: type[Exception] | None = None,
) -> BudgetConfig:
    """Validate an untrusted sidecar-RPC or inline-budget payload dict.

    Like :func:`parse_budget_raw` but accepts ``object`` as input (validates the
    container type first) and raises *exc_class* instead of
    :class:`~agentshore.errors.ConfigError` when provided. Used by
    ``sidecar/project.py`` (raises ``ProjectError``) and ``sidecar/server.py``
    (re-maps to JSON-RPC errors).

    ``enabled`` and ``total`` are required. ``warning_threshold``,
    ``time_enabled``, and ``time_total_minutes`` are optional.

    Unknown keys are rejected to keep the contract narrow.
    """
    from agentshore.config.models import BudgetConfig
    from agentshore.errors import ConfigError

    budget_keys: frozenset[str] = frozenset(
        {"enabled", "total", "warning_threshold", "time_enabled", "time_total_minutes"}
    )

    def _raise(msg: str) -> None:
        if exc_class is not None:
            raise exc_class(msg)
        raise ConfigError(msg)

    if not isinstance(payload, dict):
        _raise("budget payload must be an object")
        return BudgetConfig()  # unreachable; satisfies mypy
    unknown = set(payload.keys()) - budget_keys
    if unknown:
        _raise(f"unknown budget fields: {sorted(unknown)}")
    if "enabled" not in payload:
        _raise("budget.enabled is required")
    enabled = payload["enabled"]
    if not isinstance(enabled, bool):
        _raise("budget.enabled must be a boolean")
    if "total" not in payload:
        _raise("budget.total is required")
    total_raw = payload["total"]
    if isinstance(total_raw, bool) or not isinstance(total_raw, (int, float)):
        _raise("budget.total must be a number")
        return BudgetConfig()  # unreachable
    total = float(total_raw)
    if not math.isfinite(total):
        _raise("budget.total must be finite")
    if total < 0:
        _raise("budget.total must be >= 0")
    if enabled and total < MIN_ENABLED_BUDGET_USD:
        _raise(
            "budget.total must be at least "
            f"{MIN_ENABLED_BUDGET_USD:.2f} when budget.enabled is true, got {total!r}"
        )
    threshold = 0.20
    if "warning_threshold" in payload:
        threshold_raw = payload["warning_threshold"]
        if isinstance(threshold_raw, bool) or not isinstance(threshold_raw, (int, float)):
            _raise("budget.warning_threshold must be a number")
        threshold = float(threshold_raw)  # type: ignore[arg-type]
        if not math.isfinite(threshold):
            _raise("budget.warning_threshold must be finite")
        if threshold < 0 or threshold > 1:
            _raise("budget.warning_threshold must be between 0 and 1")
    time_enabled = payload.get("time_enabled", False)
    if not isinstance(time_enabled, bool):
        _raise("budget.time_enabled must be a boolean")
    time_total_minutes_raw = payload.get("time_total_minutes", 0)
    if isinstance(time_total_minutes_raw, bool) or not isinstance(time_total_minutes_raw, int):
        _raise("budget.time_total_minutes must be an integer")
        return BudgetConfig()  # unreachable
    time_total_minutes = int(time_total_minutes_raw)
    if time_enabled and not (
        MIN_TIME_BUDGET_MINUTES <= time_total_minutes <= MAX_TIME_BUDGET_MINUTES
    ):
        _raise(
            f"budget.time_total_minutes must be between {MIN_TIME_BUDGET_MINUTES} and "
            f"{MAX_TIME_BUDGET_MINUTES} (1h–72h) when budget.time_enabled is true, "
            f"got {time_total_minutes!r}"
        )
    if not time_enabled and time_total_minutes < 0:
        _raise("budget.time_total_minutes must be non-negative")
    return BudgetConfig(
        enabled=enabled,
        total=total,
        warning_threshold=threshold,
        time_enabled=time_enabled,
        time_total_minutes=time_total_minutes,
    )
