"""Agent capability registry — static per-type capability declarations."""

from __future__ import annotations

from agentshore.state import AgentType

# Keys: AgentType enum, values: capability dict.
# max_context is used by the observation builder; cost estimation reads pricing
# from AgentConfig (see agentshore.agents.costs), not from this registry.
# Merging is GitHub/repository plumbing, not a provider-specific capability.
# Keep it available to every agent type so scheduler availability cannot strand
# approved PRs behind a disabled or saturated provider.
AGENT_CAPABILITIES: dict[AgentType, dict[str, object]] = {
    AgentType.CLAUDE_CODE: {
        "can_implement": True,
        "can_review": True,
        "can_test": True,
        "can_create_pr": True,
        "can_merge": True,
        "can_run_skill": True,
        "max_context": 200_000,
    },
    AgentType.CODEX: {
        "can_implement": True,
        "can_review": True,
        "can_test": True,
        "can_create_pr": True,
        "can_merge": True,
        "can_run_skill": True,
        "max_context": 400_000,
    },
    AgentType.GEMINI: {
        "can_implement": True,
        "can_review": True,
        "can_test": True,
        "can_create_pr": True,
        "can_merge": True,
        "can_run_skill": True,
        "max_context": 1_000_000,
    },
    AgentType.GROK: {
        "can_implement": True,
        "can_review": True,
        "can_test": True,
        "can_create_pr": True,
        "can_merge": True,
        "can_run_skill": True,
        "max_context": 256_000,
    },
}


def get_capability(agent_type: AgentType, key: str) -> object:
    """Return the value of *key* for *agent_type*, raising KeyError if unknown."""
    return AGENT_CAPABILITIES[agent_type][key]
