"""Agent capability registry — static per-type capability declarations."""

from __future__ import annotations

from agentshore.state import AgentType

# Keys: AgentType enum, values: capability dict.
# max_context and cost values are used for observation-builder and cost estimation.
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
        "cost_per_1k_input": 0.003,
        "cost_per_1k_output": 0.015,
    },
    AgentType.CODEX: {
        "can_implement": True,
        "can_review": True,
        "can_test": True,
        "can_create_pr": True,
        "can_merge": True,
        "can_run_skill": True,
        "max_context": 400_000,
        "cost_per_1k_input": 0.00175,
        "cost_per_1k_output": 0.014,
    },
    AgentType.GEMINI: {
        "can_implement": True,
        "can_review": True,
        "can_test": True,
        "can_create_pr": True,
        "can_merge": True,
        "can_run_skill": True,
        "max_context": 1_000_000,
        "cost_per_1k_input": 0.0005,
        "cost_per_1k_output": 0.003,
    },
    AgentType.GROK: {
        "can_implement": True,
        "can_review": True,
        "can_test": True,
        "can_create_pr": True,
        "can_merge": True,
        "can_run_skill": True,
        "max_context": 256_000,
        "cost_per_1k_input": 0.001,
        "cost_per_1k_output": 0.002,
    },
}


def get_capability(agent_type: AgentType, key: str) -> object:
    """Return the value of *key* for *agent_type*, raising KeyError if unknown."""
    return AGENT_CAPABILITIES[agent_type][key]
