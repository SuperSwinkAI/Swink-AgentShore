"""Tests for agent model-tier defaults and resolution."""

from __future__ import annotations

from agentshore.agents.model_tiers import (
    DEFAULT_MODEL_TIER,
    default_model_tiers_for,
    effective_model_tier_config,
    enabled_model_tiers,
)
from agentshore.config.models import AgentConfig, ModelTierConfig
from agentshore.state import AgentType


def test_default_model_tiers_for_claude_code() -> None:
    tiers = default_model_tiers_for(AgentType.CLAUDE_CODE)

    assert set(tiers) == {"small", "medium", "large"}
    assert tiers["medium"].model == "sonnet"
    assert tiers["large"].model == "opus"
    assert tiers["large"].enabled is True


def test_default_model_tiers_returns_copy() -> None:
    tiers = default_model_tiers_for(AgentType.CODEX)
    tiers.clear()

    assert default_model_tiers_for(AgentType.CODEX)


def test_default_model_tiers_for_codex() -> None:
    tiers = default_model_tiers_for(AgentType.CODEX)

    assert set(tiers) == {"small", "medium", "large"}
    assert tiers["small"].model == "gpt-5.4-mini"
    assert tiers["small"].reasoning_effort == "low"
    assert tiers["medium"].model == "gpt-5.3-codex"
    assert tiers["medium"].reasoning_effort == "medium"
    assert tiers["large"].model == "gpt-5.5"
    assert tiers["large"].reasoning_effort == "high"


def test_default_model_tiers_for_gemini() -> None:
    tiers = default_model_tiers_for(AgentType.GEMINI)

    assert set(tiers) == {"small", "medium", "large"}
    assert tiers["small"].enabled is True
    assert tiers["small"].model == "flash-lite"
    assert tiers["medium"].model == "auto"
    assert tiers["large"].model == "pro"


def test_enabled_model_tiers_respects_agent_config() -> None:
    cfg = AgentConfig(
        model_tiers={
            "small": ModelTierConfig(enabled=True),
            "medium": ModelTierConfig(enabled=False),
        }
    )

    assert enabled_model_tiers(AgentType.CLAUDE_CODE, cfg) == ("small",)


def test_enabled_model_tiers_uses_legacy_model_as_default_tier() -> None:
    cfg = AgentConfig(model="claude-sonnet-custom")

    assert enabled_model_tiers(AgentType.CLAUDE_CODE, cfg) == (DEFAULT_MODEL_TIER,)


def test_effective_model_tier_config_merges_settings() -> None:
    cfg = AgentConfig(
        model_tiers={
            "medium": ModelTierConfig(
                enabled=True,
                model="gpt-5.5-custom",
                reasoning_effort="high",
            )
        }
    )

    resolved = effective_model_tier_config(AgentType.CODEX, cfg, "medium")

    assert resolved.enabled is True
    assert resolved.model == "gpt-5.5-custom"
    assert resolved.reasoning_effort == "high"


def test_effective_model_tier_config_preserves_legacy_top_level_settings() -> None:
    cfg = AgentConfig(model="custom-model", reasoning_effort="xhigh")

    resolved = effective_model_tier_config(AgentType.CODEX, cfg, None)

    assert resolved.model == "custom-model"
    assert resolved.reasoning_effort == "xhigh"
