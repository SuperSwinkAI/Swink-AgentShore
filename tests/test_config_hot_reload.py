"""Tests for SIGHUP-triggered config hot-reload."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml

from agentshore.config import RuntimeConfig, load_config
from agentshore.core import Orchestrator

if TYPE_CHECKING:
    from pathlib import Path


def _write_config(path: Path, overrides: dict[str, object] | None = None) -> None:
    """Write a minimal agentshore.yaml with optional top-level overrides."""
    data: dict[str, object] = {"budget": {"enabled": True, "total": 20.0}}
    if overrides:
        data.update(overrides)
    path.write_text(yaml.dump(data), encoding="utf-8")


@pytest.mark.asyncio
async def test_reload_config_swaps_cfg(tmp_path: Path) -> None:
    """Modifying the YAML and calling _reload_config swaps self._cfg."""
    config_path = tmp_path / "agentshore.yaml"
    _write_config(config_path, {"budget": {"enabled": True, "total": 20.0}})

    orch = await Orchestrator.bootstrap(
        cfg=RuntimeConfig(), repo_root=tmp_path, config_path=config_path
    )
    async with orch:
        assert orch._cfg.budget.enabled is False
        assert orch._cfg.budget.total == 0.0

        _write_config(config_path, {"budget": {"enabled": True, "total": 25.0}})
        await orch._lifecycle.reload_config()

        assert orch._cfg.budget.total == 25.0


@pytest.mark.asyncio
async def test_reload_config_no_path(tmp_path: Path) -> None:
    """Reload with no config_path logs warning and returns without error."""
    orch = await Orchestrator.bootstrap(cfg=RuntimeConfig(), repo_root=tmp_path)
    async with orch:
        original_cfg = orch._cfg
        await orch._lifecycle.reload_config()
        assert orch._cfg is original_cfg


@pytest.mark.asyncio
async def test_reload_config_invalid_yaml(tmp_path: Path) -> None:
    """Invalid YAML in config file is rejected; old config retained."""
    config_path = tmp_path / "agentshore.yaml"
    _write_config(config_path, {"budget": {"enabled": True, "total": 20.0}})
    cfg = load_config(config_path)

    orch = await Orchestrator.bootstrap(cfg=cfg, repo_root=tmp_path, config_path=config_path)
    async with orch:
        original_cfg = orch._cfg
        config_path.write_text("invalid: yaml: {{{", encoding="utf-8")
        await orch._lifecycle.reload_config()
        assert orch._cfg is original_cfg


@pytest.mark.asyncio
async def test_reload_config_no_changes(tmp_path: Path) -> None:
    """Reloading an unchanged config file does not swap the config instance."""
    config_path = tmp_path / "agentshore.yaml"
    _write_config(config_path, {"budget": {"enabled": True, "total": 20.0}})
    cfg = load_config(config_path)

    orch = await Orchestrator.bootstrap(cfg=cfg, repo_root=tmp_path, config_path=config_path)
    async with orch:
        original_cfg = orch._cfg
        # Reload an unchanged file — content still matches, no swap.
        await orch._lifecycle.reload_config()
        assert orch._cfg is original_cfg


@pytest.mark.asyncio
async def test_reload_config_logs_changed_fields(tmp_path: Path) -> None:
    """Changed fields are reflected in the new config after reload."""
    config_path = tmp_path / "agentshore.yaml"
    _write_config(config_path, {"budget": {"enabled": True, "total": 20.0}})

    orch = await Orchestrator.bootstrap(
        cfg=RuntimeConfig(), repo_root=tmp_path, config_path=config_path
    )
    async with orch:
        config_path.write_text(
            yaml.dump(
                {
                    "budget": {"enabled": True, "total": 99.0},
                    "scope": {"strict_mode": True},
                }
            ),
            encoding="utf-8",
        )
        await orch._lifecycle.reload_config()

        assert orch._cfg.budget.total == 99.0
        assert orch._cfg.scope.strict_mode is True


def _sonnet_pricing(output_rate: float, input_rate: float = 0.1) -> dict[str, object]:
    return {
        "models": {
            "sonnet": {
                "max_context": 200000,
                "cost_per_1k_input": input_rate,
                "cost_per_1k_output": output_rate,
            }
        }
    }


@pytest.mark.asyncio
async def test_reload_picks_up_global_pricing_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing the global pricing.yaml + SIGHUP reprices the next dispatch."""
    from agentshore.agents import pricing as pricing_mod

    config_path = tmp_path / "agentshore.yaml"
    _write_config(config_path)
    global_pricing = tmp_path / "pricing.yaml"
    global_pricing.write_text(yaml.dump(_sonnet_pricing(0.2)), encoding="utf-8")
    monkeypatch.setattr(pricing_mod, "GLOBAL_PRICING_PATH", global_pricing)

    orch = await Orchestrator.bootstrap(
        cfg=RuntimeConfig(), repo_root=tmp_path, config_path=config_path
    )
    async with orch:
        # Edit the single global touchpoint and reload — no restart.
        global_pricing.write_text(yaml.dump(_sonnet_pricing(0.9)), encoding="utf-8")
        await orch._lifecycle.reload_config()

        assert orch._cfg.pricebook.resolve("claude_code", "sonnet").cost_per_1k_output == 0.9


@pytest.mark.asyncio
async def test_reload_rejects_malformed_global_pricing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken global pricing.yaml on reload is rejected; old pricing retained."""
    from agentshore.agents import pricing as pricing_mod

    config_path = tmp_path / "agentshore.yaml"
    _write_config(config_path)
    global_pricing = tmp_path / "pricing.yaml"
    global_pricing.write_text(yaml.dump(_sonnet_pricing(0.9)), encoding="utf-8")
    monkeypatch.setattr(pricing_mod, "GLOBAL_PRICING_PATH", global_pricing)

    orch = await Orchestrator.bootstrap(
        cfg=RuntimeConfig(), repo_root=tmp_path, config_path=config_path
    )
    async with orch:
        await orch._lifecycle.reload_config()
        assert orch._cfg.pricebook.resolve("claude_code", "sonnet").cost_per_1k_output == 0.9

        # Negative rate → ConfigError → reload aborts, prior pricing stands.
        global_pricing.write_text(yaml.dump(_sonnet_pricing(0.2, input_rate=-5)), encoding="utf-8")
        await orch._lifecycle.reload_config()

        assert orch._cfg.pricebook.resolve("claude_code", "sonnet").cost_per_1k_output == 0.9


@pytest.mark.asyncio
async def test_reload_config_rejects_budget_below_floor(tmp_path: Path) -> None:
    """Invalid enabled budget on reload is rejected; old config retained."""
    config_path = tmp_path / "agentshore.yaml"
    _write_config(config_path, {"budget": {"enabled": True, "total": 20.0}})
    cfg = load_config(config_path)

    orch = await Orchestrator.bootstrap(cfg=cfg, repo_root=tmp_path, config_path=config_path)
    async with orch:
        original_cfg = orch._cfg
        _write_config(config_path, {"budget": {"enabled": True, "total": 19.99}})

        await orch._lifecycle.reload_config()

        assert orch._cfg is original_cfg
        assert orch._cfg.budget.total == 20.0


@pytest.mark.asyncio
async def test_reload_refreshes_live_ppo_selector_cfg(tmp_path: Path) -> None:
    """A reload pushes the swapped config into the live PPO selector.

    Regression: the selector builds its action mask from an ``orchestrator_cfg``
    captured at construction and is not re-created on reload. Without refreshing
    that reference, a play disabled mid-session via Preferences stayed selectable
    until the session restarted (run_qa ran ~8 min after being disabled).
    """
    from unittest.mock import MagicMock

    from agentshore.rl.selector import PPOSelector

    config_path = tmp_path / "agentshore.yaml"
    _write_config(config_path, {"budget": {"enabled": True, "total": 20.0}})

    orch = await Orchestrator.bootstrap(
        cfg=RuntimeConfig(), repo_root=tmp_path, config_path=config_path
    )
    async with orch:
        # spec=PPOSelector so isinstance(selector, _ppo_selector_cls()) holds.
        selector = MagicMock(spec=PPOSelector)
        orch._selector = selector

        _write_config(config_path, {"budget": {"enabled": True, "total": 25.0}})
        await orch._lifecycle.reload_config()

        selector.update_orchestrator_cfg.assert_called_once_with(orch._cfg)
        assert orch._cfg.budget.total == 25.0

        # Restore so the mock is not exercised during orchestrator shutdown.
        orch._selector = None
