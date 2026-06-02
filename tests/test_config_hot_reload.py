"""Tests for SIGHUP-triggered config hot-reload."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml

from agentshore.config import RuntimeConfig, load_config
from agentshore.core import Orchestrator

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(path: Path, overrides: dict[str, object] | None = None) -> None:
    """Write a minimal agentshore.yaml with optional top-level overrides."""
    data: dict[str, object] = {"budget": {"enabled": True, "total": 20.0}}
    if overrides:
        data.update(overrides)
    path.write_text(yaml.dump(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


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

        # Modify config file — change budget total
        _write_config(config_path, {"budget": {"enabled": True, "total": 25.0}})
        await orch._lifecycle.reload_config()

        assert orch._cfg.budget.total == 25.0


@pytest.mark.asyncio
async def test_reload_config_no_path(tmp_path: Path) -> None:
    """Reload with no config_path logs warning and returns without error."""
    orch = await Orchestrator.bootstrap(cfg=RuntimeConfig(), repo_root=tmp_path)
    async with orch:
        original_cfg = orch._cfg
        # Should not raise
        await orch._lifecycle.reload_config()
        # _cfg should be unchanged
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
        assert orch._cfg is original_cfg  # unchanged


@pytest.mark.asyncio
async def test_reload_config_no_changes(tmp_path: Path) -> None:
    """Reloading an unchanged config file does not swap the config instance."""
    config_path = tmp_path / "agentshore.yaml"
    _write_config(config_path, {"budget": {"enabled": True, "total": 20.0}})
    cfg = load_config(config_path)

    orch = await Orchestrator.bootstrap(cfg=cfg, repo_root=tmp_path, config_path=config_path)
    async with orch:
        original_cfg = orch._cfg
        # Reload without modifying the file — content still matches
        await orch._lifecycle.reload_config()
        # Config should not have been swapped (no changes detected)
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
        # Mutate config on disk: change budget and scope
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
