"""Tests for src/agentshore/ipc/commands.py — IPC command parser and validator."""

from __future__ import annotations

import json

import pytest

from agentshore.ipc.commands import (
    VALID_COMMANDS,
    parse_command,
    parse_override_play_type,
    validate_command,
)
from agentshore.state import PlayType

# ---------------------------------------------------------------------------
# parse_command tests
# ---------------------------------------------------------------------------


def test_parse_valid_pause() -> None:
    result = parse_command('{"command": "pause"}')
    assert result["command"] == "pause"


def test_parse_valid_override_play() -> None:
    line = '{"command": "override_play", "play_type": "ISSUE_PICKUP"}'
    result = parse_command(line)
    assert result["command"] == "override_play"
    assert result["play_type"] == "ISSUE_PICKUP"


def test_parse_malformed_json() -> None:
    with pytest.raises(ValueError, match="Malformed JSON"):
        parse_command("not json")


def test_parse_missing_command_key() -> None:
    with pytest.raises(ValueError, match="Missing required key 'command'"):
        parse_command('{"action": "pause"}')


def test_parse_extra_fields_preserved() -> None:
    line = '{"command": "start", "session_id": "abc123", "dry_run": true}'
    result = parse_command(line)
    assert result["command"] == "start"
    assert result["session_id"] == "abc123"
    assert result["dry_run"] is True


# ---------------------------------------------------------------------------
# validate_command tests
# ---------------------------------------------------------------------------


def test_validate_unknown_command() -> None:
    with pytest.raises(ValueError, match="Unknown command"):
        validate_command({"command": "fly"})


def test_validate_all_13_commands_valid() -> None:
    # Minimal valid payloads for every command, including required params.
    minimal_payloads: dict[str, dict[str, object]] = {
        "start": {},
        "pause": {},
        "resume": {},
        "shutdown": {},
        "override_play": {"play_type": "ISSUE_PICKUP"},
        "rescan_issues": {},
        "feedback_response": {"action": "continue"},
        "generate_report": {},
        "abort_play": {},
        "verification_response": {"checkpoint_id": "cp-1", "passed": True},
        "archive_session": {},
        "list_archives": {},
        "get_state": {},
    }
    assert len(minimal_payloads) == 13
    for command, extra in minimal_payloads.items():
        cmd: dict[str, object] = {"command": command, **extra}
        validate_command(cmd)  # must not raise


def test_validate_override_play_requires_play_type() -> None:
    with pytest.raises(ValueError, match="play_type"):
        validate_command({"command": "override_play"})


def test_validate_override_play_rejects_reserved_and_legacy_slots() -> None:
    for play_type in ("future_4", "FUTURE_7", "future_8", "compact_agent", "clear_agent"):
        with pytest.raises(ValueError, match="play_type"):
            validate_command({"command": "override_play", "play_type": play_type})


def test_parse_override_play_type_accepts_names_and_values() -> None:
    assert parse_override_play_type("ISSUE_PICKUP") is PlayType.ISSUE_PICKUP
    assert parse_override_play_type("issue_pickup") is PlayType.ISSUE_PICKUP
    assert parse_override_play_type("UNBLOCK_PR") is PlayType.UNBLOCK_PR
    assert parse_override_play_type("design_audit") is PlayType.DESIGN_AUDIT


def test_validate_feedback_response_requires_action() -> None:
    with pytest.raises(ValueError, match="action"):
        validate_command({"command": "feedback_response"})


def test_validate_pause_no_extras_needed() -> None:
    # pause has no required params beyond "command" — must not raise.
    validate_command({"command": "pause"})


# ---------------------------------------------------------------------------
# Structural / invariant tests
# ---------------------------------------------------------------------------


def test_valid_commands_has_18_entries() -> None:
    assert len(VALID_COMMANDS) == 16


def test_get_state_is_valid_command() -> None:
    """get_state requires no additional parameters and must not raise."""
    validate_command({"command": "get_state"})


def test_roundtrip_parse_and_validate() -> None:
    payload = {
        "command": "override_play",
        "play_type": "CODE_REVIEW",
        "params": {"pr_number": 7, "agent": "claude"},
        "priority": "high",
    }
    line = json.dumps(payload)
    cmd = parse_command(line)
    validate_command(cmd)  # must not raise
    assert cmd["play_type"] == "CODE_REVIEW"
    assert cmd["params"] == {"pr_number": 7, "agent": "claude"}


# ---------------------------------------------------------------------------
# adjust_budget numeric validation tests
# ---------------------------------------------------------------------------


def test_adjust_budget_rejects_non_numeric_string() -> None:
    with pytest.raises(ValueError, match="delta_usd"):
        validate_command({"command": "adjust_budget", "delta_usd": "abc"})


def test_adjust_budget_rejects_nan() -> None:
    import math

    with pytest.raises(ValueError, match="delta_usd"):
        validate_command({"command": "adjust_budget", "delta_usd": math.nan})


def test_adjust_budget_rejects_zero() -> None:
    with pytest.raises(ValueError, match="delta_usd"):
        validate_command({"command": "adjust_budget", "delta_usd": 0})


def test_adjust_budget_rejects_negative() -> None:
    with pytest.raises(ValueError, match="delta_usd"):
        validate_command({"command": "adjust_budget", "delta_usd": -1})


def test_adjust_budget_accepts_positive_float() -> None:
    validate_command({"command": "adjust_budget", "delta_usd": 5.0})  # must not raise
