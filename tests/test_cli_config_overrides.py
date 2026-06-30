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

# Import cli first: the cli<->bootstrap cycle (start->bootstrap->cli.helpers)
# deadlocks if bootstrap is imported first.
import agentshore.cli  # noqa: F401
from agentshore.cli_helpers import _DEFAULT_BUDGET, _DEFAULT_TIME_MINUTES
from agentshore.config.models import PolicyMode
from agentshore.session.bootstrap import (
    _load_config_with_overrides,
    _require_per_identity_tier_coverage,
    require_startup_model_tier_coverage,
    validate_budget_flag,
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


# Budget


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
    # Explicit disabled budget (total carried) is respected, not safety-defaulted.
    cfg_path = _write_cfg(tmp_path, "budget:\n  enabled: false\n  total: 75.0\n")
    cfg, _ = _resolve(cfg_path)
    assert cfg.budget.enabled is False
    assert cfg.budget.total == 75.0


# Dual-dimension precedence (empty/fresh config): naked start -> $200 + 24h;
# naming one dimension suppresses the other's bare default; --unlimited disables both.

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
    # Configured dollar budget + --time override -> dollar respected, time set.
    cfg_path = _write_cfg(tmp_path, "budget:\n  enabled: true\n  total: 50.0\n")
    cfg, _ = _resolve(cfg_path, time_override=120)
    assert (cfg.budget.enabled, cfg.budget.total) == (True, 50.0)
    assert (cfg.budget.time_enabled, cfg.budget.time_total_minutes) == (True, 120)


def test_naked_configured_dollar_yaml_no_time_default_injected(tmp_path: Path) -> None:
    # Configured budget block respected as-is: no 24h time default injected,
    # so existing dollar-only sessions are unchanged.
    cfg_path = _write_cfg(tmp_path, "budget:\n  enabled: true\n  total: 50.0\n")
    cfg, _ = _resolve(cfg_path)
    assert cfg.budget.time_enabled is False


# Strict


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


# Policy mode (regression: was already correct, keep it that way)


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


# Startup model-tier coverage


def test_startup_tier_coverage_blocks_missing_large(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg, _ = _resolve(
        _write_cfg(
            tmp_path,
            """
agents:
  claude_code:
    enabled: true
    model_tiers:
      small:
        enabled: true
      medium:
        enabled: true
""",
        )
    )

    with pytest.raises(SystemExit) as excinfo:
        require_startup_model_tier_coverage(cfg)

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "missing required model tier coverage: large" in err
    assert "agents.<type>.model_tiers" in err


def test_startup_tier_coverage_allows_cross_agent_coverage(tmp_path: Path) -> None:
    cfg, _ = _resolve(
        _write_cfg(
            tmp_path,
            """
agents:
  claude_code:
    enabled: true
    model_tiers:
      small:
        enabled: true
  codex:
    enabled: true
    model_tiers:
      medium:
        enabled: true
  grok:
    enabled: true
    model_tiers:
      large:
        enabled: true
""",
        )
    )

    require_startup_model_tier_coverage(cfg)


def test_startup_tier_coverage_blocks_legacy_medium_only_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg, _ = _resolve(
        _write_cfg(
            tmp_path,
            """
agents:
  claude_code:
    enabled: true
    model: sonnet
""",
        )
    )

    with pytest.raises(SystemExit) as excinfo:
        require_startup_model_tier_coverage(cfg)

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "missing required model tier coverage: small, large" in err


# Per-identity model-tier coverage (CLI feedback)


def test_per_identity_tier_coverage_cli_blocks_and_names_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI guard exits 1 and prints a clear, per-identity, multi-line error."""
    cfg, _ = _resolve(
        _write_cfg(
            tmp_path,
            """
identities:
  jwesleye:
    git_user_name: jwesleye
    git_user_email: j@example.com
    gh_token_login: jwesleye
  unseriousai:
    git_user_name: unseriousAI
    git_user_email: u@example.com
    gh_token_login: unseriousai
agents:
  claude_code:
    enabled: true
    identity: jwesleye
    model_tiers:
      small:
        enabled: true
      medium:
        enabled: true
  codex:
    enabled: true
    identity: unseriousai
    model_tiers:
      small:
        enabled: true
      medium:
        enabled: true
      large:
        enabled: true
""",
        )
    )

    with pytest.raises(SystemExit) as excinfo:
        _require_per_identity_tier_coverage(cfg)

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "every model tier" in err
    assert "'jwesleye' missing tier(s): large" in err
    assert "agentshore identity --reconfigure" in err


def test_per_identity_tier_coverage_cli_allows_full_coverage(tmp_path: Path) -> None:
    cfg, _ = _resolve(
        _write_cfg(
            tmp_path,
            """
identities:
  jwesleye:
    git_user_name: jwesleye
    git_user_email: j@example.com
    gh_token_login: jwesleye
  unseriousai:
    git_user_name: unseriousAI
    git_user_email: u@example.com
    gh_token_login: unseriousai
agents:
  claude_code:
    enabled: true
    identity: jwesleye
    model_tiers:
      small:
        enabled: true
      medium:
        enabled: true
      large:
        enabled: true
  codex:
    enabled: true
    identity: unseriousai
    model_tiers:
      small:
        enabled: true
      medium:
        enabled: true
      large:
        enabled: true
""",
        )
    )

    _require_per_identity_tier_coverage(cfg)  # no raise


# validate_budget_flag


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
