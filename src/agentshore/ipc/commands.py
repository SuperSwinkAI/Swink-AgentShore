"""Inbound IPC command parser and validator."""

from __future__ import annotations

import json
import math
from typing import TypeGuard

VALID_COMMANDS: frozenset[str] = frozenset(
    {
        "start",
        "pause",
        "resume",
        "shutdown",
        "drain",
        "hard_stop",
        "add_budget",
        "rescan_issues",
        "feedback_response",
        "generate_report",
        "abort_play",
        "verification_response",
        "archive_session",
        "list_archives",
        "get_state",
        "reload_config",
    }
)

# Required params per command (empty = no required params beyond "command")
_REQUIRED_PARAMS: dict[str, frozenset[str]] = {
    "feedback_response": frozenset({"action"}),
    "verification_response": frozenset({"checkpoint_id", "passed"}),
}

_BOOL_PARAMS: dict[str, frozenset[str]] = {
    "drain": frozenset({"end_session_report", "open_report"}),
}

# Commands that require at least one of a set of optional positive-number params.
# Each named field, when present, must be a finite positive number; ``delta_minutes``
# must additionally be a whole number of minutes (an int, or a float with no
# fractional part). At least one of the fields must be present.
_AT_LEAST_ONE_POSITIVE_PARAMS: dict[str, frozenset[str]] = {
    "add_budget": frozenset({"delta_usd", "delta_minutes"}),
}
_INTEGER_POSITIVE_PARAMS: frozenset[str] = frozenset({"delta_minutes"})


def _is_positive_number(val: object) -> TypeGuard[int | float]:
    return (
        isinstance(val, (int, float))
        and not isinstance(val, bool)
        and math.isfinite(val)
        and val > 0
    )


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

    at_least_one = _AT_LEAST_ONE_POSITIVE_PARAMS.get(str(command), frozenset())
    if at_least_one:
        present = at_least_one & cmd.keys()
        if not present:
            raise ValueError(
                f"Command {command!r} requires at least one of: {sorted(at_least_one)}"
            )
        for field in present:
            val = cmd.get(field)
            if not _is_positive_number(val):
                raise ValueError(
                    f"Command '{command}': '{field}' must be a finite positive number, got {val!r}"
                )
            if field in _INTEGER_POSITIVE_PARAMS and float(val) != int(val):
                raise ValueError(
                    f"Command '{command}': '{field}' must be a whole number of minutes, got {val!r}"
                )

    for field in _BOOL_PARAMS.get(str(command), frozenset()):
        if field in cmd and not isinstance(cmd[field], bool):
            raise ValueError(
                f"Command '{command}': '{field}' must be a boolean, got {cmd[field]!r}"
            )
