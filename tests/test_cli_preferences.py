"""Tests for the ``agentshore preferences`` CLI group."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from agentshore import preferences as gp
from agentshore.cli.commands.preferences import preferences


@pytest.fixture
def global_prefs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "preferences.yaml"
    monkeypatch.setattr(gp, "GLOBAL_PREFERENCES_PATH", path)
    return path


def test_list_shows_all_plays_on_by_default(global_prefs: Path) -> None:
    result = CliRunner().invoke(preferences, ["list"])
    assert result.exit_code == 0
    assert "[on ] cleanup" in result.output
    assert "[on ] run_qa" in result.output


def test_disable_then_enable_round_trip(global_prefs: Path) -> None:
    runner = CliRunner()
    out = runner.invoke(preferences, ["disable", "run_qa", "cleanup"]).output
    assert "[off] cleanup" in out
    assert "[off] run_qa" in out
    assert gp.load_preferences_data()["disabled_plays"] == ("cleanup", "run_qa")

    out = runner.invoke(preferences, ["enable", "run_qa"]).output
    assert "[on ] run_qa" in out
    assert "[off] cleanup" in out
    assert gp.load_preferences_data()["disabled_plays"] == ("cleanup",)


def test_disable_rejects_critical_play(global_prefs: Path) -> None:
    result = CliRunner().invoke(preferences, ["disable", "issue_pickup"])
    assert result.exit_code != 0
    assert "issue_pickup" in result.output
    assert gp.load_preferences_data()["disabled_plays"] == ()


def test_reset_clears_disabled(global_prefs: Path) -> None:
    runner = CliRunner()
    runner.invoke(preferences, ["disable", "prune"])
    runner.invoke(preferences, ["reset"])
    assert gp.load_preferences_data()["disabled_plays"] == ()
