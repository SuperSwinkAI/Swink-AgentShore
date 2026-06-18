"""Tests for agent model-tier defaults and resolution."""

from __future__ import annotations

from agentshore.agents.model_tiers import (
    DEFAULT_MODEL_TIER,
    MODEL_TIER_ORDER,
    REASONING_EFFORTS,
    configured_model_tier_coverage,
    default_model_tiers_for,
    effective_model_tier_config,
    enabled_model_tiers,
    missing_required_model_tiers,
    reasoning_efforts_for,
)
from agentshore.config.models import AgentConfig, ModelTierConfig
from agentshore.state import AgentType


def test_default_model_tiers_for_claude_code() -> None:
    tiers = default_model_tiers_for(AgentType.CLAUDE_CODE)

    assert set(tiers) == {"small", "medium", "large"}
    assert tiers["small"].model == "haiku"
    assert tiers["small"].reasoning_effort == "low"
    assert tiers["medium"].model == "sonnet"
    assert tiers["medium"].reasoning_effort == "medium"
    assert tiers["large"].model == "opus"
    assert tiers["large"].reasoning_effort == "high"
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
    assert tiers["medium"].model == "gpt-5.4"
    assert tiers["medium"].reasoning_effort == "medium"
    assert tiers["large"].model == "gpt-5.5"
    assert tiers["large"].reasoning_effort == "high"



def test_default_model_tiers_for_grok() -> None:
    tiers = default_model_tiers_for(AgentType.GROK)

    assert set(tiers) == {"small", "medium", "large"}
    assert tiers["small"].model == "grok-build"
    assert tiers["small"].reasoning_effort == "low"
    assert tiers["medium"].model == "grok-build"
    assert tiers["medium"].reasoning_effort == "medium"
    assert tiers["large"].model == "grok-build"
    assert tiers["large"].reasoning_effort == "high"


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


def test_configured_model_tier_coverage_uses_effective_enabled_tiers() -> None:
    agents = {
        "claude_code": AgentConfig(
            model_tiers={
                "small": ModelTierConfig(enabled=True),
                "medium": ModelTierConfig(enabled=False),
            }
        ),
        "codex": AgentConfig(
            model_tiers={
                "large": ModelTierConfig(enabled=True),
            }
        ),
        "grok": AgentConfig(enabled=False),
    }

    assert configured_model_tier_coverage(agents) == frozenset({"small", "large"})
    assert missing_required_model_tiers(agents) == ("medium",)


def test_missing_required_model_tiers_treats_legacy_model_as_medium_only() -> None:
    agents = {"claude_code": AgentConfig(model="claude-sonnet-custom")}

    assert configured_model_tier_coverage(agents) == frozenset({DEFAULT_MODEL_TIER})
    assert missing_required_model_tiers(agents) == ("small", "large")


def test_missing_required_model_tiers_allows_coverage_across_agent_types() -> None:
    agents = {
        "claude_code": AgentConfig(model_tiers={"small": ModelTierConfig(enabled=True)}),
        "codex": AgentConfig(model_tiers={"medium": ModelTierConfig(enabled=True)}),
        "grok": AgentConfig(model_tiers={"large": ModelTierConfig(enabled=True)}),
    }

    assert configured_model_tier_coverage(agents) == frozenset(MODEL_TIER_ORDER)
    assert missing_required_model_tiers(agents) == ()


def test_missing_required_model_tiers_reports_single_missing_small() -> None:
    agents = {
        "claude_code": AgentConfig(model_tiers={"medium": ModelTierConfig(enabled=True)}),
        "codex": AgentConfig(model_tiers={"large": ModelTierConfig(enabled=True)}),
    }

    assert missing_required_model_tiers(agents) == ("small",)


def test_missing_required_model_tiers_reports_single_missing_large() -> None:
    agents = {
        "claude_code": AgentConfig(model_tiers={"small": ModelTierConfig(enabled=True)}),
        "codex": AgentConfig(model_tiers={"medium": ModelTierConfig(enabled=True)}),
    }

    assert missing_required_model_tiers(agents) == ("large",)


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


# ---------------------------------------------------------------------------
# REASONING_EFFORTS / reasoning_efforts_for
# ---------------------------------------------------------------------------


def test_reasoning_efforts_claude_code_has_five_values() -> None:
    efforts = reasoning_efforts_for(AgentType.CLAUDE_CODE)

    assert efforts == ("low", "medium", "high", "xhigh", "max")
    assert len(efforts) == 5


def test_reasoning_efforts_grok_has_five_values() -> None:
    efforts = reasoning_efforts_for(AgentType.GROK)

    assert efforts == ("low", "medium", "high", "xhigh", "max")
    assert len(efforts) == 5


def test_reasoning_efforts_codex_includes_minimal() -> None:
    efforts = reasoning_efforts_for(AgentType.CODEX)

    assert efforts[0] == "minimal"
    assert efforts == ("minimal", "low", "medium", "high", "xhigh")
    assert len(efforts) == 5


def test_reasoning_efforts_antigravity_is_empty() -> None:
    assert reasoning_efforts_for(AgentType.ANTIGRAVITY) == ()


def test_reasoning_efforts_constant_matches_helper() -> None:
    for agent_type in AgentType:
        assert reasoning_efforts_for(agent_type) == REASONING_EFFORTS.get(agent_type, ())
