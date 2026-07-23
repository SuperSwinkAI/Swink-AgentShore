"""Agent capability registry — static per-type capability declarations.

Historically this registry also carried five ``can_*`` booleans
(``can_implement``, ``can_review``, ``can_test``, ``can_create_pr``,
``can_merge``, ``can_run_skill``) per agent type. Every one of them was
``True`` for every :class:`AgentType` — the registry never actually
discriminated capability by provider, so every ``AGENT_CAPABILITIES.get(...).
get(cap_key, False)`` gate they fed provably always passed. Per the project
policy against hardcoding per-(agent_type, play_type) capability blocklists —
providers are assumed to reach parity — the flags were removed rather than
kept as dead weight (see the wave-2 TNQA cleanup). ``max_context`` is the only
per-type datum that ever varied, so it is the only field left here.
"""

from __future__ import annotations

from agentshore.state import AgentType

# max_context feeds the observation builder; cost estimation reads pricing from
# AgentConfig (see agentshore.agents.costs), not this registry.
AGENT_CAPABILITIES: dict[AgentType, dict[str, object]] = {
    AgentType.CLAUDE_CODE: {
        "max_context": 200_000,
    },
    AgentType.CODEX: {
        "max_context": 400_000,
    },
    AgentType.GROK: {
        "max_context": 256_000,
    },
    AgentType.ANTIGRAVITY: {
        "max_context": 1_000_000,
    },
    AgentType.SWINK_CODING: {
        "max_context": 32_768,
    },
}


def get_capability(agent_type: AgentType, key: str) -> object:
    """Return the value of *key* for *agent_type*, raising KeyError if unknown."""
    return AGENT_CAPABILITIES[agent_type][key]
