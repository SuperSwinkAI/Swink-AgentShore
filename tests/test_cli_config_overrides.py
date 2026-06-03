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
from agentshore.cli_helpers import _DEFAULT_BUDGET
from agentshore.config.models import PolicyMode
from agentshore.session.bootstrap import _load_config_with_overrides, validate_budget_flag


def _write_cfg(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "agentshore.yaml"
    cfg.write_text(body)
    return cfg


def _resolve(cfg_path: Path, **kwargs: object):
    defaults: dict[str, object] = {
        "budget_override": None,
        "no_budget": False,
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


def test_no_budget_disables(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, "budget:\n  enabled: true\n  total: 50.0\n")
    cfg, _ = _resolve(cfg_path, no_budget=True)
    assert cfg.budget.enabled is False


def test_no_budget_wins_over_budget_value(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, "budget:\n  enabled: true\n  total: 50.0\n")
    cfg, _ = _resolve(cfg_path, budget_override=40.0, no_budget=True)
    assert cfg.budget.enabled is False


def test_budget_omitted_no_config_block_uses_safety_default(tmp_path: Path) -> None:
    # No budget block at all -> BudgetConfig() default -> keep the $200 safety cap.
    cfg_path = _write_cfg(tmp_path, "project:\n  target_branch: main\n")
    cfg, _ = _resolve(cfg_path)
    assert cfg.budget.enabled is True
    assert cfg.budget.total == _DEFAULT_BUDGET


def test_budget_omitted_explicit_disabled_config_respected(tmp_path: Path) -> None:
    # An explicit, non-default disabled budget (total carried) is respected, not
    # overwritten by the safety default.
    cfg_path = _write_cfg(tmp_path, "budget:\n  enabled: false\n  total: 75.0\n")
    cfg, _ = _resolve(cfg_path)
    assert cfg.budget.enabled is False
    assert cfg.budget.total == 75.0


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
