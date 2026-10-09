"""Tests for the GitHub Copilot CLI adapter (``agentshore.agents.cli_copilot``).

Fixtures are real ``copilot --output-format json`` captures from 1.0.94:
``copilot_json_1_0_94.jsonl`` (prompt over stdin, pinned ``--session-id``) and
``copilot_json_tool_denied_1_0_94.jsonl`` (``--deny-tool=write`` blocking an
``apply_patch`` call, then a final answer).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentshore.agents.cli.argv import (
    _DEFAULT_YOLO_FLAGS,
    _PINNABLE_SESSION_AGENT_TYPES,
    _RESUMABLE_AGENT_TYPES,
    _TOOL_DENIAL_CAPABLE_AGENT_TYPES,
    build_argv,
    build_resume_argv,
)
from agentshore.agents.cli.drivers import DEFAULT_CLI_DRIVERS, CopilotCliDriver
from agentshore.agents.cli.errors import _classify_error
from agentshore.agents.cli.parsing import _PARSERS, _is_terminal_event
from agentshore.agents.cli.watchdogs import _FIRST_BYTE_DEADLINE_BY_TYPE
from agentshore.agents.cli_copilot import parse_copilot_jsonl
from agentshore.errors import ErrorClass
from agentshore.state import CLI_AGENT_TYPES, AgentType

_FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


# --- argv ------------------------------------------------------------------


def test_build_argv_shape() -> None:
    argv = build_argv(
        AgentType.COPILOT,
        "do the thing",
        model="auto",
        reasoning_effort="high",
        project_dir="/wt",
        session_id="407397b3-e46e-4da3-9b4d-fa75a095eccc",
        disallowed_tools=("write_file", "edit_file"),
    )
    assert argv == [
        "copilot",
        "--output-format",
        "json",
        "--no-auto-update",
        "--no-ask-user",
        "--model",
        "auto",
        "--reasoning-effort",
        "high",
        "--session-id",
        "407397b3-e46e-4da3-9b4d-fa75a095eccc",
        "--deny-tool=write",
        "-C",
        "/wt",
        "--yolo",
        "-p",
        "do the thing",
    ]


def test_build_argv_minimal_and_user_flags_replace_yolo() -> None:
    argv = build_argv(AgentType.COPILOT, "hi", binary="/opt/copilot", extra_flags=("--allow-all",))
    assert argv == [
        "/opt/copilot",
        "--output-format",
        "json",
        "--no-auto-update",
        "--no-ask-user",
        "--allow-all",
        "-p",
        "hi",
    ]


def test_build_resume_argv_keeps_yolo_and_denials_never_pins() -> None:
    argv = build_resume_argv(
        AgentType.COPILOT,
        "emit the block",
        "sess-1",
        project_dir="/wt",
        disallowed_tools=("write_file",),
    )
    assert argv[:2] == ["copilot", "--resume=sess-1"]
    assert "--session-id" not in argv
    assert "--yolo" in argv
    assert "--deny-tool=write" in argv
    assert argv[-2:] == ["-p", "emit the block"]


@pytest.mark.parametrize(
    ("tools", "expected"),
    [
        (("write_file", "edit_file"), ["--deny-tool=write"]),
        (("bash", "shell", "write_file"), ["--deny-tool=shell", "--deny-tool=write"]),
        (("read_file", "write_file(*.md)", "nope"), []),
    ],
)
def test_tool_denial_maps_to_copilot_permission_kinds(
    tools: tuple[str, ...], expected: list[str]
) -> None:
    argv = build_argv(AgentType.COPILOT, "x", disallowed_tools=tools)
    assert [a for a in argv if a.startswith("--deny-tool")] == expected


# --- parser ----------------------------------------------------------------


def test_parse_real_capture() -> None:
    text, usage, session_id = parse_copilot_jsonl(_read("copilot_json_1_0_94.jsonl"))
    assert text == "PINEAPPLE"
    assert session_id == "407397b3-e46e-4da3-9b4d-fa75a095eccc"
    assert usage.tokens_in == 15646
    assert usage.tokens_out == 7
    assert usage.cached_tokens_in == 1152
    assert usage.max_turn_input_tokens == 15646
    # 1 premium request at the $0.04 overage rate.
    assert usage.reported_cost == pytest.approx(0.04)


def test_parse_tool_denied_capture_uses_final_answer() -> None:
    """First assistant.message is an empty tool-call turn; the final_answer wins."""
    text, usage, session_id = parse_copilot_jsonl(_read("copilot_json_tool_denied_1_0_94.jsonl"))
    assert text == "DENIED"
    assert session_id == "fb2b6f47-180c-4af8-9c70-13e436b6cadc"
    assert usage.reported_cost == pytest.approx(0.04)


def test_parse_session_error_and_nonzero_exit_surface_as_text() -> None:
    raw = (
        '{"type":"session.error","data":{"errorType":"session","message":"Turn error: boom"}}\n'
        '{"type":"result","sessionId":"s1","exitCode":1,"usage":{"premiumRequests":0}}\n'
    )
    text, usage, session_id = parse_copilot_jsonl(raw)
    assert text == "Turn error: boom"
    assert session_id == "s1"
    assert usage.reported_cost == 0.0

    bare = '{"type":"result","sessionId":"s2","exitCode":1,"usage":{}}\n'
    assert parse_copilot_jsonl(bare)[0] == "copilot exited with code 1"


def test_parse_fractional_premium_requests_and_garbage() -> None:
    raw = '{"type":"result","sessionId":"s","exitCode":0,"usage":{"premiumRequests":0.33}}\n'
    assert parse_copilot_jsonl(raw)[1].reported_cost == pytest.approx(0.33 * 0.04)
    assert parse_copilot_jsonl("not json")[0] == "not json"


# --- error classification --------------------------------------------------


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (
            'Error: Model "gpt-5-mini" from --model flag is not available.\n',
            ErrorClass.INVALID_MODEL,
        ),
        ("Error: No authentication information found.\n", ErrorClass.AUTH),
        (
            "Error: Authentication token found but could not be validated.\n\n"
            "  Failed to fetch GitHub CLI user login (401): GitHub returned: Bad credentials\n",
            ErrorClass.AUTH,
        ),
    ],
)
def test_real_startup_failures_classify(stderr: str, expected: ErrorClass) -> None:
    assert _classify_error(1, stderr, "") == expected


def test_session_error_event_feeds_classification() -> None:
    stdout = '{"type":"session.error","data":{"message":"429 Too Many Requests"}}\n'
    assert _classify_error(1, "", stdout) == ErrorClass.RATE_LIMIT


# --- registration ----------------------------------------------------------


def test_copilot_registered_everywhere() -> None:
    assert AgentType.COPILOT in _PARSERS
    assert _DEFAULT_YOLO_FLAGS[AgentType.COPILOT] == ("--yolo",)
    assert AgentType.COPILOT in _RESUMABLE_AGENT_TYPES
    assert AgentType.COPILOT in _PINNABLE_SESSION_AGENT_TYPES
    assert AgentType.COPILOT in _TOOL_DENIAL_CAPABLE_AGENT_TYPES
    assert _FIRST_BYTE_DEADLINE_BY_TYPE[AgentType.COPILOT] == 60.0
    assert isinstance(DEFAULT_CLI_DRIVERS.driver_for(AgentType.COPILOT), CopilotCliDriver)
    assert _is_terminal_event(b'{"type":"result","exitCode":0}', AgentType.COPILOT)
    assert not _is_terminal_event(b'{"type":"assistant.message"}', AgentType.COPILOT)


@pytest.mark.parametrize("agent_type", sorted(CLI_AGENT_TYPES, key=lambda t: t.value))
def test_every_cli_agent_type_has_parser_driver_and_builders(agent_type: AgentType) -> None:
    assert agent_type in _PARSERS
    DEFAULT_CLI_DRIVERS.driver_for(agent_type)
    assert build_argv(agent_type, "x")
    assert build_resume_argv(agent_type, "x", "sid")
