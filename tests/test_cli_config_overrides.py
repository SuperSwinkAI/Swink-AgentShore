"""``agentshore start`` config-override resolution.

These tests pin the contract that the CLI only overrides an ``agentshore.yaml``
setting when the corresponding flag is *actually* provided — an omitted flag must
defer to the file. Regression guard for the budget/strict clobber bug where the
CLI's flag defaults silently overwrote the configured ``budget.total`` and
``scope.strict_mode``.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest

# Import the CLI package first to establish the import order for the
# pre-existing cli<->session.bootstrap cycle (start.py imports bootstrap;
# bootstrap imports cli.helpers). Importing bootstrap first would deadlock.
import agentshore.cli  # noqa: F401
from agentshore.cli_helpers import _DEFAULT_BUDGET, _DEFAULT_TIME_MINUTES
from agentshore.config.models import PolicyMode
from agentshore.session.bootstrap import (
    _load_config_with_overrides,
    validate_budget_flag,
    validate_time_flag,
)


def _write_cfg(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "agentshore.yaml"
    cfg.write_text(body)
    return cfg


def _resolve(cfg_path: Path, **kwargs: object):
    defaults: dict[str, object] = {
        "budget_override": None,
        "time_override": None,
        "unlimited": False,
        "policy_mode_override": None,
        "strict": None,
    }
    defaults.update(kwargs)
    return _load_config_with_overrides(cfg_path, **defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #


def test_budget_omitted_respects_yaml(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, "budget:\n  enabled: true\n  total: 50.0\n")
    cfg, _ = _resolve(cfg_path)
    assert cfg.budget.enabled is True
    assert cfg.budget.total == 50.0


def test_budget_flag_overrides_yaml(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, "budget:\n  enabled: true\n  total: 50.0\n")
    cfg, _ = _resolve(cfg_path, budget_override=40.0)
    assert cfg.budget.enabled is True
    assert cfg.budget.total == 40.0


def test_unlimited_disables_both_caps(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, "budget:\n  enabled: true\n  total: 50.0\n")
    cfg, _ = _resolve(cfg_path, unlimited=True)
    assert cfg.budget.enabled is False
    assert cfg.budget.time_enabled is False


def test_unlimited_wins_over_budget_value(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, "budget:\n  enabled: true\n  total: 50.0\n")
    cfg, _ = _resolve(cfg_path, budget_override=40.0, unlimited=True)
    assert cfg.budget.enabled is False
    assert cfg.budget.time_enabled is False


def test_budget_omitted_explicit_disabled_config_respected(tmp_path: Path) -> None:
    # An explicit, non-default disabled budget (total carried) is respected, not
    # overwritten by the safety default.
    cfg_path = _write_cfg(tmp_path, "budget:\n  enabled: false\n  total: 75.0\n")
    cfg, _ = _resolve(cfg_path)
    assert cfg.budget.enabled is False
    assert cfg.budget.total == 75.0


# --------------------------------------------------------------------------- #
# Dual-dimension resolution table (empty/fresh config). Mirrors the
# user-specified precedence: naked start -> $200 + 24h; naming one dimension
# suppresses the other's bare default; --unlimited disables both.
# --------------------------------------------------------------------------- #

_EMPTY = "project:\n  target_branch: main\n"


def test_naked_empty_config_gets_both_safety_defaults(tmp_path: Path) -> None:
    cfg, _ = _resolve(_write_cfg(tmp_path, _EMPTY))
    assert (cfg.budget.enabled, cfg.budget.total) == (True, _DEFAULT_BUDGET)
    assert (cfg.budget.time_enabled, cfg.budget.time_total_minutes) == (
        True,
        _DEFAULT_TIME_MINUTES,
    )


def test_budget_only_suppresses_time_default(tmp_path: Path) -> None:
    cfg, _ = _resolve(_write_cfg(tmp_path, _EMPTY), budget_override=1000.0)
    assert (cfg.budget.enabled, cfg.budget.total) == (True, 1000.0)
    assert cfg.budget.time_enabled is False


def test_time_only_suppresses_dollar_default(tmp_path: Path) -> None:
    cfg, _ = _resolve(_write_cfg(tmp_path, _EMPTY), time_override=1440)
    assert cfg.budget.enabled is False
    assert (cfg.budget.time_enabled, cfg.budget.time_total_minutes) == (True, 1440)


def test_unlimited_empty_config_disables_both(tmp_path: Path) -> None:
    cfg, _ = _resolve(_write_cfg(tmp_path, _EMPTY), unlimited=True)
    assert cfg.budget.enabled is False
    assert cfg.budget.time_enabled is False


def test_time_override_on_configured_yaml_keeps_dollar(tmp_path: Path) -> None:
    # Configured dollar budget + a --time override -> dollar respected, time set.
    cfg_path = _write_cfg(tmp_path, "budget:\n  enabled: true\n  total: 50.0\n")
    cfg, _ = _resolve(cfg_path, time_override=120)
    assert (cfg.budget.enabled, cfg.budget.total) == (True, 50.0)
    assert (cfg.budget.time_enabled, cfg.budget.time_total_minutes) == (True, 120)


def test_naked_configured_dollar_yaml_no_time_default_injected(tmp_path: Path) -> None:
    # A configured (non-empty) budget block is respected as-is: no 24h time
    # default is injected, so existing dollar-only sessions are unchanged.
    cfg_path = _write_cfg(tmp_path, "budget:\n  enabled: true\n  total: 50.0\n")
    cfg, _ = _resolve(cfg_path)
    assert cfg.budget.time_enabled is False


# --------------------------------------------------------------------------- #
# Strict
# --------------------------------------------------------------------------- #


def test_strict_omitted_respects_yaml_true(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, "scope:\n  strict_mode: true\n")
    cfg, _ = _resolve(cfg_path)
    assert cfg.scope.strict_mode is True


def test_strict_omitted_respects_yaml_false(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, "scope:\n  strict_mode: false\n")
    cfg, _ = _resolve(cfg_path)
    assert cfg.scope.strict_mode is False


def test_strict_flag_overrides_yaml(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, "scope:\n  strict_mode: false\n")
    cfg, _ = _resolve(cfg_path, strict=True)
    assert cfg.scope.strict_mode is True


def test_no_strict_flag_overrides_yaml(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, "scope:\n  strict_mode: true\n")
    cfg, _ = _resolve(cfg_path, strict=False)
    assert cfg.scope.strict_mode is False


# --------------------------------------------------------------------------- #
# Policy mode (regression: was already correct, keep it that way)
# --------------------------------------------------------------------------- #


def test_policy_mode_omitted_respects_yaml(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, "rl:\n  policy_mode: audit-replay\n")
    cfg, effective = _resolve(cfg_path)
    assert cfg.rl.policy_mode == PolicyMode.AUDIT_REPLAY
    assert effective == PolicyMode.AUDIT_REPLAY


def test_policy_mode_override_wins(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, "rl:\n  policy_mode: audit-replay\n")
    cfg, effective = _resolve(cfg_path, policy_mode_override=PolicyMode.LEARNING)
    assert cfg.rl.policy_mode == PolicyMode.LEARNING
    assert effective == PolicyMode.LEARNING


# --------------------------------------------------------------------------- #
# validate_budget_flag
# --------------------------------------------------------------------------- #


def test_validate_budget_none_is_ok() -> None:
    validate_budget_flag(None)  # no raise — omitted defers to config


def test_validate_budget_positive_is_ok() -> None:
    validate_budget_flag(50.0)


def test_validate_budget_zero_raises() -> None:
    with pytest.raises(click.BadParameter):
        validate_budget_flag(0.0)


def test_validate_budget_below_floor_raises() -> None:
    with pytest.raises(click.BadParameter, match="at least"):
        validate_budget_flag(19.99)


# --------------------------------------------------------------------------- #
# validate_time_flag (parsed minutes; bounds 60–4320)
# --------------------------------------------------------------------------- #


def test_validate_time_none_is_ok() -> None:
    validate_time_flag(None)  # omitted defers to config


def test_validate_time_in_range_is_ok() -> None:
    validate_time_flag(60)
    validate_time_flag(1440)
    validate_time_flag(4320)


def test_validate_time_below_floor_raises() -> None:
    with pytest.raises(click.BadParameter, match="between 60 and 4320"):
        validate_time_flag(59)


def test_validate_time_above_ceiling_raises() -> None:
    with pytest.raises(click.BadParameter, match="between 60 and 4320"):
        validate_time_flag(4321)
