"""Inbound IPC command parser and validator."""

from __future__ import annotations

import json
import math

VALID_COMMANDS: frozenset[str] = frozenset(
    {
        "start",
        "pause",
        "resume",
        "shutdown",
        "drain",
        "hard_stop",
        "adjust_budget",
        "rescan_issues",
        "feedback_response",
        "generate_report",
        "abort_play",
        "verification_response",
        "archive_session",
        "list_archives",
        "get_state",
    }
)

# Required params per command (empty = no required params beyond "command")
_REQUIRED_PARAMS: dict[str, frozenset[str]] = {
    "feedback_response": frozenset({"action"}),
    "verification_response": frozenset({"checkpoint_id", "passed"}),
    "adjust_budget": frozenset({"delta_usd"}),
}

_BOOL_PARAMS: dict[str, frozenset[str]] = {
    "drain": frozenset({"end_session_report", "open_report"}),
}

_NUMERIC_POSITIVE_PARAMS: dict[str, frozenset[str]] = {
    "adjust_budget": frozenset({"delta_usd"}),
}

def parse_command(line: str) -> dict[str, object]:
    """Parse one NDJSON line into a command dict.

    Raises ValueError on malformed JSON or missing 'command' key.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object, got {type(data).__name__}")

    if "command" not in data:
        raise ValueError("Missing required key 'command'")

    return data


def validate_command(cmd: dict[str, object]) -> None:
    """Validate a parsed command dict.

    Raises ValueError on unknown command or missing required params.
    """
    command = cmd.get("command")
    if command not in VALID_COMMANDS:
        raise ValueError(f"Unknown command: {command!r}")

    required = _REQUIRED_PARAMS.get(str(command), frozenset())
    missing = required - cmd.keys()
    if missing:
        raise ValueError(f"Command {command!r} missing required parameter(s): {sorted(missing)}")

    for field in _NUMERIC_POSITIVE_PARAMS.get(str(command), frozenset()):
        val = cmd.get(field)
        if (
            not isinstance(val, (int, float))
            or isinstance(val, bool)
            or not math.isfinite(val)
            or val <= 0
        ):
            raise ValueError(
                f"Command '{command}': '{field}' must be a finite positive number, got {val!r}"
            )

    for field in _BOOL_PARAMS.get(str(command), frozenset()):
        if field in cmd and not isinstance(cmd[field], bool):
            raise ValueError(
                f"Command '{command}': '{field}' must be a boolean, got {cmd[field]!r}"
            )
