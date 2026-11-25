"""Single canonical binary-name → AgentType registry.

Adding a new CLI agent type requires one
entry in ``BINARY_TO_AGENT_TYPE``; the derived ``AgentType``-keyed views are
computed automatically."""

from __future__ import annotations

from agentshore.state import AgentType

BINARY_TO_AGENT_TYPE: dict[str, AgentType] = {
    "claude": AgentType.CLAUDE_CODE,
    "codex": AgentType.CODEX,
    "grok": AgentType.GROK,
    "grok-build": AgentType.GROK,
    "agy": AgentType.ANTIGRAVITY,
    "swink-coding": AgentType.SWINK_CODING,
    "copilot": AgentType.COPILOT,
}

AGENT_TYPE_TO_BINARY: dict[AgentType, str] = {
    AgentType.CLAUDE_CODE: "claude",
    AgentType.CODEX: "codex",
    AgentType.GROK: "grok",
    AgentType.ANTIGRAVITY: "agy",
    AgentType.SWINK_CODING: "swink-coding",
    AgentType.COPILOT: "copilot",
}

AGENT_TYPE_DISPLAY_PREFIX: dict[AgentType, str] = {
    AgentType.CLAUDE_CODE: "Claude",
    AgentType.CODEX: "Codex",
    AgentType.GROK: "Grok",
    AgentType.ANTIGRAVITY: "Antigravity",
    AgentType.SWINK_CODING: "Swink",
    AgentType.COPILOT: "Copilot",
}

BINARY_TO_AGENT_KEY: dict[str, str] = {
    binary: agent_type.value for binary, agent_type in BINARY_TO_AGENT_TYPE.items()
}

SUPPORTED_CLI_AGENT_KEYS: frozenset[str] = frozenset(BINARY_TO_AGENT_KEY.values())

_missing = [
    t for t in AgentType if t not in AGENT_TYPE_TO_BINARY or t not in AGENT_TYPE_DISPLAY_PREFIX
]
if _missing:  # pragma: no cover - import-time invariant
    raise RuntimeError(f"agents.registry maps missing AgentType entries: {_missing}")
