"""Tests for shared seed-input resolution and the bootstrap config fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentshore.config import IntakeConfig, RuntimeConfig
from agentshore.core.phases import _resolve_seed_path
from agentshore.seed_input import SeedInputError, resolve_seed_input


def test_resolve_seed_input_accepts_file(tmp_path: Path) -> None:
    seed = tmp_path / "seed.md"
    seed.write_text("# Seed\n", encoding="utf-8")
    resolved, kind = resolve_seed_input(str(seed), tmp_path)
    assert resolved == seed
    assert kind == "file"


def test_resolve_seed_input_raises_on_missing(tmp_path: Path) -> None:
    with pytest.raises(SeedInputError):
        resolve_seed_input(str(tmp_path / "nope.md"), tmp_path)


def test_resolve_seed_path_prefers_transient(tmp_path: Path) -> None:
    # An explicit (transient) seed_path always wins over the config value.
    transient = tmp_path / "explicit.md"
    transient.write_text("x\n", encoding="utf-8")
    cfg = RuntimeConfig(intake=IntakeConfig(seed_paths=("config-seed.md",)))
    assert _resolve_seed_path(cfg, transient, tmp_path) == transient


def test_resolve_seed_path_falls_back_to_config(tmp_path: Path) -> None:
    # No transient seed → resolve the first intake.seed_paths entry relative to repo_root.
    (tmp_path / "PRD.md").write_text("# seed\n", encoding="utf-8")
    cfg = RuntimeConfig(intake=IntakeConfig(seed_paths=("PRD.md",)))
    resolved = _resolve_seed_path(cfg, None, tmp_path)
    assert resolved == tmp_path / "PRD.md"


def test_resolve_seed_path_missing_config_seed_returns_none(tmp_path: Path) -> None:
    # A stale/missing config seed degrades to open-start (None), never a crash.
    cfg = RuntimeConfig(intake=IntakeConfig(seed_paths=("does-not-exist.md",)))
    assert _resolve_seed_path(cfg, None, tmp_path) is None


def test_resolve_seed_path_no_config_returns_none(tmp_path: Path) -> None:
    cfg = RuntimeConfig(intake=IntakeConfig(seed_paths=()))
    assert _resolve_seed_path(cfg, None, tmp_path) is None
