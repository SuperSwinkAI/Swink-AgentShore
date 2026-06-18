"""Tests for the external pricing table (agentshore.agents.pricing)."""

from __future__ import annotations

import importlib.resources
from typing import TYPE_CHECKING

import pytest
import yaml

from agentshore.agents import pricing as pricing_mod
from agentshore.agents.pricing import (
    AgentPricing,
    PriceBook,
    bundled_pricebook,
    default_quote,
    load_pricebook,
)
from agentshore.errors import ConfigError

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Bundled load
# ---------------------------------------------------------------------------


def test_bundled_pricing_yaml_is_resource_readable() -> None:
    """The wheel ships pricing.yaml and it parses (guards packaging)."""
    ref = importlib.resources.files("agentshore.data").joinpath("pricing.yaml")
    data = yaml.safe_load(ref.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "models" in data and "agent_defaults" in data and "default" in data


def test_load_pricebook_has_expected_structure() -> None:
    pb = load_pricebook()
    assert {"claude_code", "codex", "grok", "antigravity"} <= set(pb.agent_defaults)
    assert "sonnet" in pb.models
    assert pb.cache_read_multiplier == 0.1
    assert pb.cache_write_multiplier == 1.25
    assert isinstance(pb.default, AgentPricing)


# ---------------------------------------------------------------------------
# Resolution precedence
# ---------------------------------------------------------------------------


def test_resolve_prefers_per_model_entry() -> None:
    pb = load_pricebook()
    sonnet = pb.resolve("claude_code", "sonnet")
    assert sonnet is pb.models["sonnet"]
    assert sonnet.cost_per_1k_output == 0.015


def test_resolve_falls_back_to_agent_default_for_unlisted_model() -> None:
    pb = load_pricebook()
    # A real Anthropic model id not enumerated in `models:` → agent default.
    resolved = pb.resolve("claude_code", "claude-opus-4-8")
    assert resolved == pb.agent_defaults["claude_code"]


def test_resolve_falls_back_to_global_default_for_unknown_agent() -> None:
    pb = load_pricebook()
    resolved = pb.resolve("totally-unknown", "mystery-model")
    assert resolved == pb.default


def test_resolve_with_no_model_uses_agent_default_without_warning() -> None:
    pb = load_pricebook()
    pricing_mod._WARNED_FALLBACKS.clear()
    resolved = pb.resolve("codex", None)
    assert resolved == pb.agent_defaults["codex"]
    # model=None is the expected pre-dispatch path, not a gap → no warning.
    assert not pricing_mod._WARNED_FALLBACKS


def test_unlisted_model_warns_once() -> None:
    pb = load_pricebook()
    pricing_mod._WARNED_FALLBACKS.clear()
    pb.resolve("claude_code", "brand-new-model")
    pb.resolve("claude_code", "brand-new-model")
    assert ("claude_code", "brand-new-model") in pricing_mod._WARNED_FALLBACKS
    assert len(pricing_mod._WARNED_FALLBACKS) == 1


def test_quote_bundles_multipliers() -> None:
    pb = load_pricebook()
    quote = pb.quote("claude_code", "sonnet")
    assert quote.pricing == pb.models["sonnet"]
    assert quote.cache_read_multiplier == pb.cache_read_multiplier
    assert quote.cache_write_multiplier == pb.cache_write_multiplier


def test_default_quote_is_global_default() -> None:
    quote = default_quote()
    assert quote.pricing == bundled_pricebook().default


# ---------------------------------------------------------------------------
# Global override (single touchpoint) — deep merge
# ---------------------------------------------------------------------------


def _point_global_pricing_at(monkeypatch: pytest.MonkeyPatch, path: Path, payload: object) -> None:
    path.write_text(yaml.dump(payload), encoding="utf-8")
    monkeypatch.setattr(pricing_mod, "GLOBAL_PRICING_PATH", path)


def test_global_override_deep_merges_over_bundled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _point_global_pricing_at(
        monkeypatch,
        tmp_path / "pricing.yaml",
        {
            "models": {
                # Override one existing model and add a brand new one.
                "sonnet": {
                    "max_context": 200000,
                    "cost_per_1k_input": 0.999,
                    "cost_per_1k_output": 1.5,
                },
                "claude-opus-4-8": {
                    "max_context": 1000000,
                    "cost_per_1k_input": 0.005,
                    "cost_per_1k_output": 0.025,
                },
            }
        },
    )
    pb = load_pricebook()
    # Overridden value wins.
    assert pb.models["sonnet"].cost_per_1k_input == 0.999
    # New model is now present.
    assert pb.resolve("claude_code", "claude-opus-4-8").cost_per_1k_output == 0.025
    # Untouched bundled entries survive the merge.
    assert "haiku" in pb.models
    assert pb.agent_defaults["codex"].cost_per_1k_input == 0.00175


def test_global_override_can_change_cache_multipliers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _point_global_pricing_at(
        monkeypatch, tmp_path / "pricing.yaml", {"cache_read_multiplier": 0.25}
    )
    pb = load_pricebook()
    assert pb.cache_read_multiplier == 0.25
    # Unspecified multiplier keeps the bundled default.
    assert pb.cache_write_multiplier == 1.25


def test_bundled_pricebook_ignores_global_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cached bundled book (the bare-RuntimeConfig default) is deterministic."""
    bundled_pricebook.cache_clear()
    _point_global_pricing_at(
        monkeypatch,
        tmp_path / "pricing.yaml",
        {
            "models": {
                "sonnet": {"max_context": 1, "cost_per_1k_input": 9.9, "cost_per_1k_output": 9.9}
            }
        },
    )
    assert bundled_pricebook().models["sonnet"].cost_per_1k_input == 0.003
    assert load_pricebook().models["sonnet"].cost_per_1k_input == 9.9


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_negative_rate_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _point_global_pricing_at(
        monkeypatch,
        tmp_path / "pricing.yaml",
        {
            "models": {
                "sonnet": {
                    "max_context": 200000,
                    "cost_per_1k_input": -1,
                    "cost_per_1k_output": 0.1,
                }
            }
        },
    )
    with pytest.raises(ConfigError, match="cost_per_1k_input"):
        load_pricebook()


def test_missing_required_rate_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _point_global_pricing_at(
        monkeypatch,
        tmp_path / "pricing.yaml",
        {"models": {"sonnet": {"max_context": 200000, "cost_per_1k_input": 0.1}}},
    )
    with pytest.raises(ConfigError, match="cost_per_1k_output is required"):
        load_pricebook()


def test_non_mapping_root_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _point_global_pricing_at(monkeypatch, tmp_path / "pricing.yaml", ["not", "a", "mapping"])
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_pricebook()


def test_bad_max_context_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _point_global_pricing_at(
        monkeypatch,
        tmp_path / "pricing.yaml",
        {
            "models": {
                "sonnet": {"max_context": 0, "cost_per_1k_input": 0.1, "cost_per_1k_output": 0.2}
            }
        },
    )
    with pytest.raises(ConfigError, match="max_context"):
        load_pricebook()


def test_build_pricebook_rejects_missing_default() -> None:
    with pytest.raises(ConfigError, match="default"):
        pricing_mod._build_pricebook({"models": {}, "agent_defaults": {}})


def test_pricebook_is_immutable() -> None:
    pb = load_pricebook()
    with pytest.raises(TypeError):
        pb.models["x"] = AgentPricing(1, 0.1, None, None, 0.2)  # type: ignore[index]
    assert isinstance(pb, PriceBook)
