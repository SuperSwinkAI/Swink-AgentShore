"""Single canonical binary-name → AgentType registry.

Previously the binary→agent-type mapping lived in four separate places:
  - ``cli/constants.py:_AGENT_KEY_BY_BINARY``  (str→str, used by agent_select wizard)
  - ``cli_helpers.py:208``                     (inline ternary in render_yaml)

All callers now import from here.  Adding a new CLI agent type requires one
entry in ``BINARY_TO_AGENT_TYPE``; the derived ``AgentType``-keyed views are
computed automatically.
"""

from __future__ import annotations

from agentshore.state import AgentType

# Canonical map: every binary name (including aliases) → AgentType.
# Used by the CLI wizard to resolve a detected binary to an agent key, and by
# the YAML renderer to translate ``binary`` fields back to agent config keys.
BINARY_TO_AGENT_TYPE: dict[str, AgentType] = {
    "claude": AgentType.CLAUDE_CODE,
    "codex": AgentType.CODEX,
    "grok": AgentType.GROK,
    "grok-build": AgentType.GROK,
    "agy": AgentType.ANTIGRAVITY,
}

# Inverse: AgentType → canonical binary name (first/primary name only).
# Used when a default binary name is needed for a given agent type.
AGENT_TYPE_TO_BINARY: dict[AgentType, str] = {
    AgentType.CLAUDE_CODE: "claude",
    AgentType.CODEX: "codex",
    AgentType.GROK: "grok",
    AgentType.ANTIGRAVITY: "agy",
}

# String-keyed variants for callers that compare against AgentType.value
# (e.g. CLI constants, YAML keys). These are derived from the above so they
# stay in sync automatically.
BINARY_TO_AGENT_KEY: dict[str, str] = {
    binary: agent_type.value for binary, agent_type in BINARY_TO_AGENT_TYPE.items()
}

SUPPORTED_CLI_AGENT_KEYS: frozenset[str] = frozenset(BINARY_TO_AGENT_KEY.values())
