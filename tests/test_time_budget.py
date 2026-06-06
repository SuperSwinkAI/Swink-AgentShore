"""Unit coverage for the wall-clock time-budget dimension (#39).

Covers the shared helpers (``parse_duration``, time reserve), config validation
bounds, and the ``BudgetSnapshot`` time fields produced by the snapshot builder.
Resolution precedence and CLI flag handling live in
``tests/test_cli_config_overrides.py``; drain/terminate enforcement lives in
``tests/test_orchestrator_phase4.py``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentshore.budget import (
    MAX_TIME_BUDGET_MINUTES,
    MIN_TIME_BUDGET_MINUTES,
    TIME_BUDGET_DRAIN_RESERVE_MINUTES,
    parse_duration,
    time_budget_reserve_reached,
    time_budget_reserve_threshold,
)
from agentshore.config import load_config
from agentshore.config.models import BudgetConfig
from agentshore.core.mixins.snapshots import SnapshotProjector
from agentshore.errors import ConfigError

# --------------------------------------------------------------------------- #
# parse_duration
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1h", 60),
        ("24h", 1440),
        ("72h", 4320),
        ("90m", 90),
        ("120", 120),
        ("1.5h", 90),
        ("  24h  ", 1440),
    ],
)
def test_parse_duration_valid(text: str, expected: int) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "1d", "h", "-5h", "1.2.3"])
def test_parse_duration_unparseable_raises(text: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(text)


@pytest.mark.parametrize("text", ["59m", "30m", "73h", "5000", "4321"])
def test_parse_duration_out_of_range_raises(text: str) -> None:
    with pytest.raises(ValueError, match="between 60 and 4320"):
        parse_duration(text)


def test_parse_duration_bounds_inclusive() -> None:
    assert parse_duration(f"{MIN_TIME_BUDGET_MINUTES}") == MIN_TIME_BUDGET_MINUTES
    assert parse_duration(f"{MAX_TIME_BUDGET_MINUTES}") == MAX_TIME_BUDGET_MINUTES


# --------------------------------------------------------------------------- #
# time reserve
# --------------------------------------------------------------------------- #


def test_time_reserve_threshold() -> None:
    assert time_budget_reserve_threshold(1440) == 1440 - TIME_BUDGET_DRAIN_RESERVE_MINUTES
    # Never negative for tiny caps.
    assert time_budget_reserve_threshold(10) == 0.0


def test_time_reserve_reached() -> None:
    assert not time_budget_reserve_reached(elapsed_minutes=1419, total_minutes=1440)
    assert time_budget_reserve_reached(elapsed_minutes=1420, total_minutes=1440)
    assert time_budget_reserve_reached(elapsed_minutes=1440, total_minutes=1440)


# --------------------------------------------------------------------------- #
# config validation (load_config -> _parse_budget)
# --------------------------------------------------------------------------- #


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "agentshore.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_config_time_in_range(tmp_path: Path) -> None:
    cfg = load_config(
        _write(tmp_path, "budget:\n  time_enabled: true\n  time_total_minutes: 1440\n")
    )
    assert cfg.budget.time_enabled is True
    assert cfg.budget.time_total_minutes == 1440


@pytest.mark.parametrize("minutes", [59, 4321, 0])
def test_config_time_out_of_range_raises(tmp_path: Path, minutes: int) -> None:
    with pytest.raises(ConfigError, match="time_total_minutes"):
        load_config(
            _write(
                tmp_path,
                f"budget:\n  time_enabled: true\n  time_total_minutes: {minutes}\n",
            )
        )


def test_config_time_disabled_allows_zero(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "budget:\n  time_enabled: false\n  time_total_minutes: 0\n"))
    assert cfg.budget.time_enabled is False
    assert cfg.budget.time_total_minutes == 0


def test_empty_budget_config_is_default_sentinel() -> None:
    # The bare-default injection in resolution keys off this equality, so the
    # new time fields must not change the empty sentinel.
    assert BudgetConfig() == BudgetConfig(
        enabled=False, total=0.0, warning_threshold=0.20, time_enabled=False, time_total_minutes=0
    )


# --------------------------------------------------------------------------- #
# snapshot time fields
# --------------------------------------------------------------------------- #


def _projector() -> SnapshotProjector:
    return SnapshotProjector(manager=MagicMock(), store=MagicMock(), session_id="test")


def test_snapshot_time_fields_when_enabled() -> None:
    cfg = BudgetConfig(enabled=True, total=200.0, time_enabled=True, time_total_minutes=1440)
    snap = _projector().build_budget_snapshot(
        total_plays=10,
        total_cost=50.0,
        budget_cfg=cfg,
        extra_budget=0.0,
        elapsed_minutes=100.0,
    )
    assert snap.time_enabled is True
    assert snap.time_total_minutes == 1440.0
    assert snap.time_elapsed_minutes == 100.0
    assert snap.time_remaining_minutes == 1340.0


def test_snapshot_time_remaining_clamped_at_zero() -> None:
    cfg = BudgetConfig(time_enabled=True, time_total_minutes=60)
    snap = _projector().build_budget_snapshot(
        total_plays=1,
        total_cost=0.0,
        budget_cfg=cfg,
        extra_budget=0.0,
        elapsed_minutes=120.0,  # past the cap
    )
    assert snap.time_remaining_minutes == 0.0


def test_snapshot_time_fields_none_when_disabled() -> None:
    cfg = BudgetConfig(enabled=True, total=200.0, time_enabled=False)
    snap = _projector().build_budget_snapshot(
        total_plays=1,
        total_cost=10.0,
        budget_cfg=cfg,
        extra_budget=0.0,
        elapsed_minutes=100.0,
    )
    assert snap.time_enabled is False
    assert snap.time_total_minutes is None
    assert snap.time_elapsed_minutes is None
    assert snap.time_remaining_minutes is None
