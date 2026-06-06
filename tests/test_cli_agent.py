"""Tests for the CLI agent adapter (dispatch_cli) using the mock agent harness."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentshore.agents.cli_agent import (
    _classify_error,
    _extract_session_id_from_jsonl,
    _extract_text_from_codex_jsonl,
    _extract_text_from_gemini_jsonl,
    _extract_text_from_stream_json,
    _is_terminal_event,
    build_argv,
    dispatch_cli,
)
from agentshore.agents.handle import AgentHandle
from agentshore.config import AgentConfig
from agentshore.errors import (
    AgentOutputInvalid,
    AgentProcessError,
    ErrorClass,
    PlayTimeoutError,
)
from agentshore.result_parser import parse_skill_result
from agentshore.state import AgentStatus, AgentType


@pytest.fixture(autouse=True)
def _identity_executable_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep dispatch argv deterministic across hosts. On Windows,
    _resolve_executable() rewrites a bare 'codex' to the real codex.CMD path
    via shutil.which; pin which() to identity so argv assertions (e.g.
    argv[0] == 'codex') hold regardless of what npm shims are installed. The
    dedicated _resolve_executable tests patch shutil.which themselves.
    """
    import agentshore.agents.cli_agent as ca

    monkeypatch.setattr(ca.shutil, "which", lambda name: name)


def _make_cfg(*, timeout: int | None = None, max_output_size: int = 10_000_000) -> AgentConfig:
    return AgentConfig(
        enabled=True,
        binary=str(Path(__file__).parent / "fixtures" / "mock_agent.py"),
        timeout=timeout,
        max_output_size=max_output_size,
    )


def _make_handle(
    agent_id: str = "a1",
    agent_type: AgentType = AgentType.CLAUDE_CODE,
) -> AgentHandle:
    return AgentHandle(
        agent_id=agent_id,
        agent_type=agent_type,
        status=AgentStatus.IDLE,
        working_dir=Path(tempfile.gettempdir()),
    )


class _AsyncBytes:
    def __init__(self, lines: list[bytes], *, read_bytes: bytes = b"") -> None:
        self._lines = lines
        self._read_bytes = read_bytes

    def __aiter__(self) -> _AsyncBytes:
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)

    async def read(self) -> bytes:
        return self._read_bytes


class _FakeProcess:
    def __init__(
        self,
        lines: list[bytes],
        *,
        returncode: int = 0,
        stderr: bytes = b"",
    ) -> None:
        self.stdout = _AsyncBytes(lines)
        self.stderr = _AsyncBytes([], read_bytes=stderr)
        self.returncode = returncode
        self.pid = 4242

    async def wait(self) -> int:
        return self.returncode


def _codex_json_lines() -> list[bytes]:
    result = {
        "schema_version": 1,
        "success": True,
        "artifacts": [],
        "issues_created": [],
        "requested_mutations": [],
        "metrics": {},
        "error": None,
    }
    content = f"```json\n{json.dumps(result)}\n```"
    return [
        b'{"type":"thread.started","thread_id":"thread_mock"}\n',
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": content},
            }
        ).encode()
        + b"\n",
        b'{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}\n',
    ]


def _codex_cached_json_lines() -> list[bytes]:
    return [
        b'{"type":"thread.started","thread_id":"thread_mock"}\n',
        b'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n',
        (
            b'{"type":"turn.completed","usage":{"input_tokens":1000,'
            b'"cached_input_tokens":800,"output_tokens":120,'
            b'"reasoning_output_tokens":80}}\n'
        ),
    ]


def _codex_cumulative_json_lines() -> list[bytes]:
    return [
        b'{"type":"thread.started","thread_id":"thread_mock"}\n',
        b'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n',
        (
            b'{"type":"token_count","info":{"total_token_usage":{"input_tokens":1000,'
            b'"cached_input_tokens":600,"output_tokens":20},'
            b'"last_token_usage":{"input_tokens":300,"cached_input_tokens":200,'
            b'"output_tokens":20}}}\n'
        ),
        (
            b'{"type":"token_count","info":{"total_token_usage":{"input_tokens":1800,'
            b'"cached_input_tokens":1100,"output_tokens":40},'
            b'"last_token_usage":{"input_tokens":500,"cached_input_tokens":400,'
            b'"output_tokens":20}}}\n'
        ),
    ]


def _claude_json_lines() -> list[bytes]:
    result = {
        "schema_version": 1,
        "success": True,
        "artifacts": [],
        "issues_created": [],
        "requested_mutations": [],
        "metrics": {},
        "error": None,
    }
    return [
        json.dumps(
            {
                "type": "result",
                "session_id": "claude-new",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "result": f"```json\n{json.dumps(result)}\n```",
            }
        ).encode()
        + b"\n"
    ]


def _claude_cached_json_lines() -> list[bytes]:
    return [
        json.dumps(
            {
                "type": "result",
                "session_id": "claude-new",
                "usage": {
                    "input_tokens": 100,
                    "cache_creation_input_tokens": 200,
                    "cache_read_input_tokens": 700,
                    "output_tokens": 50,
                },
                "result": "ok",
            }
        ).encode()
        + b"\n"
    ]


def _gemini_json_lines() -> list[bytes]:
    result = {
        "schema_version": 1,
        "success": True,
        "artifacts": [],
        "issues_created": [],
        "requested_mutations": [],
        "metrics": {},
        "error": None,
    }
    content = f"```json\n{json.dumps(result)}\n```"
    return [
        b'{"type":"init","session_id":"gemini-session","model":"gemini-3-flash-preview"}\n',
        b'{"type":"message","role":"assistant","message":{"content":"ignored chunk"}}\n',
        json.dumps(
            {
                "type": "result",
                "response": content,
                "stats": {
                    "usageMetadata": {
                        "promptTokenCount": 42,
                        "candidatesTokenCount": 13,
                        "cachedContentTokenCount": 7,
                    }
                },
            }
        ).encode()
        + b"\n",
    ]


# ---------------------------------------------------------------------------
# build_argv
# ---------------------------------------------------------------------------


def test_build_argv_claude_code_shape() -> None:
    argv = build_argv(AgentType.CLAUDE_CODE, "do the thing", binary="claude")
    assert argv[0] == "claude"
    assert "-p" in argv
    assert "--output-format" in argv
    assert "stream-json" in argv
    assert argv[-1] == "do the thing"


def test_build_argv_codex_shape() -> None:
    argv = build_argv(AgentType.CODEX, "do the thing", binary="codex", project_dir="/work")
    assert argv[0] == "codex"
    assert "exec" in argv
    # Default YOLO flag for codex; --full-auto is skipped when bypass is set.
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "--full-auto" not in argv
    assert "-C" in argv
    assert "/work" in argv
    assert argv[-1] == "do the thing"


def test_build_argv_gemini_shape() -> None:
    argv = build_argv(
        AgentType.GEMINI,
        "do the thing",
        binary="gemini",
        model="gemini-3-flash-preview",
    )

    assert argv[0] == "gemini"
    assert "--output-format" in argv
    assert "stream-json" in argv
    assert "--approval-mode=yolo" in argv
    assert "--skip-trust" in argv
    assert "--model" in argv
    assert "gemini-3-flash-preview" in argv
    assert "-p" in argv
    assert argv[-1] == "do the thing"


def test_build_argv_codex_inherits_env_to_shell_subprocesses() -> None:
    """desktop-pxg: codex's shell tool strips env vars by default, so the
    GH_TOKEN we inject doesn't reach gh/git subprocesses. We pass
    shell_environment_policy.inherit=all so the identity overlay (GH_TOKEN,
    GH_CONFIG_DIR, GIT_AUTHOR_*) survives to every shell command codex spawns.
    """
    argv = build_argv(AgentType.CODEX, "do the thing", binary="codex")
    assert "-c" in argv
    inherit_index = argv.index("-c")
    # The -c before the policy value must be paired correctly.
    assert "shell_environment_policy.inherit=all" in argv
    # And the paired -c must be immediately before its value.
    while inherit_index < len(argv) - 1:
        if argv[inherit_index] == "-c" and argv[inherit_index + 1].startswith(
            "shell_environment_policy"
        ):
            break
        inherit_index = argv.index("-c", inherit_index + 1)
    assert argv[inherit_index + 1] == "shell_environment_policy.inherit=all"


def test_build_argv_codex_explicit_flags_disable_yolo_default() -> None:
    """If the user specifies extra_flags, no YOLO default is injected."""
    argv = build_argv(
        AgentType.CODEX,
        "do the thing",
        binary="codex",
        project_dir="/work",
        extra_flags=("--some-other-flag",),
    )
    assert "--some-other-flag" in argv
    assert "--ignore-user-config" not in argv
    assert "--ignore-rules" not in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    # Without the bypass, --full-auto IS appended.
    assert "--full-auto" in argv


def test_build_argv_claude_yolo_default() -> None:
    argv = build_argv(AgentType.CLAUDE_CODE, "do the thing", binary="claude")
    assert "--dangerously-skip-permissions" in argv


def test_build_argv_claude_explicit_flags_disable_yolo_default() -> None:
    argv = build_argv(
        AgentType.CLAUDE_CODE,
        "do the thing",
        binary="claude",
        extra_flags=("--debug",),
    )
    assert "--debug" in argv
    assert "--dangerously-skip-permissions" not in argv


def test_build_argv_codex_reasoning_effort() -> None:
    argv = build_argv(
        AgentType.CODEX,
        "do the thing",
        binary="codex",
        model="gpt-5.5",
        reasoning_effort="xhigh",
    )

    assert "-m" in argv
    assert "gpt-5.5" in argv
    assert "-c" in argv
    assert 'model_reasoning_effort="xhigh"' in argv


def test_build_argv_codex_no_resume() -> None:
    """Regression — `session_id` / `is_resume` were removed; every dispatch
    builds a fresh-session argv. See `feedback_persistent_sessions` memory."""
    argv = build_argv(
        AgentType.CODEX,
        "continue",
        binary="codex",
        project_dir="/work",
    )

    assert argv[:3] == ["codex", "exec", "--json"]
    # YOLO bypass is the default, so --full-auto is omitted (yolo replaces it).
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "--full-auto" not in argv
    assert "resume" not in argv
    assert "-C" in argv
    assert argv[-1] == "continue"


async def test_dispatch_cli_does_not_resume_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[list[str]] = []

    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        captured.append(list(argv))
        return _FakeProcess(_codex_json_lines())

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    cfg = AgentConfig(enabled=True, binary="codex", timeout=10)
    handle = _make_handle(agent_type=AgentType.CODEX)
    handle.dispatches = 1

    result = await dispatch_cli(handle, "prompt", cfg=cfg)

    assert result.exit_code == 0
    assert captured[0][:3] == ["codex", "exec", "--json"]
    assert "resume" not in captured[0]
    assert "-C" in captured[0]


async def test_dispatch_cli_never_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression — every CLI dispatch starts a fresh session. --resume was
    removed because it produced silent state-rot late in long sessions
    (observed in a prior long session)."""
    captured: list[list[str]] = []

    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        captured.append(list(argv))
        return _FakeProcess(_claude_json_lines())

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    cfg = AgentConfig(enabled=True, binary="claude", timeout=10)
    handle = _make_handle(agent_type=AgentType.CLAUDE_CODE)

    # First dispatch should not include --resume even though Claude reports a
    # session_id back via _claude_json_lines() that older code stashed on the
    # handle and replayed on subsequent dispatches.
    await dispatch_cli(handle, "prompt", cfg=cfg)
    await dispatch_cli(handle, "prompt", cfg=cfg)

    for argv in captured:
        assert "--resume" not in argv
        assert "resume" not in argv  # Codex codepath would emit `exec resume`


# ---------------------------------------------------------------------------
# Happy path — plain output (Codex-style)
# ---------------------------------------------------------------------------


async def test_dispatch_cli_success_plain(mock_agent_path: Path) -> None:
    cfg = AgentConfig(
        enabled=True,
        binary=str(mock_agent_path),
        timeout=10,
    )
    handle = _make_handle(agent_type=AgentType.CODEX)
    result = await dispatch_cli(handle, "prompt", cfg=cfg, python_executable=sys.executable)
    assert result.exit_code == 0
    sr = parse_skill_result(result.raw_output)
    assert sr.success is True
    assert len(sr.artifacts) == 1
    assert sr.artifacts[0]["number"] == 42  # type: ignore[index]


async def test_dispatch_cli_success_codex_json(
    mock_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_AGENT_FORMAT", "codex_json")
    cfg = AgentConfig(
        enabled=True,
        binary=str(mock_agent_path),
        timeout=10,
    )
    handle = _make_handle(agent_type=AgentType.CODEX)
    result = await dispatch_cli(handle, "prompt", cfg=cfg, python_executable=sys.executable)
    assert result.exit_code == 0
    assert result.tokens_in == 300
    assert result.tokens_out == 120
    assert result.turn_count == 1
    assert result.max_turn_input_tokens == 300
    sr = parse_skill_result(result.raw_output)
    assert sr.success is True


async def test_dispatch_cli_success_gemini_stream_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(_gemini_json_lines())

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    cfg = AgentConfig(enabled=True, binary="gemini", timeout=10)
    handle = _make_handle(agent_type=AgentType.GEMINI)
    result = await dispatch_cli(handle, "prompt", cfg=cfg)

    assert result.exit_code == 0
    assert result.tokens_in == 42
    assert result.tokens_out == 13
    assert result.cached_tokens_in == 7
    sr = parse_skill_result(result.raw_output)
    assert sr.success is True


async def test_dispatch_cli_codex_json_discounts_cached_input_and_does_not_double_count_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(_codex_cached_json_lines())

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    cfg = AgentConfig(
        enabled=True,
        binary="codex",
        timeout=10,
        cost_per_1k_input=0.003,
        cost_per_1k_output=0.012,
    )
    handle = _make_handle(agent_type=AgentType.CODEX)
    result = await dispatch_cli(handle, "prompt", cfg=cfg)

    assert result.tokens_in == 1000
    assert result.cached_tokens_in == 800
    assert result.tokens_out == 120
    assert result.turn_count == 1
    assert result.max_turn_input_tokens == 1000
    expected = (200 / 1000) * 0.003 + (800 / 1000) * 0.0003 + (120 / 1000) * 0.012
    assert result.dollar_cost == pytest.approx(expected)


async def test_dispatch_cli_codex_json_records_cumulative_and_per_turn_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(_codex_cumulative_json_lines())

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    cfg = AgentConfig(enabled=True, binary="codex", timeout=10)
    handle = _make_handle(agent_type=AgentType.CODEX)
    result = await dispatch_cli(handle, "prompt", cfg=cfg)

    assert result.tokens_in == 1800
    assert result.cached_tokens_in == 1100
    assert result.tokens_out == 40
    assert result.turn_count == 2
    assert result.max_turn_input_tokens == 500


# ---------------------------------------------------------------------------
# Happy path — stream-json output (Claude-style)
# ---------------------------------------------------------------------------


async def test_dispatch_cli_success_stream_json(
    mock_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_AGENT_FORMAT", "stream_json")
    cfg = AgentConfig(
        enabled=True,
        binary=str(mock_agent_path),
        timeout=10,
    )
    handle = _make_handle(agent_type=AgentType.CLAUDE_CODE)
    result = await dispatch_cli(handle, "prompt", cfg=cfg, python_executable=sys.executable)
    assert result.exit_code == 0
    assert result.tokens_in == 500
    assert result.tokens_out == 200
    sr = parse_skill_result(result.raw_output)
    assert sr.success is True


async def test_dispatch_cli_claude_json_accounts_for_cache_read_and_write_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(_claude_cached_json_lines())

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    cfg = AgentConfig(
        enabled=True,
        binary="claude",
        timeout=10,
        cost_per_1k_input=0.003,
        cost_per_1k_cached_input=0.0003,
        cost_per_1k_cache_write_input=0.00375,
        cost_per_1k_output=0.015,
    )
    handle = _make_handle(agent_type=AgentType.CLAUDE_CODE)
    result = await dispatch_cli(handle, "prompt", cfg=cfg)

    assert result.tokens_in == 1000
    assert result.cached_tokens_in == 700
    assert result.cache_write_tokens_in == 200
    assert result.tokens_out == 50
    expected = (
        (100 / 1000) * 0.003 + (700 / 1000) * 0.0003 + (200 / 1000) * 0.00375 + (50 / 1000) * 0.015
    )
    assert result.dollar_cost == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Failure result (agent exits 0, result block has success=false)
# ---------------------------------------------------------------------------


async def test_dispatch_cli_failure_result(
    mock_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_AGENT_MODE", "failure")
    cfg = AgentConfig(enabled=True, binary=str(mock_agent_path), timeout=10)
    handle = _make_handle(agent_type=AgentType.CODEX)
    result = await dispatch_cli(handle, "prompt", cfg=cfg, python_executable=sys.executable)
    assert result.exit_code == 0
    sr = parse_skill_result(result.raw_output)
    assert sr.success is False
    assert sr.error is not None


# ---------------------------------------------------------------------------
# Non-zero exit → AgentProcessError
# ---------------------------------------------------------------------------


async def test_dispatch_cli_nonzero_exit_raises(tmp_path: Path) -> None:
    script = tmp_path / "exit1.py"
    script.write_text("import sys; sys.exit(1)\n", encoding="utf-8")
    cfg = AgentConfig(enabled=True, binary=str(script), timeout=5)
    handle = _make_handle(agent_type=AgentType.CODEX)
    with pytest.raises(AgentProcessError):
        await dispatch_cli(handle, "p", cfg=cfg, python_executable=sys.executable)


async def test_dispatch_cli_gemini_model_not_found_is_concise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = (
        b"YOLO mode is enabled. All tool calls will be automatically approved.\n"
        b"Ripgrep is not available. Falling back to GrepTool.\n"
        b"Error when talking to Gemini API Full report available at: /tmp/gemini.json "
        b"ModelNotFoundError: Requested entity was not found.\n"
    )
    lines = [b'{"type":"message","role":"user","message":{"content":"PROMPT SHOULD NOT LEAK"}}\n']

    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(lines, returncode=1, stderr=stderr)

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    cfg = AgentConfig(enabled=True, binary="gemini", model="gemini-3-pro", timeout=10)
    handle = _make_handle(agent_type=AgentType.GEMINI)

    with pytest.raises(AgentProcessError) as exc_info:
        await dispatch_cli(handle, "prompt", cfg=cfg)

    message = str(exc_info.value)
    assert "[invalid_model]" in message
    assert "gemini model 'gemini-3-pro' is not available" in message
    assert "Full report: /tmp/gemini.json" in message
    assert "YOLO mode" not in message
    assert "PROMPT SHOULD NOT LEAK" not in message


# ---------------------------------------------------------------------------
# Output overflow → AgentOutputInvalid
# ---------------------------------------------------------------------------


async def test_dispatch_cli_output_overflow_raises(
    mock_agent_path: Path,
) -> None:
    cfg = AgentConfig(
        enabled=True,
        binary=str(mock_agent_path),
        timeout=10,
        max_output_size=10,  # tiny cap — mock output will exceed this
    )
    handle = _make_handle(agent_type=AgentType.CODEX)
    with pytest.raises(AgentOutputInvalid, match="max_output_size"):
        await dispatch_cli(handle, "prompt", cfg=cfg, python_executable=sys.executable)


# ---------------------------------------------------------------------------
# Per-line buffer cap (asyncio readline limit) → AgentOutputInvalid
# ---------------------------------------------------------------------------


async def test_dispatch_cli_long_line_below_default_limit_succeeds(
    mock_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 200KB line passes under the 4MB default.

    It would fail under asyncio's 64KB default.
    """
    monkeypatch.setenv("MOCK_AGENT_MODE", "long_line")
    monkeypatch.setenv("MOCK_AGENT_LINE_BYTES", "200000")
    cfg = AgentConfig(enabled=True, binary=str(mock_agent_path), timeout=10)
    handle = _make_handle(agent_type=AgentType.CODEX)
    result = await dispatch_cli(handle, "prompt", cfg=cfg, python_executable=sys.executable)
    assert result.exit_code == 0


async def test_dispatch_cli_line_exceeds_limit_raises(
    mock_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A line larger than line_limit_bytes raises AgentOutputInvalid with a hint."""
    monkeypatch.setenv("MOCK_AGENT_MODE", "long_line")
    monkeypatch.setenv("MOCK_AGENT_LINE_BYTES", "200000")
    cfg = AgentConfig(
        enabled=True,
        binary=str(mock_agent_path),
        timeout=10,
        line_limit_bytes=10_000,  # well below mock output line size
    )
    handle = _make_handle(agent_type=AgentType.CODEX)
    with pytest.raises(AgentOutputInvalid, match="line_limit_bytes"):
        await dispatch_cli(handle, "prompt", cfg=cfg, python_executable=sys.executable)


async def test_dispatch_cli_warns_on_large_line(
    mock_agent_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A line larger than 1MB but under line_limit_bytes emits a drift warning once."""
    monkeypatch.setenv("MOCK_AGENT_MODE", "long_line")
    monkeypatch.setenv("MOCK_AGENT_LINE_BYTES", str(1_200_000))  # >1MB
    cfg = AgentConfig(
        enabled=True,
        binary=str(mock_agent_path),
        timeout=10,
        line_limit_bytes=4_194_304,
    )
    handle = _make_handle(agent_type=AgentType.CODEX)
    with caplog.at_level("WARNING"):
        await dispatch_cli(handle, "prompt", cfg=cfg, python_executable=sys.executable)
    # structlog routing varies by test ordering — accept the warning surfacing
    # via either capsys (printed structlog) or caplog (stdlib propagation).
    captured = capsys.readouterr()
    in_capsys = "cli_agent_large_line" in (captured.out + captured.err)
    in_caplog = any("cli_agent_large_line" in r.getMessage() for r in caplog.records)
    assert in_capsys or in_caplog


# ---------------------------------------------------------------------------
# Timeout → PlayTimeoutError + SIGTERM/SIGKILL
# ---------------------------------------------------------------------------


async def test_dispatch_cli_timeout_raises(
    mock_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_AGENT_MODE", "timeout")
    cfg = AgentConfig(
        enabled=True,
        binary=str(mock_agent_path),
        timeout=1,  # 1 second — mock sleeps forever
    )
    handle = _make_handle(agent_type=AgentType.CODEX)
    with pytest.raises(PlayTimeoutError, match="timed out"):
        await dispatch_cli(handle, "prompt", cfg=cfg, python_executable=sys.executable)


async def test_dispatch_cli_stream_idle_timeout_raises(tmp_path: Path) -> None:
    script = tmp_path / "idle_after_output.py"
    script.write_text(
        "import sys, time\nsys.stdout.write('first\\n')\nsys.stdout.flush()\ntime.sleep(10)\n",
        encoding="utf-8",
    )
    cfg = AgentConfig(
        enabled=True,
        binary=str(script),
        timeout=5,
        stream_idle_timeout=0.05,
    )
    handle = _make_handle(agent_type=AgentType.CODEX)

    with pytest.raises(PlayTimeoutError) as exc_info:
        await dispatch_cli(handle, "prompt", cfg=cfg, python_executable=sys.executable)

    assert exc_info.value.error_class == "timeout_stream_idle"
    assert handle.process is None


async def test_dispatch_cli_wallclock_timeout_raises_while_stream_active(tmp_path: Path) -> None:
    script = tmp_path / "active_stream.py"
    script.write_text(
        "import sys, time\n"
        "for i in range(1000):\n"
        "    sys.stdout.write(f'{i}\\n')\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.02)\n",
        encoding="utf-8",
    )
    cfg = AgentConfig(
        enabled=True,
        binary=str(script),
        timeout=0.12,
        stream_idle_timeout=0.5,
    )
    handle = _make_handle(agent_type=AgentType.CODEX)

    with pytest.raises(PlayTimeoutError) as exc_info:
        await dispatch_cli(handle, "prompt", cfg=cfg, python_executable=sys.executable)

    assert exc_info.value.error_class == "timeout_wallclock"
    assert handle.process is None


async def test_dispatch_cli_stream_activity_resets_idle_watchdog(tmp_path: Path) -> None:
    # Inter-line sleep + stream_idle_timeout are intentionally well-separated
    # (40 ms vs. 500 ms) so the watchdog has plenty of margin even when pytest
    # workers contend for CPU under xdist. Tighter values produced spurious
    # failures: passes solo, intermittently fails parallel.
    script = tmp_path / "active_then_complete.py"
    script.write_text(
        "import json, sys, time\n"
        "lines = [\n"
        "    {'type': 'thread.started', 'thread_id': 'thread_active'},\n"
        "    {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'ok'}},\n"
        "]\n"
        "for line in lines:\n"
        "    sys.stdout.write(json.dumps(line) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.04)\n",
        encoding="utf-8",
    )
    cfg = AgentConfig(
        enabled=True,
        binary=str(script),
        timeout=5,
        stream_idle_timeout=0.5,
    )
    handle = _make_handle(agent_type=AgentType.CODEX)

    result = await dispatch_cli(handle, "prompt", cfg=cfg, python_executable=sys.executable)

    assert result.raw_output == "ok"
    assert handle.process is None


@pytest.mark.parametrize("raised", [SystemExit(7), KeyboardInterrupt()])
async def test_dispatch_cli_does_not_clean_up_process_for_control_flow_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    raised: BaseException,
) -> None:
    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(_claude_json_lines())

    async def fake_read_output(
        *args: Any, **kwargs: Any
    ) -> tuple[str, int, int, int, int, int, int, str | None]:
        raise raised

    kill_calls: list[str] = []

    async def fake_kill_process(proc: _FakeProcess, agent_id: str) -> None:
        kill_calls.append(agent_id)

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr("agentshore.agents.cli_agent._read_output", fake_read_output)
    monkeypatch.setattr("agentshore.agents.cli_agent._kill_process", fake_kill_process)
    cfg = AgentConfig(enabled=True, binary="claude", timeout=10)
    handle = _make_handle(agent_type=AgentType.CLAUDE_CODE)

    with pytest.raises(type(raised)):
        await dispatch_cli(handle, "prompt", cfg=cfg)

    assert kill_calls == []
    assert handle.process is None


async def test_dispatch_cli_cleans_up_process_for_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(_claude_json_lines())

    async def fake_read_output(
        *args: Any, **kwargs: Any
    ) -> tuple[str, int, int, int, int, int, int, str | None]:
        raise asyncio.CancelledError

    kill_calls: list[str] = []

    async def fake_kill_process(proc: _FakeProcess, agent_id: str) -> None:
        kill_calls.append(agent_id)

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr("agentshore.agents.cli_agent._read_output", fake_read_output)
    monkeypatch.setattr("agentshore.agents.cli_agent._kill_process", fake_kill_process)
    cfg = AgentConfig(enabled=True, binary="claude", timeout=10)
    handle = _make_handle(agent_type=AgentType.CLAUDE_CODE)

    with pytest.raises(asyncio.CancelledError):
        await dispatch_cli(handle, "prompt", cfg=cfg)

    assert kill_calls == [handle.agent_id]
    assert handle.process is None


# ---------------------------------------------------------------------------
# _kill_process — Windows teardown path (no os.killpg / os.getpgid)
# ---------------------------------------------------------------------------


class _FakeKillProcess:
    """Minimal proc stand-in for _kill_process: a pid and an awaitable wait()."""

    def __init__(self, pid: int | None = 9999, *, returncode: int = 0) -> None:
        self.pid = pid
        self.returncode = returncode
        self._transport = None

    async def wait(self) -> int:
        return self.returncode


class _FakeTaskkill:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode


async def test_kill_process_uses_taskkill_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under Windows-simulation, _kill_process drives taskkill and never touches
    os.killpg (which is absent on Windows -> AttributeError)."""
    import os as _os

    from agentshore.agents import cli_agent as ca

    # Simulate Windows: hasattr(os, "killpg") is False, getpgid absent too.
    monkeypatch.delattr(_os, "killpg", raising=False)
    monkeypatch.delattr(_os, "getpgid", raising=False)

    captured_argv: list[list[str]] = []

    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeTaskkill:
        captured_argv.append(list(argv))
        return _FakeTaskkill(returncode=0)

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    proc = _FakeKillProcess(pid=4321)
    # Must not raise AttributeError despite os.killpg being absent.
    await ca._kill_process(proc, "agent-win")  # type: ignore[arg-type]

    assert captured_argv, "taskkill was never invoked"
    assert captured_argv[0][:3] == ["taskkill", "/PID", "4321"]
    assert "/T" in captured_argv[0]
    # Process exited within grace -> no force kill needed.
    assert all("/F" not in argv for argv in captured_argv)


async def test_kill_process_windows_no_warn_when_process_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero taskkill exit (e.g. 128 'process not found') for a process
    that has already exited is benign and must NOT be logged as a failure."""
    import os as _os

    from agentshore.agents import cli_agent as ca

    monkeypatch.delattr(_os, "killpg", raising=False)
    monkeypatch.delattr(_os, "getpgid", raising=False)

    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeTaskkill:
        return _FakeTaskkill(returncode=128)

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    mock_logger = MagicMock()
    monkeypatch.setattr(ca, "_logger", mock_logger)

    # _FakeKillProcess.returncode is 0 -> the process exited, so even though
    # taskkill returned non-zero, teardown succeeded and nothing is logged.
    proc = _FakeKillProcess(pid=4321)
    await ca._kill_process(proc, "agent-win")  # type: ignore[arg-type]

    warnings = [
        c for c in mock_logger.warning.call_args_list if c.args and c.args[0] == "taskkill_failed"
    ]
    assert warnings == []


async def test_kill_process_windows_bounds_wait_when_force_kill_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``taskkill /F`` cannot stop the process, teardown still completes via a
    bounded wait instead of hanging the session forever (codex review P2)."""
    import os as _os

    from agentshore.agents import cli_agent as ca

    monkeypatch.delattr(_os, "killpg", raising=False)
    monkeypatch.delattr(_os, "getpgid", raising=False)
    monkeypatch.setattr(ca, "_SIGKILL_GRACE", 0.01)

    captured_argv: list[list[str]] = []

    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeTaskkill:
        captured_argv.append(list(argv))
        return _FakeTaskkill(returncode=128)  # every taskkill fails

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    mock_logger = MagicMock()
    monkeypatch.setattr(ca, "_logger", mock_logger)

    class _HangingProc(_FakeKillProcess):
        async def wait(self) -> int:
            await asyncio.sleep(3600)  # never exits on its own
            return 0

    # returncode stays None — the process never dies, so taskkill genuinely
    # failed and the warning must fire (unlike the already-gone benign case).
    proc = _HangingProc(pid=4321, returncode=None)  # type: ignore[arg-type]
    # Guard the test itself: a regression would hang here instead of returning.
    await asyncio.wait_for(ca._kill_process(proc, "agent-win"), timeout=5)  # type: ignore[arg-type]

    # The forced kill was attempted, and the failure was surfaced (not raised).
    assert any("/F" in argv for argv in captured_argv), "forced taskkill never attempted"
    warnings = [
        c for c in mock_logger.warning.call_args_list if c.args and c.args[0] == "taskkill_failed"
    ]
    assert len(warnings) == 1
    assert warnings[0].kwargs["returncode"] == 128


def test_resolve_executable_resolves_npm_shim_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex/claude/gemini are .cmd npm shims; CreateProcess only finds bare
    names ending in .exe, so resolve to the full .cmd path via shutil.which."""
    from agentshore.agents import cli_agent as ca

    monkeypatch.setattr(ca.sys, "platform", "win32")
    monkeypatch.setattr(ca.shutil, "which", lambda _name: r"C:\npm\codex.CMD")

    out = ca._resolve_executable(["codex", "exec", "--json"])
    assert out == [r"C:\npm\codex.CMD", "exec", "--json"]


def test_resolve_executable_noop_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentshore.agents import cli_agent as ca

    monkeypatch.setattr(ca.sys, "platform", "linux")
    assert ca._resolve_executable(["codex", "exec"]) == ["codex", "exec"]


def test_resolve_executable_noop_when_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from agentshore.agents import cli_agent as ca

    monkeypatch.setattr(ca.sys, "platform", "win32")
    called: list[str] = []
    monkeypatch.setattr(ca.shutil, "which", lambda n: called.append(n) or None)
    abs_path = os.path.abspath("python")  # absolute on the test runner

    assert ca._resolve_executable([abs_path, "script"]) == [abs_path, "script"]
    assert called == []  # absolute paths are not re-resolved


def test_resolve_executable_noop_when_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentshore.agents import cli_agent as ca

    monkeypatch.setattr(ca.sys, "platform", "win32")
    monkeypatch.setattr(ca.shutil, "which", lambda _name: None)
    assert ca._resolve_executable(["missing", "arg"]) == ["missing", "arg"]


# ---------------------------------------------------------------------------
# multi_block — parser uses last result block
# ---------------------------------------------------------------------------


async def test_dispatch_cli_multi_block_uses_last(
    mock_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_AGENT_MODE", "multi_block")
    cfg = AgentConfig(enabled=True, binary=str(mock_agent_path), timeout=10)
    handle = _make_handle(agent_type=AgentType.CODEX)
    result = await dispatch_cli(handle, "prompt", cfg=cfg, python_executable=sys.executable)
    sr = parse_skill_result(result.raw_output)
    assert sr.success is True
    # The real result has PR #42, the example block had #0
    assert sr.artifacts[0]["number"] == 42  # type: ignore[index]


# ---------------------------------------------------------------------------
# Identity env injection
# ---------------------------------------------------------------------------


async def test_dispatch_cli_no_identity_env_inherits_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        captured["env"] = kwargs.get("env")
        return _FakeProcess(_codex_json_lines())

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    cfg = AgentConfig(enabled=True, binary="codex", timeout=10)
    handle = _make_handle(agent_type=AgentType.CODEX)

    await dispatch_cli(handle, "prompt", cfg=cfg)

    # When no identity_env is supplied, env=None so the child inherits parent.
    assert captured["env"] is None


async def test_dispatch_cli_identity_env_overlays_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        captured["env"] = kwargs.get("env")
        return _FakeProcess(_codex_json_lines())

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setenv("PRE_EXISTING", "kept")
    cfg = AgentConfig(enabled=True, binary="codex", timeout=10, identity="unseriousAI")
    handle = _make_handle(agent_type=AgentType.CODEX)

    overlay = {
        "GIT_AUTHOR_NAME": "unseriousAI",
        "GIT_AUTHOR_EMAIL": "bot@example.com",
        "GH_TOKEN": "ghp_test",
    }
    await dispatch_cli(handle, "prompt", cfg=cfg, identity_env=overlay)

    env = captured["env"]
    assert env is not None
    # Parent env preserved.
    assert env["PRE_EXISTING"] == "kept"
    # Overlay applied.
    assert env["GIT_AUTHOR_NAME"] == "unseriousAI"
    assert env["GIT_AUTHOR_EMAIL"] == "bot@example.com"
    assert env["GH_TOKEN"] == "ghp_test"


async def _collect_spawned(bucket: list[int], pid: int) -> None:
    bucket.append(pid)


async def _collect_exited(bucket: list[tuple[int, int | None]], pid: int, code: int | None) -> None:
    bucket.append((pid, code))


async def test_dispatch_cli_emits_subprocess_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = _make_handle(agent_type=AgentType.CODEX)
    cfg = _make_cfg()
    spawned: list[int] = []
    exited: list[tuple[int, int | None]] = []

    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        del argv, kwargs
        return _FakeProcess(_codex_json_lines(), returncode=0)

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    await dispatch_cli(
        handle,
        "prompt",
        cfg=cfg,
        on_subprocess_spawned=lambda pid: _collect_spawned(spawned, pid),
        on_subprocess_exited=lambda pid, code: _collect_exited(exited, pid, code),
    )

    assert spawned == [4242]
    assert exited == [(4242, 0)]


# ---------------------------------------------------------------------------
# _classify_error
# ---------------------------------------------------------------------------


def test_classify_error_rate_limit() -> None:
    assert _classify_error(1, "429 Too Many Requests", "") == "rate_limit"


def test_classify_error_rate_limit_from_stdout() -> None:
    assert _classify_error(1, "", "some output\nrate limit exceeded\n") == "rate_limit"


def test_classify_error_auth() -> None:
    assert _classify_error(1, "HTTP 401 Unauthorized", "") == "auth"


def test_classify_error_github_repo_access_as_auth() -> None:
    assert (
        _classify_error(
            1,
            "GraphQL: Could not resolve to a Repository with the name 'owner/repo'.",
            "",
        )
        == "auth"
    )


def test_classify_error_timeout() -> None:
    assert _classify_error(1, "context deadline exceeded", "") == "timeout"


def test_classify_error_returns_error_class_members() -> None:
    """The classifier returns typed ErrorClass members, not bare strings.

    ErrorClass is a StrEnum, so ``== "rate_limit"`` keeps working; this guards
    the stronger property that the *type* is the enum so downstream typed
    comparisons (eligibility, gates) are exhaustive and typo-proof.
    """
    rl = _classify_error(1, "429 Too Many Requests", "")
    assert rl is ErrorClass.RATE_LIMIT
    assert isinstance(rl, ErrorClass)
    assert _classify_error(1, "HTTP 403 Forbidden", "") is ErrorClass.AUTH
    assert _classify_error(1, "context deadline exceeded", "") is ErrorClass.TIMEOUT
    assert _classify_error(1, "model not found", "") is ErrorClass.INVALID_MODEL
    assert _classify_error(-9, "", "") is ErrorClass.CRASH_SIGNAL
    assert _classify_error(1, "something nobody matches", "") is ErrorClass.UNKNOWN


def test_classify_error_invalid_model() -> None:
    assert (
        _classify_error(1, "ModelNotFoundError: Requested entity was not found.", "")
        == "invalid_model"
    )


def test_classify_error_codex_chatgpt_unsupported_model() -> None:
    assert (
        _classify_error(
            1,
            "",
            "The 'o4-mini' model is not supported when using Codex with a ChatGPT account.",
        )
        == "invalid_model"
    )


def test_classify_error_stdout_work_product_not_misclassified() -> None:
    """#19: generic tokens in a coding agent's stdout (its work product) must
    NOT be classified as rate_limit/auth/timeout/invalid_model. These are the
    failure modes that corrupted the RL signal and tore down working agents."""
    # A failed file edit whose surrounding diff/output happens to mention these.
    assert (
        _classify_error(
            1,
            "",
            "Error executing tool replace: could not find the string to replace.\n"
            "context near: if resp.status == 429: raise Overloaded('capacity')  # throttle\n",
        )
        == "unknown"
    )
    # Agent editing HTTP/error-handling code; 403/forbidden/401 are work product.
    assert (
        _classify_error(1, "", "added handler for 403 Forbidden and 401 Unauthorized") == "unknown"
    )
    # "timeout" is ubiquitous in code/test names.
    assert (
        _classify_error(1, "", "def test_request_timeout(): ...  # deadline exceeded path")
        == "unknown"
    )
    # Generic invalid-model phrasing inside written code, not a CLI verdict.
    assert _classify_error(1, "", 'raise ModelNotFoundError("model not found")') == "unknown"


def test_classify_error_stderr_still_matches_generic_tokens() -> None:
    """The full pattern set still applies to stderr (a CLI's own diagnostics)."""
    assert _classify_error(1, "Error: 429 overloaded, retry after 5s", "") == "rate_limit"
    assert _classify_error(1, "HTTP 403 Forbidden", "") == "auth"
    assert _classify_error(1, "request timeout", "") == "timeout"
    assert _classify_error(1, "model not found", "") == "invalid_model"


def test_classify_error_high_precision_stdout_phrases_still_match() -> None:
    """Distinctive phrases (real CLI/tool verdicts) are still caught in stdout."""
    # Claude reports quota exhaustion on stdout with nothing on stderr.
    assert _classify_error(1, "", "...\nrate limit exceeded\n") == "rate_limit"
    # gh tool auth failure echoed into the agent's stdout JSONL.
    assert (
        _classify_error(1, "", "GraphQL: Could not resolve to a Repository with the name 'o/r'")
        == "auth"
    )
    # Codex prints this model error to stdout.
    assert (
        _classify_error(
            1, "", "The 'o4-mini' model is not supported when using Codex with a ChatGPT account."
        )
        == "invalid_model"
    )


def test_classify_error_unknown() -> None:
    assert _classify_error(1, "something went wrong", "generic output") == "unknown"


def test_classify_error_empty_both() -> None:
    assert _classify_error(1, "", "") == "unknown"


def test_classify_error_sigkill_is_crash_not_unknown() -> None:
    """SIGKILL (-9, e.g. OS OOM kill) must be a crash, not the rate-limit-eligible
    'unknown' bucket (#7 — the mass -9 burst was misclassified as rate_limit)."""
    assert _classify_error(-9, "", "") == "crash_signal"
    assert _classify_error(-6, "", "build was a long compile") == "crash_signal"


def test_classify_error_oom_signature() -> None:
    assert _classify_error(-9, "", "fatal: Out of memory") == "crash_oom"
    assert _classify_error(1, "Cannot allocate memory", "") == "crash_oom"


def test_classify_error_graceful_signals_stay_unknown() -> None:
    """SIGTERM/SIGINT are AgentShore/OS-initiated graceful stops, not crashes."""
    assert _classify_error(-15, "", "") == "unknown"
    assert _classify_error(-2, "", "") == "unknown"


# ---------------------------------------------------------------------------
# _is_terminal_event (#21 — response-complete fast-kill for all agent types)
# ---------------------------------------------------------------------------


def test_is_terminal_event_detects_each_agent_type() -> None:
    """Claude/Gemini emit type:result; Codex emits turn.completed. All three
    must be recognized so the 60s post-response grace applies (not the 30-min
    stream_idle_timeout)."""
    assert _is_terminal_event(b'{"type":"result","result":"ok"}', AgentType.CLAUDE_CODE)
    assert _is_terminal_event(b'{"type":"result","response":"ok"}', AgentType.GEMINI)
    assert _is_terminal_event(
        b'{"type":"turn.completed","usage":{"input_tokens":1}}', AgentType.CODEX
    )


def test_is_terminal_event_ignores_non_terminal_and_cross_type() -> None:
    # Mid-stream events are not terminal.
    assert not _is_terminal_event(b'{"type":"assistant","message":{}}', AgentType.CLAUDE_CODE)
    assert not _is_terminal_event(b'{"type":"item.completed","item":{}}', AgentType.CODEX)
    # Codex's terminal type must not fire for Claude/Gemini and vice versa.
    assert not _is_terminal_event(b'{"type":"turn.completed"}', AgentType.GEMINI)
    assert not _is_terminal_event(b'{"type":"result"}', AgentType.CODEX)
    # Work product mentioning the word "result" is not a result event.
    assert not _is_terminal_event(
        b'{"type":"assistant","text":"the result is 42"}', AgentType.GEMINI
    )
    # Non-JSON lines never raise.
    assert not _is_terminal_event(b"not json at all", AgentType.CLAUDE_CODE)


def test_classify_error_content_wins_over_signal() -> None:
    """An explicit rate-limit message still wins even on a signal death."""
    assert _classify_error(-9, "429 Too Many Requests", "") == "rate_limit"


def test_classify_error_codex_rollout_thread_missing() -> None:
    # Real stderr captured in desktop-yxlj. The Codex CLI's rollout-recording
    # layer references a thread id it can't find on disk and exits with code 1.
    stderr = (
        "Reading additional input from stdin...\n"
        "2026-05-21T00:07:07.213928Z ERROR codex_core::session: "
        "failed to record rollout items: thread "
        "019e47da-9aa8-75f2-ae43-26fa80d8df59 not found"
    )
    assert _classify_error(1, stderr, "") == "codex_rollout"


def test_socket_close_classifies_as_transient_network() -> None:
    # claude_code's "socket connection was closed unexpectedly" used to fall
    # into the generic "unknown" bucket and log a misleading rate-limit recovery
    # (#23). It is now its own transient_network class.
    stderr = "API Error: The socket connection was closed unexpectedly"
    assert _classify_error(1, stderr, "") == "transient_network"


def test_connection_reset_classifies_as_transient_network() -> None:
    assert _classify_error(1, "read ECONNRESET", "") == "transient_network"


def test_transient_network_is_recoverable_and_in_unknown_path() -> None:
    """transient_network keeps the recoverable take_break treatment of the old
    "unknown" classification, via the distinct unknown-error path (#23/#24)."""
    from agentshore.core.mixins.completion import _UNKNOWN_ERROR_RECOVERY_ERROR_CLASSES
    from agentshore.state import RECOVERABLE_ERROR_CLASSES

    assert "transient_network" in _UNKNOWN_ERROR_RECOVERY_ERROR_CLASSES
    assert "transient_network" in RECOVERABLE_ERROR_CLASSES


def test_codex_rollout_is_in_take_break_recovery_set() -> None:
    # If this assertion ever fails, the classifier name changed but the
    # recovery set did not — the agent will skip the take_break override and
    # surface a permanent ERROR instead of rotating to a fresh codex process.
    # codex_rollout now lives in the unknown-error recovery path (split from
    # rate-limit recovery in #23/#24), not the rate-limit set.
    from agentshore.core.mixins.completion import (
        _RATE_LIMIT_RECOVERY_ERROR_CLASSES,
        _UNKNOWN_ERROR_RECOVERY_ERROR_CLASSES,
    )

    assert "codex_rollout" in _UNKNOWN_ERROR_RECOVERY_ERROR_CLASSES
    assert "codex_rollout" not in _RATE_LIMIT_RECOVERY_ERROR_CLASSES


def test_extract_session_id_from_jsonl_handles_whitespace_lines() -> None:
    raw = '\n  {"thread_id":"t-1"}  \n'
    assert _extract_session_id_from_jsonl(raw) == "t-1"


def test_extract_text_from_codex_jsonl_handles_whitespace_lines() -> None:
    raw = (
        '\n  {"type":"thread.started","thread_id":"t-1"}  \n'
        '  {"type":"item.completed","item":{"type":"agent_message","text":"hello"}}  \n'
    )
    text, usage, session_id = _extract_text_from_codex_jsonl(raw)
    assert text == "hello"
    assert session_id == "t-1"
    assert usage.tokens_in == 0


def test_extract_text_from_gemini_jsonl_handles_whitespace_lines() -> None:
    raw = (
        '\n  {"type":"init","session_id":"g-1"}  \n'
        '  {"type":"message","role":"assistant","message":{"content":"hi"}}  \n'
    )
    text, usage, session_id = _extract_text_from_gemini_jsonl(raw)
    assert text == "hi"
    assert session_id == "g-1"
    assert usage.tokens_out == 0


def test_extract_text_from_stream_json_handles_whitespace_lines() -> None:
    raw = '\n  {"type":"result","result":"ok"}  \n'
    assert _extract_text_from_stream_json(raw) == "ok"


@pytest.mark.asyncio
async def test_watch_stream_idle_error_includes_tier_and_prompt_bytes() -> None:
    """desktop-awc: timeout errors must self-diagnose with model tier + prompt size."""
    import time

    from agentshore.agents.cli_agent import _StdoutActivity, _watch_stream_idle
    from agentshore.errors import PlayTimeoutError

    activity = _StdoutActivity(last_stdout_at=time.monotonic() - 10.0)
    activity.received_any = True

    with pytest.raises(PlayTimeoutError) as excinfo:
        await _watch_stream_idle(
            activity,
            timeout=0.01,  # tiny — fires immediately on first poll
            agent_id="agent-abc",
            agent_type="claude_code",
            model_tier="medium",
            prompt_bytes=9587,
        )
    msg = str(excinfo.value)
    assert "claude_code/medium" in msg
    assert "prompt_bytes=9587" in msg
    assert "agent-abc" in msg


@pytest.mark.asyncio
async def test_watch_stream_idle_error_omits_unknown_prompt_bytes() -> None:
    """When prompt_bytes isn't passed, the suffix omits it."""
    import time

    from agentshore.agents.cli_agent import _StdoutActivity, _watch_stream_idle
    from agentshore.errors import PlayTimeoutError

    activity = _StdoutActivity(last_stdout_at=time.monotonic() - 10.0)
    activity.received_any = True

    with pytest.raises(PlayTimeoutError) as excinfo:
        await _watch_stream_idle(
            activity,
            timeout=0.01,
            agent_id="agent-xyz",
            agent_type="codex",
            model_tier=None,  # unknown tier
        )
    msg = str(excinfo.value)
    assert "codex/?" in msg
    assert "prompt_bytes" not in msg


@pytest.mark.asyncio
async def test_watch_stream_idle_kills_silent_subprocess() -> None:
    """Silent subprocesses (no output at all) must still timeout.

    Production session 08a948ed-2026-05-28 had calibrate_alignment run for
    20+ minutes emitting zero events, holding the trunk lock and blocking
    every merge. The prior implementation guarded the timeout with
    ``if not received_any: continue``, which meant fully-silent subprocesses
    were never killed.
    """
    import time

    from agentshore.agents.cli_agent import _StdoutActivity, _watch_stream_idle
    from agentshore.errors import PlayTimeoutError

    # received_any stays False — no stdout ever arrived.
    activity = _StdoutActivity(last_stdout_at=time.monotonic() - 10.0)
    assert activity.received_any is False

    with pytest.raises(PlayTimeoutError) as excinfo:
        await _watch_stream_idle(
            activity,
            timeout=0.01,  # tiny — fires on first poll
            agent_id="agent-silent",
            agent_type="claude_code",
            model_tier="large",
        )
    msg = str(excinfo.value)
    assert "never produced any stdout" in msg
    assert "agent-silent" in msg
