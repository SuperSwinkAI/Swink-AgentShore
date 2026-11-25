"""Schema-enforced result envelope: harness flag where supported, text fallback everywhere."""

from __future__ import annotations

import json

import pytest

from agentshore.agents.cli import argv as cli_argv
from agentshore.agents.cli.argv import build_argv, build_resume_argv, with_result_schema
from agentshore.agents.cli.parsing import _extract_text_from_stream_json
from agentshore.config.models import SkillsConfig
from agentshore.result_parser import (
    RESULT_ENVELOPE_JSON_SCHEMA,
    RESULT_ENVELOPE_SCHEMA_JSON,
    parse_skill_result,
)
from agentshore.state import AgentType


@pytest.fixture(autouse=True)
def _posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")


def test_schema_pins_only_what_the_parser_requires() -> None:
    assert RESULT_ENVELOPE_JSON_SCHEMA["required"] == ["success", "artifacts"]
    assert "additionalProperties" not in RESULT_ENVELOPE_JSON_SCHEMA
    assert json.loads(RESULT_ENVELOPE_SCHEMA_JSON) == RESULT_ENVELOPE_JSON_SCHEMA


def test_claude_fresh_argv_gets_the_flag_before_the_prompt() -> None:
    argv = build_argv(AgentType.CLAUDE_CODE, "do the thing", model="sonnet")
    out = with_result_schema(AgentType.CLAUDE_CODE, argv, RESULT_ENVELOPE_SCHEMA_JSON)

    i = out.index("--json-schema")
    assert out[i + 1] == RESULT_ENVELOPE_SCHEMA_JSON
    assert out[i - 2 : i] == ["--output-format", "stream-json"]
    assert out[-1] == "do the thing"
    assert [a for a in out if a not in ("--json-schema", RESULT_ENVELOPE_SCHEMA_JSON)] == argv


def test_claude_resume_argv_gets_the_flag_too() -> None:
    argv = build_resume_argv(AgentType.CLAUDE_CODE, "emit the block", "sess-1")
    out = with_result_schema(AgentType.CLAUDE_CODE, argv, RESULT_ENVELOPE_SCHEMA_JSON)
    assert "--json-schema" in out
    assert out[-1] == "emit the block"
    assert out[out.index("--resume") + 1] == "sess-1"


@pytest.mark.parametrize(
    "agent_type",
    [t for t in AgentType if t not in cli_argv._RESULT_SCHEMA_CAPABLE_AGENT_TYPES],
)
def test_unverified_harnesses_are_left_on_the_prompt_contract(agent_type: AgentType) -> None:
    argv = ["bin", "--output-format", "stream-json", "prompt"]
    assert with_result_schema(agent_type, argv, RESULT_ENVELOPE_SCHEMA_JSON) == argv


def test_no_schema_requested_is_a_no_op() -> None:
    argv = build_argv(AgentType.CLAUDE_CODE, "p")
    assert with_result_schema(AgentType.CLAUDE_CODE, argv, None) == argv


def test_windows_shims_skip_the_inline_json_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    argv = ["claude", "-p", "--verbose", "--output-format", "stream-json"]
    assert with_result_schema(AgentType.CLAUDE_CODE, argv, RESULT_ENVELOPE_SCHEMA_JSON) == argv


def test_enforcement_is_on_by_default_and_can_be_switched_off() -> None:
    assert SkillsConfig().enforce_result_schema is True
    assert SkillsConfig(enforce_result_schema=False).enforce_result_schema is False


def _result_event(**fields: object) -> str:
    return json.dumps({"type": "result", "subtype": "success", **fields})


def test_result_text_already_json_is_used_as_is() -> None:
    envelope = {"success": True, "artifacts": [{"type": "pr", "url": "u"}], "issue_picked_up": 7}
    raw = _result_event(result=json.dumps(envelope), structured_output=envelope)
    text = _extract_text_from_stream_json(raw)
    parsed = parse_skill_result(text)
    assert parsed.success is True
    assert parsed.artifacts == [{"type": "pr", "url": "u"}]


def test_structured_output_rescues_a_prose_result() -> None:
    envelope = {"success": True, "artifacts": [{"type": "review", "pr": 42}]}
    raw = _result_event(result="All done! The PR is reviewed.", structured_output=envelope)
    parsed = parse_skill_result(_extract_text_from_stream_json(raw))
    assert parsed.success is True
    assert parsed.artifacts == [{"type": "review", "pr": 42}]


def test_structured_output_used_when_result_text_is_absent() -> None:
    raw = _result_event(structured_output={"success": False, "artifacts": [], "error": "blocked"})
    parsed = parse_skill_result(_extract_text_from_stream_json(raw))
    assert parsed.success is False
    assert parsed.error == "blocked"


def test_without_structured_output_the_text_path_is_unchanged() -> None:
    fenced = 'work done\n```json\n{"success": true, "artifacts": []}\n```'
    assert _extract_text_from_stream_json(_result_event(result=fenced)) == fenced
    prose = parse_skill_result(_extract_text_from_stream_json(_result_event(result="done")))
    assert prose.success is False
    assert "no valid result block" in (prose.error or "")
