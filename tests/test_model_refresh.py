"""Tests for model_refresh: the orchestrator behind `agentshore models refresh`.

Discovery mechanics (real subprocess spawn) are already covered by
test_model_discovery.py / test_model_discovery_llm.py — these tests mock
discover_all / discover_claude_code_models_via_agent at the function boundary
and focus purely on diff/merge/write/opt-in orchestration logic.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from agentshore.agents import model_catalog as model_catalog_mod
from agentshore.agents import model_discovery, model_discovery_llm
from agentshore.agents.model_discovery import DiscoveryResult
from agentshore.agents.model_discovery_llm import LlmDiscoveryResult
from agentshore.agents.model_refresh import ModelRefreshSummary, refresh_model_catalog


@pytest.fixture(autouse=True)
def _isolated_global_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "models.yaml"
    monkeypatch.setattr(model_catalog_mod, "GLOBAL_MODELS_PATH", path)
    return path


def _ok_free_results(
    *,
    codex: tuple[str, ...] = ("gpt-5.5", "gpt-5.4"),
    grok: tuple[str, ...] = ("grok-4.5",),
    antigravity: tuple[str, ...] = ("Gemini 3.5 Flash (High)",),
) -> dict[str, DiscoveryResult]:
    return {
        "codex": DiscoveryResult("codex", codex, "ok"),
        "grok": DiscoveryResult("grok", grok, "ok"),
        "antigravity": DiscoveryResult("antigravity", antigravity, "ok"),
    }


def test_refresh_only_probes_free_harnesses_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_discovery, "discover_all", lambda **_kw: _ok_free_results())
    llm_spy = MagicMock()
    monkeypatch.setattr(model_discovery_llm, "discover_claude_code_models_via_agent", llm_spy)

    summary = refresh_model_catalog()

    llm_spy.assert_not_called()
    assert summary.harnesses["claude_code"].status == "skipped"
    assert summary.harnesses["codex"].status == "ok"


def test_refresh_diffs_against_prior_catalog(
    monkeypatch: pytest.MonkeyPatch, _isolated_global_models: Path
) -> None:
    _isolated_global_models.write_text(
        yaml.dump({"models": {"grok": ["grok-old-model"]}}), encoding="utf-8"
    )
    monkeypatch.setattr(
        model_discovery, "discover_all", lambda **_kw: _ok_free_results(grok=("grok-4.5",))
    )
    monkeypatch.setattr(model_discovery_llm, "discover_claude_code_models_via_agent", MagicMock())

    summary = refresh_model_catalog()

    grok_outcome = summary.harnesses["grok"]
    assert grok_outcome.added == ("grok-4.5",)
    assert grok_outcome.removed == ("grok-old-model",)
    assert summary.any_changes


def test_refresh_writes_only_successful_harnesses(
    monkeypatch: pytest.MonkeyPatch, _isolated_global_models: Path
) -> None:
    _isolated_global_models.write_text(
        yaml.dump({"models": {"grok": ["grok-prior-override"]}}), encoding="utf-8"
    )

    def fake_discover_all(**_kw: object) -> dict[str, DiscoveryResult]:
        return {
            "codex": DiscoveryResult("codex", ("gpt-5.5",), "ok"),
            "grok": DiscoveryResult("grok", (), "error", "boom"),
            "antigravity": DiscoveryResult("antigravity", ("model-x",), "ok"),
        }

    monkeypatch.setattr(model_discovery, "discover_all", fake_discover_all)
    monkeypatch.setattr(model_discovery_llm, "discover_claude_code_models_via_agent", MagicMock())

    refresh_model_catalog()

    written = yaml.safe_load(_isolated_global_models.read_text())
    assert written["models"]["codex"] == ["gpt-5.5"]
    assert written["models"]["antigravity"] == ["model-x"]
    # grok's probe failed this round — its prior override entry must survive untouched.
    assert written["models"]["grok"] == ["grok-prior-override"]


def test_refresh_dry_run_does_not_write(
    monkeypatch: pytest.MonkeyPatch, _isolated_global_models: Path
) -> None:
    monkeypatch.setattr(model_discovery, "discover_all", lambda **_kw: _ok_free_results())
    monkeypatch.setattr(model_discovery_llm, "discover_claude_code_models_via_agent", MagicMock())

    summary = refresh_model_catalog(dry_run=True)

    assert summary.dry_run is True
    assert not _isolated_global_models.exists()


def test_refresh_flags_models_missing_a_pricing_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        model_discovery,
        "discover_all",
        lambda **_kw: _ok_free_results(codex=("gpt-5.5", "brand-new-unpriced-model")),
    )
    monkeypatch.setattr(model_discovery_llm, "discover_claude_code_models_via_agent", MagicMock())

    summary = refresh_model_catalog()

    assert ("codex", "brand-new-unpriced-model") in summary.unpriced_models
    assert ("codex", "gpt-5.5") not in summary.unpriced_models  # already priced


def test_refresh_include_claude_code_calls_llm_discovery_and_sums_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_discovery, "discover_all", lambda **_kw: _ok_free_results())
    llm_result = LlmDiscoveryResult(
        "claude_code", ("sonnet", "opus"), "ok", "cost_usd=0.3500", 0.35, "haiku"
    )
    monkeypatch.setattr(
        model_discovery_llm,
        "discover_claude_code_models_via_agent",
        lambda **_kw: llm_result,
    )

    summary = refresh_model_catalog(include_claude_code=True)

    assert summary.harnesses["claude_code"].status == "ok"
    assert summary.harnesses["claude_code"].models == ("sonnet", "opus")
    assert summary.total_cost_usd == pytest.approx(0.35)


def test_refresh_claude_code_budget_exceeded_keeps_prior_models(
    monkeypatch: pytest.MonkeyPatch, _isolated_global_models: Path
) -> None:
    _isolated_global_models.write_text(
        yaml.dump({"models": {"claude_code": ["sonnet"]}}), encoding="utf-8"
    )
    monkeypatch.setattr(model_discovery, "discover_all", lambda **_kw: _ok_free_results())
    monkeypatch.setattr(
        model_discovery_llm,
        "discover_claude_code_models_via_agent",
        lambda **_kw: LlmDiscoveryResult("claude_code", (), "budget_exceeded", "capped", 0.15),
    )

    summary = refresh_model_catalog(include_claude_code=True)

    outcome = summary.harnesses["claude_code"]
    assert outcome.status == "budget_exceeded"
    assert outcome.models == ("sonnet",)  # prior list preserved, nothing written
    written = yaml.safe_load(_isolated_global_models.read_text())
    assert written["models"]["claude_code"] == ["sonnet"]


def test_any_changes_false_when_nothing_added_or_removed(
    monkeypatch: pytest.MonkeyPatch, _isolated_global_models: Path
) -> None:
    # Pre-seed the override so "before" exactly matches what the mocked probe
    # returns for all three free harnesses — a genuine no-op refresh.
    seed = _ok_free_results()
    _isolated_global_models.write_text(
        yaml.dump({"models": {k: list(v.models) for k, v in seed.items()}}), encoding="utf-8"
    )
    monkeypatch.setattr(model_discovery, "discover_all", lambda **_kw: seed)
    monkeypatch.setattr(model_discovery_llm, "discover_claude_code_models_via_agent", MagicMock())

    summary = refresh_model_catalog()

    assert isinstance(summary, ModelRefreshSummary)
    assert not summary.any_changes
