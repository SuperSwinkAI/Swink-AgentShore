"""Model-tier defaults and helpers for agent instantiation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.config.models import AgentConfig, ModelTierConfig
from agentshore.state import AgentType

if TYPE_CHECKING:
    from collections.abc import Mapping

DEFAULT_MODEL_TIER = "medium"
# Spawn order for INSTANTIATE_AGENT round-robin: medium first (workhorse, runs
# every play), then small (cheap-band coverage), then large (high-complexity).
MODEL_TIER_PRIORITY: tuple[str, ...] = ("medium", "small", "large")
MODEL_TIER_ORDER: tuple[str, ...] = ("small", "medium", "large")
REQUIRED_MODEL_TIERS: tuple[str, ...] = MODEL_TIER_ORDER

DEFAULT_MODEL_TIERS: dict[AgentType, dict[str, ModelTierConfig]] = {
    AgentType.CLAUDE_CODE: {
        "small": ModelTierConfig(model="haiku", reasoning_effort="low"),
        "medium": ModelTierConfig(model="sonnet", reasoning_effort="medium"),
        "large": ModelTierConfig(model="opus", reasoning_effort="high"),
    },
    AgentType.CODEX: {
        # gpt-5.x (non-``-codex``) ids are the ChatGPT-account-compatible line.
        # The ``-codex`` suffixed ids are API-key only and
        # return HTTP 400 "not supported when using Codex with a ChatGPT
        # account", so they must not be defaults.
        "small": ModelTierConfig(model="gpt-5.4-mini", reasoning_effort="low"),
        "medium": ModelTierConfig(model="gpt-5.4", reasoning_effort="medium"),
        "large": ModelTierConfig(model="gpt-5.5", reasoning_effort="high"),
    },
    AgentType.GROK: {
        "small": ModelTierConfig(model="grok-build", reasoning_effort="low"),
        "medium": ModelTierConfig(model="grok-build", reasoning_effort="medium"),
        "large": ModelTierConfig(model="grok-build", reasoning_effort="high"),
    },
    AgentType.ANTIGRAVITY: {
        # Reasoning effort is baked into the model display-name, so no
        # reasoning_effort is set (mirrors gemini — REASONING_EFFORTS is empty).
        # Small tier uses the open-weight GPT-OSS 120B backend agy exposes.
        "small": ModelTierConfig(model="GPT-OSS 120B (Medium)"),
        "medium": ModelTierConfig(model="Gemini 3.5 Flash (High)"),
        "large": ModelTierConfig(model="Gemini 3.1 Pro (High)"),
    },
}


# Canonical reasoning-effort vocabularies per agent type.  Empty tuple means
# the agent CLI has no effort flag and the field must not be set.
REASONING_EFFORTS: dict[AgentType, tuple[str, ...]] = {
    AgentType.CLAUDE_CODE: ("low", "medium", "high", "xhigh", "max"),
    AgentType.GROK: ("low", "medium", "high", "xhigh", "max"),
    AgentType.CODEX: ("minimal", "low", "medium", "high", "xhigh"),
    AgentType.ANTIGRAVITY: (),
}


def reasoning_efforts_for(agent_type: AgentType) -> tuple[str, ...]:
    """Return the valid reasoning-effort values for an agent type."""
    return REASONING_EFFORTS.get(agent_type, ())


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


def _agent_type_for_config(agent_name: str, agent_cfg: AgentConfig) -> AgentType | None:
    """Resolve a configured agent entry to a built-in AgentType when possible."""
    try:
        return AgentType(agent_name)
    except ValueError:
        pass

    if agent_cfg.binary:
        from agentshore.agents.registry import BINARY_TO_AGENT_TYPE

        return BINARY_TO_AGENT_TYPE.get(agent_cfg.binary)
    return None


def configured_model_tier_coverage(agents: Mapping[str, AgentConfig]) -> frozenset[str]:
    """Return required tiers provided by the effective enabled agent config.

    This intentionally uses :func:`enabled_model_tiers` instead of checking raw
    YAML keys so legacy top-level model config contributes only the default
    medium tier, while implicit built-in defaults contribute all default tiers.
    """
    covered: set[str] = set()
    for agent_name, agent_cfg in agents.items():
        if not agent_cfg.enabled:
            continue
        agent_type = _agent_type_for_config(agent_name, agent_cfg)
        if agent_type is None:
            continue
        covered.update(enabled_model_tiers(agent_type, agent_cfg))
    return frozenset(tier for tier in REQUIRED_MODEL_TIERS if tier in covered)


def missing_required_model_tiers(agents: Mapping[str, AgentConfig]) -> tuple[str, ...]:
    """Return required model tiers absent from the effective enabled config."""
    covered = configured_model_tier_coverage(agents)
    return tuple(tier for tier in REQUIRED_MODEL_TIERS if tier not in covered)


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
            max=configured.max,
        )

    if tier == DEFAULT_MODEL_TIER and (agent_cfg.model or agent_cfg.reasoning_effort):
        return ModelTierConfig(
            model=agent_cfg.model or default.model,
            reasoning_effort=agent_cfg.reasoning_effort or default.reasoning_effort,
        )

    return default
