"""Tests for model_discovery_llm: real subprocess spawn against a fake `claude`.

Mirrors tests/test_model_discovery.py's approach — drive the real
subprocess.Popen path against a tiny fake executable that emits a canned
``--output-format json`` envelope (captured from real dispatches during
development), so parsing/classification is covered without spending any
actual API tokens.

POSIX-only: the fake "binary" is a ``chmod +x`` shebang script, which is not
directly executable via ``CreateProcess`` on Windows.
"""

from __future__ import annotations

import json
import sys
import time
from typing import TYPE_CHECKING

import pytest

from agentshore.agents.model_discovery_llm import (
    DEFAULT_MAX_BUDGET_USD,
    _parse_model_list,
    _strip_markdown_fences,
    discover_claude_code_models_via_agent,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="fake CLI binary is a POSIX chmod+x shebang script",
)


def _make_fake_claude(tmp_path: Path, *, body: str) -> str:
    script = tmp_path / "fake-claude"
    script.write_text(f"#!{sys.executable}\nimport sys, time, json\n{body}\n")
    script.chmod(0o755)
    return str(script)


def _envelope(**overrides: object) -> str:
    base: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps(["claude-fable-5", "sonnet", "opus", "haiku"]),
        "total_cost_usd": 0.4123,
    }
    base.update(overrides)
    return json.dumps(base)


# ---------------------------------------------------------------------------
# _strip_markdown_fences / _parse_model_list unit tests
# ---------------------------------------------------------------------------


def test_strip_markdown_fences_removes_json_fence() -> None:
    text = '```json\n["a", "b"]\n```'
    assert _strip_markdown_fences(text) == '["a", "b"]'


def test_strip_markdown_fences_removes_bare_fence() -> None:
    text = '```\n["a", "b"]\n```'
    assert _strip_markdown_fences(text) == '["a", "b"]'


def test_strip_markdown_fences_noop_without_fence() -> None:
    assert _strip_markdown_fences('["a", "b"]') == '["a", "b"]'


def test_parse_model_list_rejects_empty_array() -> None:
    assert _parse_model_list("[]") is None


def test_parse_model_list_rejects_non_string_items() -> None:
    assert _parse_model_list('["a", 1, "b"]') is None


def test_parse_model_list_rejects_blank_strings() -> None:
    assert _parse_model_list('["a", "  ", "b"]') is None


def test_parse_model_list_rejects_non_list() -> None:
    assert _parse_model_list('{"a": 1}') is None


def test_parse_model_list_rejects_prose() -> None:
    assert _parse_model_list("Sure, here are the models: sonnet, opus") is None


def test_parse_model_list_accepts_fenced_array() -> None:
    assert _parse_model_list('```json\n["sonnet", "opus"]\n```') == ("sonnet", "opus")


# ---------------------------------------------------------------------------
# discover_claude_code_models_via_agent
# ---------------------------------------------------------------------------


def test_discover_ok_parses_models_and_cost(tmp_path: Path) -> None:
    payload = _envelope()
    binary = _make_fake_claude(tmp_path, body=f"print({payload!r}); sys.exit(0)")
    result = discover_claude_code_models_via_agent(binary=binary)
    assert result.status == "ok"
    assert result.models == ("claude-fable-5", "sonnet", "opus", "haiku")
    assert result.cost_usd == pytest.approx(0.4123)
    assert result.agent_key == "claude_code"


def test_discover_ok_strips_markdown_fence_in_result(tmp_path: Path) -> None:
    fenced_result = "```json\n" + json.dumps(["sonnet", "opus"]) + "\n```"
    payload = _envelope(result=fenced_result)
    binary = _make_fake_claude(tmp_path, body=f"print({payload!r}); sys.exit(0)")
    result = discover_claude_code_models_via_agent(binary=binary)
    assert result.status == "ok"
    assert result.models == ("sonnet", "opus")


def test_discover_budget_exceeded_is_distinct_status(tmp_path: Path) -> None:
    payload = _envelope(
        subtype="error_max_budget_usd",
        is_error=True,
        result=None,
        total_cost_usd=0.15,
        errors=["Reached maximum budget ($0.15)"],
    )
    binary = _make_fake_claude(tmp_path, body=f"print({payload!r}); sys.exit(1)")
    result = discover_claude_code_models_via_agent(binary=binary)
    assert result.status == "budget_exceeded"
    assert result.models == ()
    assert result.cost_usd == pytest.approx(0.15)
    assert "0.15" in result.detail


def test_discover_generic_provider_error_is_error_status(tmp_path: Path) -> None:
    payload = _envelope(
        subtype="error_during_execution",
        is_error=True,
        result=None,
        total_cost_usd=0.02,
        errors=["something went wrong"],
    )
    binary = _make_fake_claude(tmp_path, body=f"print({payload!r}); sys.exit(1)")
    result = discover_claude_code_models_via_agent(binary=binary)
    assert result.status == "error"
    assert result.cost_usd == pytest.approx(0.02)


def test_discover_result_not_a_model_array_is_error(tmp_path: Path) -> None:
    payload = _envelope(result="Sure! The models are sonnet, opus, and haiku.")
    binary = _make_fake_claude(tmp_path, body=f"print({payload!r}); sys.exit(0)")
    result = discover_claude_code_models_via_agent(binary=binary)
    assert result.status == "error"
    assert result.models == ()


def test_discover_unparseable_envelope_is_error(tmp_path: Path) -> None:
    binary = _make_fake_claude(tmp_path, body="print('not json at all'); sys.exit(0)")
    result = discover_claude_code_models_via_agent(binary=binary)
    assert result.status == "error"


def test_discover_missing_binary_is_unavailable(tmp_path: Path) -> None:
    result = discover_claude_code_models_via_agent(binary=str(tmp_path / "does-not-exist"))
    assert result.status == "unavailable"
    assert result.models == ()


def test_discover_timeout_returns_promptly_and_tree_kills(tmp_path: Path) -> None:
    binary = _make_fake_claude(tmp_path, body="time.sleep(30)")
    started = time.monotonic()
    result = discover_claude_code_models_via_agent(binary=binary, timeout=0.5)
    elapsed = time.monotonic() - started
    assert result.status == "timeout"
    assert elapsed < 10.0, f"probe blocked for {elapsed:.1f}s instead of tree-killing"


def test_discover_passes_safety_flags_to_the_cli(tmp_path: Path) -> None:
    """Guards the actual flags sent: --safe-mode, both WebSearch+WebFetch,
    and --max-budget-usd — a regression here would either leak project
    config/hooks into the probe or silently remove the cost ceiling."""
    argv_capture = tmp_path / "argv.json"
    payload = _envelope()
    body = f"json.dump(sys.argv, open({str(argv_capture)!r}, 'w'))\nprint({payload!r}); sys.exit(0)"
    binary = _make_fake_claude(tmp_path, body=body)
    discover_claude_code_models_via_agent(binary=binary, max_budget_usd=0.5)

    argv = json.loads(argv_capture.read_text())
    assert "--safe-mode" in argv
    assert "--bare" not in argv
    tools_idx = argv.index("--allowedTools")
    assert argv[tools_idx + 1] == "WebSearch,WebFetch"
    budget_idx = argv.index("--max-budget-usd")
    assert argv[budget_idx + 1] == "0.5"


def test_default_max_budget_is_above_observed_successful_cost() -> None:
    # Real successful dev runs cost up to ~$0.40; the default ceiling must
    # leave headroom rather than truncating a legitimate run.
    assert DEFAULT_MAX_BUDGET_USD >= 0.5
