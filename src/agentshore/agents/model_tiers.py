"""Model-tier defaults and helpers for agent instantiation."""

from __future__ import annotations

from agentshore.config.models import AgentConfig, ModelTierConfig
from agentshore.state import AgentType

DEFAULT_MODEL_TIER = "medium"
# Spawn order for INSTANTIATE_AGENT round-robin: medium first (workhorse, runs
# every play), then small (cheap-band coverage), then large (high-complexity).
MODEL_TIER_PRIORITY: tuple[str, ...] = ("medium", "small", "large")
MODEL_TIER_ORDER: tuple[str, ...] = ("small", "medium", "large")

DEFAULT_MODEL_TIERS: dict[AgentType, dict[str, ModelTierConfig]] = {
    AgentType.CLAUDE_CODE: {
        "small": ModelTierConfig(model="haiku"),
        "medium": ModelTierConfig(model="sonnet"),
        "large": ModelTierConfig(model="opus"),
    },
    AgentType.CODEX: {
        "small": ModelTierConfig(model="gpt-5.4-mini", reasoning_effort="low"),
        "medium": ModelTierConfig(model="gpt-5.3-codex", reasoning_effort="medium"),
        "large": ModelTierConfig(model="gpt-5.5", reasoning_effort="high"),
    },
    AgentType.GEMINI: {
        "small": ModelTierConfig(model="flash-lite"),
        "medium": ModelTierConfig(model="auto"),
        "large": ModelTierConfig(model="pro"),
    },
}


def default_model_tiers_for(agent_type: AgentType) -> dict[str, ModelTierConfig]:
    """Return the pinned tier map for an agent type."""
    return dict(DEFAULT_MODEL_TIERS.get(agent_type, {}))


def enabled_model_tiers(agent_type: AgentType, agent_cfg: AgentConfig) -> tuple[str, ...]:
    """Return enabled tiers in the order AgentShore should instantiate them."""
    if agent_cfg.model_tiers:
        return tuple(
            tier
            for tier in MODEL_TIER_PRIORITY
            if tier in agent_cfg.model_tiers and agent_cfg.model_tiers[tier].enabled
        )

    if agent_cfg.model or agent_cfg.reasoning_effort or agent_cfg.approved_models:
        return (DEFAULT_MODEL_TIER,)

    defaults = DEFAULT_MODEL_TIERS.get(agent_type, {})
    return tuple(tier for tier in MODEL_TIER_PRIORITY if tier in defaults)


def effective_model_tier_config(
    agent_type: AgentType,
    agent_cfg: AgentConfig,
    model_tier: str | None,
) -> ModelTierConfig:
    """Resolve the concrete model settings for an agent tier.

    Explicit ``model_tiers`` entries win. Legacy top-level ``model`` and
    ``reasoning_effort`` are preserved for the default medium-tier equivalent.
    """
    tier = model_tier or DEFAULT_MODEL_TIER
    default = DEFAULT_MODEL_TIERS.get(agent_type, {}).get(tier, ModelTierConfig())
    configured = agent_cfg.model_tiers.get(tier)

    if configured is not None:
        return ModelTierConfig(
            enabled=configured.enabled,
            model=configured.model or default.model,
            reasoning_effort=configured.reasoning_effort or default.reasoning_effort,
        )

    if tier == DEFAULT_MODEL_TIER and (agent_cfg.model or agent_cfg.reasoning_effort):
        return ModelTierConfig(
            model=agent_cfg.model or default.model,
            reasoning_effort=agent_cfg.reasoning_effort or default.reasoning_effort,
        )

    return default
