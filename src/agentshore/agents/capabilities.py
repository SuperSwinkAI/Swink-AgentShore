"""Agent capability registry — static per-type capability declarations."""

from __future__ import annotations

from agentshore.state import AgentType

# max_context feeds the observation builder; cost estimation reads pricing from
# AgentConfig (see agentshore.agents.costs), not this registry.
# can_merge is GitHub plumbing, not a provider-specific capability — kept on every
# type so availability can't strand approved PRs behind a saturated provider.
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
    AgentType.GROK: {
        "can_implement": True,
        "can_review": True,
        "can_test": True,
        "can_create_pr": True,
        "can_merge": True,
        "can_run_skill": True,
        "max_context": 256_000,
    },
    AgentType.ANTIGRAVITY: {
        "can_implement": True,
        "can_review": True,
        "can_test": True,
        "can_create_pr": True,
        "can_merge": True,
        "can_run_skill": True,
        "max_context": 1_000_000,
    },
    AgentType.SWINK_CODING: {
        "can_implement": True,
        "can_review": True,
        "can_test": True,
        "can_create_pr": True,
        "can_merge": True,
        "can_run_skill": True,
        "max_context": 32_768,
    },
}


def get_capability(agent_type: AgentType, key: str) -> object:
    """Return the value of *key* for *agent_type*, raising KeyError if unknown."""
    return AGENT_CAPABILITIES[agent_type][key]
