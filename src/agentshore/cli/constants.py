"""Shared constants for the AgentShore CLI."""

from __future__ import annotations

from agentshore.config.models import RunMode

# Socket-wait constants used by ``_launch_dashboard_background``. We poll the
# Unix-socket path that the orchestrator subprocess will create on startup.
# Splitting the timeout from the poll interval keeps each independently
# tunable and self-documenting.
_SOCKET_WAIT_TIMEOUT_S: float = 15.0
_SOCKET_POLL_INTERVAL_S: float = 0.25
_SOCKET_WAIT_RETRIES: int = int(_SOCKET_WAIT_TIMEOUT_S / _SOCKET_POLL_INTERVAL_S)

# Graceful ``agentshore stop`` drain wait before the CLI escalates to a hard stop.
_DRAIN_WAIT_TIMEOUT_S: float = 15 * 60.0
_DRAIN_WAIT_POLL_INTERVAL_S: float = 0.5
_DRAIN_WAIT_RETRIES: int = int(_DRAIN_WAIT_TIMEOUT_S / _DRAIN_WAIT_POLL_INTERVAL_S)

_START_MODE_TUI = "tui"
_START_MODE_AGENT = RunMode.AGENT.value

_BYPASS_FLAGS: dict[str, tuple[str, ...]] = {
    "claude_code": ("--dangerously-skip-permissions",),
    "codex": ("--dangerously-bypass-approvals-and-sandbox",),
    "gemini": ("--approval-mode=yolo", "--skip-trust"),
}
_AGENT_KEY_BY_BINARY: dict[str, str] = {
    "claude": "claude_code",
    "codex": "codex",
    "gemini": "gemini",
}
_SUPPORTED_CLI_AGENT_KEYS: frozenset[str] = frozenset(_AGENT_KEY_BY_BINARY.values())

_CUSTOM_MODEL_SENTINEL = "[ enter custom model... ]"
