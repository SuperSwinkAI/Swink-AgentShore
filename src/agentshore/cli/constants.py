"""Shared constants for the AgentShore CLI."""

from __future__ import annotations

from agentshore.agents.registry import BINARY_TO_AGENT_KEY, SUPPORTED_CLI_AGENT_KEYS
from agentshore.config.models import RunMode

# Socket-wait constants for ``_launch_dashboard_background``: poll the socket
# path the orchestrator creates on startup. Timeout/interval split so each is
# independently tunable.
_SOCKET_WAIT_TIMEOUT_S: float = 15.0
_SOCKET_POLL_INTERVAL_S: float = 0.25
_SOCKET_WAIT_RETRIES: int = int(_SOCKET_WAIT_TIMEOUT_S / _SOCKET_POLL_INTERVAL_S)

# Poll interval for the graceful ``agentshore stop`` drain wait — no deadline
# (polls until exit or Ctrl+C), so only the interval is configurable.
_DRAIN_WAIT_POLL_INTERVAL_S: float = 0.5

_START_MODE_TUI = "tui"
_START_MODE_AGENT = RunMode.AGENT.value

_BYPASS_FLAGS: dict[str, tuple[str, ...]] = {
    "claude_code": ("--dangerously-skip-permissions",),
    "codex": ("--dangerously-bypass-approvals-and-sandbox",),
    "grok": ("--permission-mode", "bypassPermissions"),
    "antigravity": ("--dangerously-skip-permissions",),
}

# Sourced from the canonical registry — one source of truth for binary→key.
_AGENT_KEY_BY_BINARY: dict[str, str] = BINARY_TO_AGENT_KEY
_SUPPORTED_CLI_AGENT_KEYS: frozenset[str] = SUPPORTED_CLI_AGENT_KEYS

_CUSTOM_MODEL_SENTINEL = "[ enter custom model... ]"
