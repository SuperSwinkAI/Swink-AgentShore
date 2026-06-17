"""Tests for the CLI agent adapter (dispatch_cli) using the mock agent harness."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentshore.agents.cli_agent import (
    _PARSERS,
    _classify_error,
    _extract_session_id_from_jsonl,
    _extract_text_from_codex_jsonl,
    _extract_text_from_gemini_jsonl,
    _extract_text_from_grok_jsonl,
    _extract_text_from_stream_json,
    _is_terminal_event,
    _read_output,
    _StderrSniffer,
    _watch_stderr_auth,
    build_argv,
    dispatch_cli,
)
from agentshore.agents.handle import AgentHandle
from agentshore.agents.pricing import AgentPricing, PricingQuote
from agentshore.config import AgentConfig
from agentshore.errors import (
    AgentOutputInvalid,
    AgentProcessError,
    ErrorClass,
    PlayTimeoutError,
)
from agentshore.result_parser import parse_skill_result
from agentshore.state import AgentStatus, AgentType


def _price_quote(
    *,
    cost_per_1k_input: float,
    cost_per_1k_output: float,
    cost_per_1k_cached_input: float | None = None,
    cost_per_1k_cache_write_input: float | None = None,
) -> PricingQuote:
    """Resolved pricing for a single dispatch (replaces per-AgentConfig rates)."""
    return PricingQuote(
        pricing=AgentPricing(
            max_context=200000,
            cost_per_1k_input=cost_per_1k_input,
            cost_per_1k_cached_input=cost_per_1k_cached_input,
            cost_per_1k_cache_write_input=cost_per_1k_cache_write_input,
            cost_per_1k_output=cost_per_1k_output,
        ),
        cache_read_multiplier=0.1,
        cache_write_multiplier=1.25,
    )


@pytest.fixture(autouse=True)
def _identity_executable_resolution(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep dispatch argv deterministic across hosts. On Windows,
    _resolve_executable() rewrites a bare 'codex' to the real codex.CMD path
    via shutil.which; pin which() to identity so argv assertions (e.g.
    argv[0] == 'codex') hold regardless of what npm shims are installed. The
    dedicated _resolve_executable tests opt out via @pytest.mark.real_resolve_executable
    so they exercise the genuine function.
    """
    if request.node.get_closest_marker("real_resolve_executable") is not None:
        return

    import agentshore.agents.cli_agent as ca

    monkeypatch.setattr(ca, "_resolve_executable", lambda argv: argv)


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


class _FakeStdin:
    """Minimal StreamWriter stand-in so the Windows prompt-on-stdin path works."""

    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


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
        self.stdin = _FakeStdin()
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


def _grok_json_lines() -> list[bytes]:
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
        b'{"type":"session.started","session_id":"grok-session"}\n',
        b'{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"ignored"}]}}\n',
        json.dumps(
            {
                "type": "result",
                "message": {"role": "assistant", "content": content},
                "usage": {
                    "input_tokens": 120,
                    "cached_input_tokens": 40,
                    "output_tokens": 30,
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


def test_build_argv_claude_code_reasoning_effort() -> None:
    """``--effort`` flag is emitted for claude when reasoning_effort is set."""
    argv = build_argv(
        AgentType.CLAUDE_CODE,
        "do the thing",
        binary="claude",
        model="sonnet",
        reasoning_effort="high",
    )

    assert "--effort" in argv
    assert argv[argv.index("--effort") + 1] == "high"
    # --effort must appear before extra_flags / prompt.
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


def test_build_argv_prompt_on_stdin_omits_prompt_from_argv() -> None:
    """Windows: the prompt rides stdin to dodge the cmd.exe command-line limit,
    so the (possibly huge) prompt text must never appear as an argv element."""
    huge = "x" * 20000
    claude = build_argv(AgentType.CLAUDE_CODE, huge, binary="claude", prompt_on_stdin=True)
    codex = build_argv(
        AgentType.CODEX, huge, binary="codex", project_dir="/work", prompt_on_stdin=True
    )
    gemini = build_argv(AgentType.GEMINI, huge, binary="gemini", prompt_on_stdin=True)
    # Grok has no stdin prompt mode, so the dispatch layer hands it a prompt-file
    # path instead (issue #160); that is what keeps the prompt out of argv.
    grok = build_argv(
        AgentType.GROK, huge, binary="grok", prompt_on_stdin=True, prompt_file="/tmp/p.txt"
    )

    for argv in (claude, codex, gemini, grok):
        assert huge not in argv

    # claude -p with no prompt arg reads stdin (last token stays a flag/value).
    assert "-p" in claude
    assert claude[-1] != huge
    # codex exec reads the prompt from stdin when handed "-".
    assert codex[-1] == "-"
    # gemini stays headless via an empty -p while the prompt arrives on stdin.
    assert gemini[-2:] == ["-p", ""]
    # grok reads the prompt from a file (no stdin mode; empty -p errors).
    assert grok[-2:] == ["--prompt-file", "/tmp/p.txt"]


def test_build_argv_grok_empty_prompt_never_emits_empty_dash_p() -> None:
    """Regression for #160: grok must never be invoked with ``-p ""``.

    The Grok CLI validates that ``-p/--single`` is non-empty before reading
    anything, so an empty value fails with ``--single: prompt is empty``. With
    no prompt-file the real prompt must be passed via ``-p``; with one it must
    use ``--prompt-file`` — never an empty ``-p``.
    """
    direct = build_argv(AgentType.GROK, "real prompt", binary="grok", prompt_on_stdin=True)
    assert direct[-2:] == ["-p", "real prompt"]
    assert "" not in direct

    via_file = build_argv(
        AgentType.GROK, "real prompt", binary="grok", prompt_on_stdin=True, prompt_file="/tmp/p"
    )
    assert via_file[-2:] == ["--prompt-file", "/tmp/p"]


async def test_feed_prompt_stdin_writes_and_closes() -> None:
    from agentshore.agents.cli_agent import _feed_prompt_stdin

    proc = _FakeProcess(_codex_json_lines())
    await _feed_prompt_stdin(proc, "the full prompt")  # type: ignore[arg-type]

    assert proc.stdin.data == b"the full prompt"
    assert proc.stdin.closed is True


def test_build_argv_grok_shape() -> None:
    argv = build_argv(
        AgentType.GROK,
        "do the thing",
        binary="grok",
        model="grok-build",
        reasoning_effort="medium",
        project_dir="/worktree",
    )

    assert argv == [
        "grok",
        "--no-auto-update",
        "--no-subagents",
        "--verbatim",
        # Ephemeral single-turn dispatches: cross-session memory is meaningless
        # and slow, and plan mode adds an unwanted planning round.
        "--no-memory",
        "--no-plan",
        "--cwd",
        "/worktree",
        "--output-format",
        "streaming-json",
        "-m",
        "grok-build",
        "--effort",
        "medium",
        "--permission-mode",
        "bypassPermissions",
        "-p",
        "do the thing",
    ]


def test_build_argv_antigravity_shape() -> None:
    """``agy`` argv: plain-text passthrough — no ``--output-format``, no effort flag.

    The YOLO default supplies ``--dangerously-skip-permissions``; the model is the
    display-name string with the reasoning effort baked in, so there is no
    separate ``--effort`` flag and no JSON stream-format flag.
    """
    argv = build_argv(
        AgentType.ANTIGRAVITY,
        "do the thing",
        binary="agy",
        model="Gemini 3.5 Flash (Low)",
        project_dir="/wt",
    )

    assert argv == [
        "agy",
        "--model",
        "Gemini 3.5 Flash (Low)",
        "--add-dir",
        "/wt",
        "--print-timeout",
        "50m0s",
        "--dangerously-skip-permissions",
        "-p",
        "do the thing",
    ]
    assert "--output-format" not in argv


def test_build_argv_antigravity_prompt_always_in_argv_never_stdin() -> None:
    """``agy`` has no stdin prompt mode — the real prompt must always ride ``-p``.

    Even when ``prompt_on_stdin`` is set (the Windows arg-length path the other
    CLIs use), antigravity keeps the verbatim prompt as the trailing ``-p`` value
    and never emits an empty ``-p``/``-`` placeholder. The dispatch layer relies
    on this to keep stdin closed for antigravity (it would otherwise block on a
    pipe the child never drains).
    """
    huge = "x" * 20000
    argv = build_argv(
        AgentType.ANTIGRAVITY,
        huge,
        binary="agy",
        model="Gemini 3.5 Flash (Low)",
        project_dir="/wt",
        prompt_on_stdin=True,
    )
    assert argv[-2:] == ["-p", huge]
    assert "" not in argv
    assert "-" not in argv


async def test_read_output_antigravity_passthrough_returns_raw_verbatim() -> None:
    """``agy`` has no ``_PARSERS`` entry, so plain-text stdout is returned verbatim.

    The embedded JSON result block survives untouched (no JSONL extraction), and
    ``parse_skill_result`` can still pull ``success=True`` out of the raw text.
    """
    # No parser for antigravity → the read loop takes the raw passthrough branch.
    assert AgentType.ANTIGRAVITY not in _PARSERS

    raw_text = 'Working on it...\nHere is the result: {"success": true, "summary": "ok"}\nDone.\n'
    proc = _FakeProcess([raw_text.encode()])
    out = await _read_output(
        proc,  # type: ignore[arg-type]
        AgentType.ANTIGRAVITY,
        max_bytes=10_000_000,
        line_limit=4_194_304,
        agent_id="agy-1",
    )

    # Raw stdout is returned byte-for-byte; no token usage is parsed.
    assert out.raw == raw_text
    assert out.usage.tokens_in == 0
    assert out.usage.tokens_out == 0
    assert out.session_id is None

    parsed = parse_skill_result(out.raw)
    assert parsed.success is True


async def test_read_output_emits_cli_first_byte_once_with_elapsed() -> None:
    """First stdout byte emits exactly one ``cli_first_byte`` with a TTFB (#212)."""
    import time

    import structlog

    from agentshore.agents.cli_agent import _StdoutActivity

    activity = _StdoutActivity(
        last_stdout_at=time.monotonic(), dispatch_start=time.monotonic() - 0.05
    )
    proc = _FakeProcess(_codex_json_lines())  # multiple stdout lines
    with structlog.testing.capture_logs() as logs:
        await _read_output(
            proc,  # type: ignore[arg-type]
            AgentType.CODEX,
            max_bytes=10_000_000,
            line_limit=4_194_304,
            agent_id="codex-1",
            stdout_activity=activity,
        )

    first_byte = [e for e in logs if e.get("event") == "cli_first_byte"]
    assert len(first_byte) == 1  # only the first byte, not every line
    evt = first_byte[0]
    assert evt["agent_id"] == "codex-1"
    assert evt["agent_type"] == str(AgentType.CODEX)
    assert evt["elapsed_ms"] >= 0
    assert activity.first_byte_at is not None


async def test_read_output_no_first_byte_event_without_dispatch_context() -> None:
    """No ``cli_first_byte`` when the activity carries no dispatch_start (unit path)."""
    import time

    import structlog

    from agentshore.agents.cli_agent import _StdoutActivity

    activity = _StdoutActivity(last_stdout_at=time.monotonic())  # dispatch_start=0.0
    proc = _FakeProcess(_codex_json_lines())
    with structlog.testing.capture_logs() as logs:
        await _read_output(
            proc,  # type: ignore[arg-type]
            AgentType.CODEX,
            max_bytes=10_000_000,
            line_limit=4_194_304,
            agent_id="codex-1",
            stdout_activity=activity,
        )

    assert not any(e.get("event") == "cli_first_byte" for e in logs)
    assert activity.received_any is True  # mark() still flipped


def test_antigravity_first_byte_deadline_is_1800s() -> None:
    """agy emits no stdout until its async task completes (#217); the first-byte
    watchdog stays generous (30 min) so long code-review tasks don't die as
    spurious launch wedges."""
    from agentshore.agents.cli_agent import _FIRST_BYTE_DEADLINE_BY_TYPE

    assert _FIRST_BYTE_DEADLINE_BY_TYPE[AgentType.ANTIGRAVITY] == 1800.0


def test_extract_output_antigravity_passthrough_when_no_status_block() -> None:
    """Plain streaming output (no task-status envelope) is returned unchanged."""
    from agentshore.agents.cli_antigravity import extract_output

    raw = 'Thinking...\n{"success": true, "error": null}\n'
    assert extract_output(raw) == raw


def test_extract_output_antigravity_extracts_output_section() -> None:
    """Task-status block: the content between Output: and Error: is returned."""
    from agentshore.agents.cli_antigravity import extract_output

    raw = (
        "[Task abc123/task-1 Status Update]\n"
        "Status: COMPLETED\n"
        "Exit Code: 0\n"
        "Log Path: file:///some/path/task-1.log\n"
        "Output:\n"
        '{"success": true, "error": null}\n'
        "Error: (none)\n"
    )
    result = extract_output(raw)
    assert result == '{"success": true, "error": null}'


def test_extract_output_antigravity_empty_output_normalised() -> None:
    """(empty) output section is normalised to empty string, not the literal string."""
    from agentshore.agents.cli_antigravity import extract_output

    raw = (
        "[Task abc123/task-2 Status Update]\n"
        "Status: COMPLETED\n"
        "Exit Code: 0\n"
        "Log Path: file:///some/path/task-2.log\n"
        "Output:\n"
        "(empty)\n"
        "Error: timed out waiting for response\n"
    )
    assert extract_output(raw) == ""


@pytest.mark.parametrize(
    "alias",
    [
        "grok-build-0.1",
        "grok-code-fast-1",
        "grok-code-fast",
        "grok-code-fast-1-0825",
    ],
)
def test_build_argv_grok_normalizes_cli_model_aliases(alias: str) -> None:
    argv = build_argv(AgentType.GROK, "do the thing", binary="grok", model=alias)

    assert argv[argv.index("-m") + 1] == "grok-build"


def test_build_argv_grok_prefers_grok_default_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_which(name: str) -> str | None:
        return f"/usr/local/bin/{name}" if name in {"grok", "grok-build"} else None

    monkeypatch.setattr("agentshore.agents.cli_grok.shutil.which", fake_which)

    argv = build_argv(AgentType.GROK, "do the thing")

    assert argv[0] == "grok"


def test_build_argv_grok_falls_back_to_grok_build_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_which(name: str) -> str | None:
        return "/usr/local/bin/grok-build" if name == "grok-build" else None

    monkeypatch.setattr("agentshore.agents.cli_grok.shutil.which", fake_which)

    argv = build_argv(AgentType.GROK, "do the thing")

    assert argv[0] == "grok-build"


def test_build_argv_grok_explicit_flags_replace_permission_default_only() -> None:
    argv = build_argv(
        AgentType.GROK,
        "do the thing",
        binary="grok",
        project_dir="/worktree",
        extra_flags=("--permission-mode", "readOnly"),
    )

    assert "--no-auto-update" in argv
    assert "--no-subagents" in argv
    assert "--verbatim" in argv
    assert "--cwd" in argv
    assert "--output-format" in argv
    assert "streaming-json" in argv
    assert "bypassPermissions" not in argv
    assert "--permission-mode" in argv
    assert "readOnly" in argv
    assert "--worktree" not in argv
    assert "--best-of-n" not in argv
    assert "--agents" not in argv
    assert "--resume" not in argv
    assert "--continue" not in argv
    assert "--session-id" not in argv


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
    """Regression â€” `session_id` / `is_resume` were removed; every dispatch
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


@pytest.mark.parametrize("identity_env", [None, {"GH_TOKEN": "tok"}])
async def test_dispatch_cli_pins_noninteractive_git_editor(
    monkeypatch: pytest.MonkeyPatch,
    identity_env: dict[str, str] | None,
) -> None:
    """Agent subprocesses run git rebase/commit inside skills; the env must
    carry a non-interactive editor so a rebase-internal ``git commit -e`` can't
    open vim and hang forever, leaking the worktree (#168). The editor must be
    pinned with or without an identity overlay (None previously leaked raw
    os.environ with no editor set)."""
    captured_kwargs: list[dict[str, Any]] = []

    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        captured_kwargs.append(kwargs)
        return _FakeProcess(_codex_json_lines())

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    cfg = AgentConfig(enabled=True, binary="codex", timeout=10)
    handle = _make_handle(agent_type=AgentType.CODEX)

    result = await dispatch_cli(handle, "prompt", cfg=cfg, identity_env=identity_env)

    assert result.exit_code == 0
    env = captured_kwargs[0]["env"]
    assert env is not None
    assert env["GIT_EDITOR"] == "true"
    assert env["GIT_SEQUENCE_EDITOR"] == "true"
    if identity_env is not None:
        assert env["GH_TOKEN"] == "tok"


async def test_dispatch_cli_injects_per_identity_git_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the agent's identity carries a token, the dispatched env hardens git
    (``GIT_TERMINAL_PROMPT=0`` — so a credential prompt fails fast instead of
    hanging the full wall-clock, #177) AND injects the token as an HTTPS
    Basic-auth header so the agent's own ``git push`` authenticates AS ITS OWN
    identity, non-interactively. Multi-identity-safe: the header is derived from
    *this* subprocess's token."""
    captured_kwargs: list[dict[str, Any]] = []

    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        captured_kwargs.append(kwargs)
        return _FakeProcess(_codex_json_lines())

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    cfg = AgentConfig(enabled=True, binary="codex", timeout=10)
    handle = _make_handle(agent_type=AgentType.CODEX)

    result = await dispatch_cli(handle, "prompt", cfg=cfg, identity_env={"GH_TOKEN": "tok-xyz"})

    assert result.exit_code == 0
    env = captured_kwargs[0]["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    count = int(env["GIT_CONFIG_COUNT"])
    values = [env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)]
    expected = base64.b64encode(b"x-access-token:tok-xyz").decode("ascii")
    assert f"Authorization: Basic {expected}" in values


async def test_dispatch_cli_hardens_git_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no identity token the env is still hardened (no hang) but injects no
    auth header — nothing to authenticate with, so no GIT_CONFIG auth trio."""
    captured_kwargs: list[dict[str, Any]] = []

    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        captured_kwargs.append(kwargs)
        return _FakeProcess(_codex_json_lines())

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    cfg = AgentConfig(enabled=True, binary="codex", timeout=10)
    handle = _make_handle(agent_type=AgentType.CODEX)

    result = await dispatch_cli(handle, "prompt", cfg=cfg, identity_env=None)

    assert result.exit_code == 0
    env = captured_kwargs[0]["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_CONFIG_COUNT" not in env


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="prompt-on-stdin is Windows-only")
async def test_dispatch_cli_feeds_prompt_via_stdin_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Windows a large prompt is delivered over stdin, not as an argv element,
    so npm .cmd shims can't trip the cmd.exe command-line limit."""
    captured_argv: list[list[str]] = []
    captured_kwargs: list[dict[str, Any]] = []
    procs: list[_FakeProcess] = []

    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        captured_argv.append(list(argv))
        captured_kwargs.append(kwargs)
        proc = _FakeProcess(_codex_json_lines())
        procs.append(proc)
        return proc

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    cfg = AgentConfig(enabled=True, binary="codex", timeout=10)
    handle = _make_handle(agent_type=AgentType.CODEX)

    big_prompt = "groom the backlog " * 2000  # ~36 KB â€” over cmd.exe's ~8191 limit
    result = await dispatch_cli(handle, big_prompt, cfg=cfg)

    assert result.exit_code == 0
    assert big_prompt not in captured_argv[0]
    assert captured_argv[0][-1] == "-"  # codex reads the prompt from stdin
    assert captured_kwargs[0]["stdin"] is asyncio.subprocess.PIPE
    assert procs[0].stdin.data == big_prompt.encode("utf-8")
    assert procs[0].stdin.closed is True


async def test_dispatch_cli_never_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression â€” every CLI dispatch starts a fresh session. --resume was
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
# Happy path â€” plain output (Codex-style)
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


async def test_dispatch_cli_success_grok_streaming_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(_grok_json_lines())

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    cfg = AgentConfig(enabled=True, binary="grok", timeout=10)
    handle = _make_handle(agent_type=AgentType.GROK)
    result = await dispatch_cli(
        handle,
        "prompt",
        cfg=cfg,
        pricing=_price_quote(
            cost_per_1k_input=0.001,
            cost_per_1k_cached_input=0.0002,
            cost_per_1k_output=0.002,
        ),
    )

    assert result.exit_code == 0
    assert result.tokens_in == 120
    assert result.cached_tokens_in == 40
    assert result.tokens_out == 30
    assert result.session_id == "grok-session"
    expected = (80 / 1000) * 0.001 + (40 / 1000) * 0.0002 + (30 / 1000) * 0.002
    assert result.dollar_cost == pytest.approx(expected)
    sr = parse_skill_result(result.raw_output)
    assert sr.success is True


async def test_dispatch_cli_claude_prefers_vendor_total_cost_usd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude's authoritative ``total_cost_usd`` overrides token-derivation.

    The pricing quote here would derive a far smaller figure from the tokens; the
    vendor number (which accounts for the exact model + 1h ephemeral-cache tier)
    must win, fixing the ~2x dashboard cost undercount.
    """
    lines = [
        json.dumps(
            {
                "type": "result",
                "session_id": "claude-cost",
                "total_cost_usd": 0.063115,
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

    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(lines)

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    cfg = AgentConfig(enabled=True, binary="claude", timeout=10)
    handle = _make_handle(agent_type=AgentType.CLAUDE_CODE)
    result = await dispatch_cli(
        handle,
        "prompt",
        cfg=cfg,
        pricing=_price_quote(cost_per_1k_input=0.003, cost_per_1k_output=0.015),
    )

    # Token counts are still captured for stats/display...
    assert result.tokens_in == 1000  # 100 + 700 cache-read + 200 cache-write
    assert result.tokens_out == 50
    # ...but the billed cost is the vendor figure, not the token derivation.
    assert result.dollar_cost == pytest.approx(0.063115)


async def test_dispatch_cli_claude_falls_back_to_token_cost_without_total_cost_usd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``total_cost_usd`` on the result event → token-derived cost as before."""

    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(_claude_cached_json_lines())

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    cfg = AgentConfig(enabled=True, binary="claude", timeout=10)
    handle = _make_handle(agent_type=AgentType.CLAUDE_CODE)
    result = await dispatch_cli(
        handle,
        "prompt",
        cfg=cfg,
        pricing=_price_quote(cost_per_1k_input=0.003, cost_per_1k_output=0.015),
    )

    # uncached 100 @ .003 + cache-read 700 @ .0003 + cache-write 200 @ .00375 + out 50 @ .015
    expected = (
        (100 / 1000) * 0.003 + (700 / 1000) * 0.0003 + (200 / 1000) * 0.00375 + (50 / 1000) * 0.015
    )
    assert result.dollar_cost == pytest.approx(expected)


async def test_dispatch_cli_codex_json_discounts_cached_input_and_does_not_double_count_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(_codex_cached_json_lines())

    monkeypatch.setattr(
        "agentshore.agents.cli_agent.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    cfg = AgentConfig(enabled=True, binary="codex", timeout=10)
    handle = _make_handle(agent_type=AgentType.CODEX)
    # No explicit cached rate → cache_read_multiplier (0.1) yields 0.0003.
    result = await dispatch_cli(
        handle,
        "prompt",
        cfg=cfg,
        pricing=_price_quote(cost_per_1k_input=0.003, cost_per_1k_output=0.012),
    )

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
# Happy path â€” stream-json output (Claude-style)
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
    cfg = AgentConfig(enabled=True, binary="claude", timeout=10)
    handle = _make_handle(agent_type=AgentType.CLAUDE_CODE)
    result = await dispatch_cli(
        handle,
        "prompt",
        cfg=cfg,
        pricing=_price_quote(
            cost_per_1k_input=0.003,
            cost_per_1k_cached_input=0.0003,
            cost_per_1k_cache_write_input=0.00375,
            cost_per_1k_output=0.015,
        ),
    )

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
# Non-zero exit â†’ AgentProcessError
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
# Output overflow â†’ AgentOutputInvalid
# ---------------------------------------------------------------------------


async def test_dispatch_cli_output_overflow_raises(
    mock_agent_path: Path,
) -> None:
    cfg = AgentConfig(
        enabled=True,
        binary=str(mock_agent_path),
        timeout=10,
        max_output_size=10,  # tiny cap â€” mock output will exceed this
    )
    handle = _make_handle(agent_type=AgentType.CODEX)
    with pytest.raises(AgentOutputInvalid, match="max_output_size"):
        await dispatch_cli(handle, "prompt", cfg=cfg, python_executable=sys.executable)


# ---------------------------------------------------------------------------
# Per-line buffer cap (asyncio readline limit) â†’ AgentOutputInvalid
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
    # structlog routing varies by test ordering â€” accept the warning surfacing
    # via either capsys (printed structlog) or caplog (stdlib propagation).
    captured = capsys.readouterr()
    in_capsys = "cli_agent_large_line" in (captured.out + captured.err)
    in_caplog = any("cli_agent_large_line" in r.getMessage() for r in caplog.records)
    assert in_capsys or in_caplog


# ---------------------------------------------------------------------------
# Timeout â†’ PlayTimeoutError + SIGTERM/SIGKILL
# ---------------------------------------------------------------------------


async def test_dispatch_cli_timeout_raises(
    mock_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_AGENT_MODE", "timeout")
    cfg = AgentConfig(
        enabled=True,
        binary=str(mock_agent_path),
        timeout=1,  # 1 second â€” mock sleeps forever
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
# _kill_process â€” Windows teardown path (no os.killpg / os.getpgid)
# ---------------------------------------------------------------------------


class _FakeKillProcess:
    """Minimal proc stand-in for _kill_process: a pid and an awaitable wait()."""

    def __init__(self, pid: int | None = 9999, *, returncode: int = 0) -> None:
        self.pid = pid
        self.returncode = returncode
        self._transport = None

    async def wait(self) -> int:
        return self.returncode


async def test_kill_process_uses_taskkill_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under Windows-simulation, _kill_process tears the tree down by PID via
    ``subprocess_env.kill_tree_sync`` (taskkill) and never touches os.killpg
    (which is absent on Windows -> AttributeError)."""
    import os as _os

    from agentshore.agents import cli_agent as ca

    # Simulate Windows: hasattr(os, "killpg") is False, getpgid absent too.
    monkeypatch.delattr(_os, "killpg", raising=False)
    monkeypatch.delattr(_os, "getpgid", raising=False)

    killed_pids: list[int] = []
    monkeypatch.setattr(
        "agentshore.agents.cli_agent.subprocess_env.kill_tree_sync",
        lambda pid: killed_pids.append(pid),
    )

    proc = _FakeKillProcess(pid=4321)
    # Must not raise AttributeError despite os.killpg being absent.
    await ca._kill_process(proc, "agent-win")  # type: ignore[arg-type]

    # The process tree was torn down by pid; the process exited within grace so
    # there is no post-grace retry.
    assert killed_pids == [4321]


async def test_kill_process_windows_no_warn_when_process_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process that has already exited is benign: the tree kill is attempted
    but teardown succeeds, so nothing is logged as a failure."""
    import os as _os

    from agentshore.agents import cli_agent as ca

    monkeypatch.delattr(_os, "killpg", raising=False)
    monkeypatch.delattr(_os, "getpgid", raising=False)
    monkeypatch.setattr(
        "agentshore.agents.cli_agent.subprocess_env.kill_tree_sync",
        lambda _pid: None,
    )
    mock_logger = MagicMock()
    monkeypatch.setattr(ca, "_logger", mock_logger)

    # _FakeKillProcess.returncode is 0 -> the process exited, so teardown
    # succeeded and nothing is logged.
    proc = _FakeKillProcess(pid=4321)
    await ca._kill_process(proc, "agent-win")  # type: ignore[arg-type]

    warnings = [
        c for c in mock_logger.warning.call_args_list if c.args and c.args[0] == "taskkill_failed"
    ]
    assert warnings == []


async def test_kill_process_windows_bounds_wait_when_force_kill_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the tree kill cannot stop the process, teardown still completes via a
    bounded wait instead of hanging the session forever (codex review P2)."""
    import os as _os

    from agentshore.agents import cli_agent as ca

    monkeypatch.delattr(_os, "killpg", raising=False)
    monkeypatch.delattr(_os, "getpgid", raising=False)
    monkeypatch.setattr(ca, "_SIGKILL_GRACE", 0.01)

    killed_pids: list[int] = []
    # taskkill is a no-op here: the process never dies, simulating an
    # unkillable tree.
    monkeypatch.setattr(
        "agentshore.agents.cli_agent.subprocess_env.kill_tree_sync",
        lambda pid: killed_pids.append(pid),
    )
    mock_logger = MagicMock()
    monkeypatch.setattr(ca, "_logger", mock_logger)

    class _HangingProc(_FakeKillProcess):
        async def wait(self) -> int:
            await asyncio.sleep(3600)  # never exits on its own
            return 0

    # returncode stays None — the process never dies, so the tree kill genuinely
    # failed and the warning must fire (unlike the already-gone benign case).
    proc = _HangingProc(pid=4321, returncode=None)  # type: ignore[arg-type]
    # Guard the test itself: a regression would hang here instead of returning.
    await asyncio.wait_for(ca._kill_process(proc, "agent-win"), timeout=5)  # type: ignore[arg-type]

    # The tree kill was attempted twice (initial + post-grace retry) and the
    # unrecoverable failure was surfaced as a warning, not raised.
    assert killed_pids == [4321, 4321]
    warnings = [
        c for c in mock_logger.warning.call_args_list if c.args and c.args[0] == "taskkill_failed"
    ]
    assert len(warnings) == 1
    assert warnings[0].kwargs["pid"] == 4321


@pytest.mark.real_resolve_executable
def test_resolve_executable_resolves_npm_shim_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex/claude/gemini are .cmd npm shims; CreateProcess only finds bare
    names ending in .exe, so resolve to the full .cmd path via shutil.which."""
    import shutil
    import sys

    from agentshore.agents import cli_agent as ca

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", lambda _name: r"C:\npm\codex.CMD")

    out = ca._resolve_executable(["codex", "exec", "--json"])
    assert out == [r"C:\npm\codex.CMD", "exec", "--json"]


@pytest.mark.real_resolve_executable
def test_resolve_executable_noop_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from agentshore.agents import cli_agent as ca

    monkeypatch.setattr(sys, "platform", "linux")
    assert ca._resolve_executable(["codex", "exec"]) == ["codex", "exec"]


@pytest.mark.real_resolve_executable
def test_resolve_executable_noop_when_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import shutil
    import sys

    from agentshore.agents import cli_agent as ca

    monkeypatch.setattr(sys, "platform", "win32")
    called: list[str] = []
    monkeypatch.setattr(shutil, "which", lambda n: called.append(n) or None)
    abs_path = os.path.abspath("python")  # absolute on the test runner

    assert ca._resolve_executable([abs_path, "script"]) == [abs_path, "script"]
    assert called == []  # absolute paths are not re-resolved


@pytest.mark.real_resolve_executable
def test_resolve_executable_noop_when_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil
    import sys

    from agentshore.agents import cli_agent as ca

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert ca._resolve_executable(["missing", "arg"]) == ["missing", "arg"]


# ---------------------------------------------------------------------------
# multi_block â€” parser uses last result block
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

    monkeypatch.setenv("PRE_EXISTING", "kept")
    await dispatch_cli(handle, "prompt", cfg=cfg)

    # With no identity_env the child still inherits the full parent environment
    # — but env is now an explicit superset dict (not None) so the
    # non-interactive git editor can be pinned to stop a rebase-internal
    # ``git commit -e`` opening vim and hanging (#168).
    env = captured["env"]
    assert env is not None
    assert env["PRE_EXISTING"] == "kept"
    assert env["GIT_EDITOR"] == "true"
    assert env["GIT_SEQUENCE_EDITOR"] == "true"


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
    cfg = AgentConfig(enabled=True, binary="codex", timeout=10, identity="bot-user")
    handle = _make_handle(agent_type=AgentType.CODEX)

    overlay = {
        "GIT_AUTHOR_NAME": "bot-user",
        "GIT_AUTHOR_EMAIL": "bot@example.com",
        "GH_TOKEN": "ghp_test",
    }
    await dispatch_cli(handle, "prompt", cfg=cfg, identity_env=overlay)

    env = captured["env"]
    assert env is not None
    # Parent env preserved.
    assert env["PRE_EXISTING"] == "kept"
    # Overlay applied.
    assert env["GIT_AUTHOR_NAME"] == "bot-user"
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


def test_classify_error_claude_session_limit_from_stdout() -> None:
    assert (
        _classify_error(
            1,
            "",
            "You've hit your session limit · resets 6:20am (America/Chicago)",
        )
        is ErrorClass.RATE_LIMIT
    )


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


@pytest.mark.parametrize(
    "stderr",
    [
        "ERROR failed to renew cache TTL",
        "warn: failed to refresh available models, retrying",
    ],
)
def test_classify_error_codex_backend_ttl_expiry_on_stderr_is_auth(stderr: str) -> None:
    """Codex backend session-token expiry markers on stderr classify as AUTH."""
    assert _classify_error(1, stderr, "") is ErrorClass.AUTH


@pytest.mark.parametrize(
    "stdout",
    [
        "failed to renew cache ttl",
        "failed to refresh available models",
    ],
)
def test_classify_error_codex_ttl_markers_on_stdout_only_are_not_auth(stdout: str) -> None:
    """The TTL markers are stderr-only: the same strings in an agent's stdout
    work product must NOT trigger AUTH (they are not in _AUTH_STDOUT)."""
    assert _classify_error(1, "", stdout) is not ErrorClass.AUTH


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


# ---------------------------------------------------------------------------
# stderr auth-sniffer (#zeke auth-hang): a backend session-token expiry that
# hangs the process on stdin must be killed as AUTH in well under the idle
# timeout, not after the full stream_idle_timeout as TIMEOUT_STREAM_IDLE.
# ---------------------------------------------------------------------------


def test_stderr_sniffer_feed_flags_auth_tail() -> None:
    sniffer = _StderrSniffer()
    assert sniffer.feed("starting up\n") is False
    # First auth marker flips the flag and returns True exactly once.
    assert sniffer.feed("ERROR failed to renew cache TTL\n") is True
    assert sniffer.auth_hit is True
    # Subsequent feeds (even more markers) do not re-fire.
    assert sniffer.feed("failed to refresh available models\n") is False


def test_stderr_sniffer_feed_no_match_on_clean_stderr() -> None:
    sniffer = _StderrSniffer()
    assert sniffer.feed("INFO booting model\nINFO ready\n") is False
    assert sniffer.auth_hit is False
    assert "booting model" in sniffer.captured


def test_stderr_sniffer_tail_is_bounded() -> None:
    # The tail never grows unbounded regardless of how much stderr is fed, and
    # a marker landing within the live window is still caught.
    sniffer = _StderrSniffer(tail_window=64)
    assert sniffer.feed("x" * 4096) is False  # no marker, far exceeds the window
    assert len(sniffer.tail) <= 64
    # Marker landing now (fits the 64-byte window) is still caught.
    assert sniffer.feed("failed to renew cache ttl") is True


def test_stderr_sniffer_suppresses_transient_cache_renewal_eof() -> None:
    """#190: the transient cache-renewal EOF-parse blip must NOT trip auth.

    Codex prints this during a transient model-cache TTL renewal blip; the
    agent keeps working on the very next dispatch, so it is not an auth
    rejection and must not abort in-flight work (observed 415s lost)."""
    sniffer = _StderrSniffer()
    line = (
        "ERROR codex_models_manager::manager: failed to renew cache TTL: "
        "EOF while parsing a value at line 1 column 0\n"
    )
    assert sniffer.feed(line) is False
    assert sniffer.auth_hit is False


def test_stderr_sniffer_bare_cache_renewal_still_trips() -> None:
    """#190: a bare cache-renewal line (no EOF-parse suffix) is a genuine
    session-token expiry and must still flip auth_hit."""
    sniffer = _StderrSniffer()
    assert sniffer.feed("ERROR failed to renew cache TTL\n") is True
    assert sniffer.auth_hit is True

    sniffer2 = _StderrSniffer()
    assert sniffer2.feed("warn: failed to refresh available models, retrying\n") is True
    assert sniffer2.auth_hit is True


def test_stderr_sniffer_real_auth_rejection_still_trips() -> None:
    """#190: a real backend-auth rejection (401/unauthorized) is unaffected by
    the cache-renewal suppression and still trips."""
    sniffer = _StderrSniffer()
    assert sniffer.feed("HTTP 401 Unauthorized: invalid api key\n") is True
    assert sniffer.auth_hit is True


def test_stderr_sniffer_real_401_trips_even_alongside_cache_blip() -> None:
    """#190: the suppression must apply ONLY to the cache-renewal markers — a
    real 401 coexisting in the same tail with a transient cache-renewal+EOF
    line must still trip (do not let the blip mask a genuine rejection)."""
    sniffer = _StderrSniffer()
    text = (
        "ERROR codex_models_manager::manager: failed to renew cache TTL: "
        "EOF while parsing a value at line 1 column 0\n"
        "HTTP 401 Unauthorized\n"
    )
    assert sniffer.feed(text) is True
    assert sniffer.auth_hit is True


class _FakeStderr:
    """Minimal async-iterable stand-in for ``proc.stderr`` (a StreamReader)."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self) -> _FakeStderr:
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self._lines.pop(0)


class _FakeProc:
    def __init__(self, stderr_lines: list[bytes]) -> None:
        self.stderr = _FakeStderr(stderr_lines)


@pytest.mark.asyncio
async def test_watch_stderr_auth_aborts_fast_with_auth_class() -> None:
    """The watcher raises PlayTimeoutError(AUTH) as soon as a marker lands —
    in well under a second, never the multi-minute idle window."""
    proc = _FakeProc([b"booting\n", b"failed to renew cache TTL\n", b"more\n"])
    sniffer = _StderrSniffer()
    with pytest.raises(PlayTimeoutError) as excinfo:
        await asyncio.wait_for(
            _watch_stderr_auth(  # type: ignore[arg-type]
                proc,
                sniffer,
                agent_id="codex-1",
                agent_type="codex",
            ),
            timeout=2.0,
        )
    assert excinfo.value.error_class is ErrorClass.AUTH
    assert sniffer.auth_hit is True


@pytest.mark.asyncio
async def test_watch_stderr_auth_sleeps_on_clean_eof() -> None:
    """With no auth marker the watcher drains stderr then yields indefinitely
    (it loses the read/idle race rather than completing)."""
    proc = _FakeProc([b"INFO ok\n", b"INFO done\n"])
    sniffer = _StderrSniffer()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            _watch_stderr_auth(  # type: ignore[arg-type]
                proc,
                sniffer,
                agent_id="codex-1",
                agent_type="codex",
            ),
            timeout=0.2,
        )
    assert sniffer.auth_hit is False
    assert "done" in sniffer.captured


def test_classify_error_sigkill_is_crash_not_unknown() -> None:
    """SIGKILL (-9, e.g. OS OOM kill) must be a crash, not the rate-limit-eligible
    'unknown' bucket (#7 â€” the mass -9 burst was misclassified as rate_limit)."""
    assert _classify_error(-9, "", "") == "crash_signal"
    assert _classify_error(-6, "", "build was a long compile") == "crash_signal"


def test_classify_error_oom_signature() -> None:
    assert _classify_error(-9, "", "fatal: Out of memory") == "crash_oom"
    assert _classify_error(1, "Cannot allocate memory", "") == "crash_oom"


def test_classify_error_enospc_signature() -> None:
    """Host disk-full surfaced by the agent is an environment condition (#180)."""
    assert _classify_error(1, "fatal: write error: No space left on device", "") == "crash_enospc"
    assert _classify_error(1, "", "error: ENOSPC: no space left") == "crash_enospc"
    assert _classify_error(1, "OSError: [Errno 28] No space left on device", "") == "crash_enospc"


def test_classify_error_graceful_signals_stay_unknown() -> None:
    """SIGTERM/SIGINT are AgentShore/OS-initiated graceful stops, not crashes."""
    assert _classify_error(-15, "", "") == "unknown"
    assert _classify_error(-2, "", "") == "unknown"


# ---------------------------------------------------------------------------
# _is_terminal_event (#21 â€” response-complete fast-kill for all agent types)
# ---------------------------------------------------------------------------


def test_is_terminal_event_detects_each_agent_type() -> None:
    """Claude/Gemini emit type:result; Codex emits turn.completed. CLI agents
    must be recognized so the 60s post-response grace applies (not the 30-min
    stream_idle_timeout)."""
    assert _is_terminal_event(b'{"type":"result","result":"ok"}', AgentType.CLAUDE_CODE)
    assert _is_terminal_event(b'{"type":"result","response":"ok"}', AgentType.GEMINI)
    assert _is_terminal_event(
        b'{"type":"turn.completed","usage":{"input_tokens":1}}', AgentType.CODEX
    )
    # Grok's real terminal event uses type:"end".
    assert _is_terminal_event(
        b'{"type":"end","stopReason":"EndTurn","sessionId":"grok-session"}',
        AgentType.GROK,
    )
    # Grok CLI may also use the ``event`` key instead of ``type``.
    assert _is_terminal_event(b'{"event":"end","stopReason":"EndTurn"}', AgentType.GROK)


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
    # recovery set did not â€” the agent will skip the take_break override and
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


def test_extract_text_from_grok_jsonl_real_format_with_usage() -> None:
    """Narrow parser: text stream + end terminal with Grok-native usage keys."""
    raw = "\n".join(
        [
            json.dumps({"type": "session.started", "metadata": {"sessionId": "grok-1"}}),
            json.dumps({"type": "text", "data": "hel"}),
            json.dumps({"type": "text", "data": "lo"}),
            json.dumps(
                {
                    "type": "end",
                    "stopReason": "EndTurn",
                    "usage": {
                        "prompt_tokens": 10,
                        "cached_input_tokens": 3,
                        "completion_tokens": 4,
                    },
                }
            ),
        ]
    )
    text, usage, session_id = _extract_text_from_grok_jsonl(raw)
    assert text == "hello"
    assert session_id == "grok-1"
    assert usage.tokens_in == 10
    assert usage.cached_tokens_in == 3
    assert usage.tokens_out == 4


def test_extract_text_from_grok_jsonl_reassembles_text_data_stream() -> None:
    result = {
        "schema_version": 1,
        "success": True,
        "artifacts": [],
        "issues_created": [],
        "requested_mutations": [],
        "metrics": {},
        "error": None,
    }
    raw = "\n".join(
        [
            json.dumps({"type": "text", "data": "```json\n"}),
            json.dumps({"type": "text", "data": json.dumps(result)}),
            json.dumps({"type": "text", "data": "\n```"}),
            json.dumps(
                {
                    "type": "end",
                    "stopReason": "EndTurn",
                    "sessionId": "019ea33a-3fd4-7e52-845b-2df0c89494a0",
                }
            ),
        ]
    )

    text, usage, session_id = _extract_text_from_grok_jsonl(raw)

    assert session_id == "019ea33a-3fd4-7e52-845b-2df0c89494a0"
    assert usage.tokens_in == 0
    assert usage.cached_tokens_in == 0
    assert usage.tokens_out == 0
    assert parse_skill_result(text).success is True


def test_extract_text_from_grok_jsonl_keeps_terminal_result_with_non_assistant_role() -> None:
    raw = "\n".join(
        [
            json.dumps(
                {
                    "type": "message",
                    "role": "user",
                    "message": {"content": "ignore user echo"},
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "role": "system",
                    "result": {"content": "final answer"},
                }
            ),
        ]
    )

    text, _, _ = _extract_text_from_grok_jsonl(raw)

    assert text == "final answer"


def test_extract_text_from_grok_jsonl_result_extraction_succeeds_94() -> None:
    """Regression: #94 — groom_backlog play emitted agent_json_retry with 'no
    valid result block found'.  The narrow parser must correctly extract the
    result JSON from a realistic Grok CLI transcript so that result extraction
    succeeds on the first attempt."""
    result_payload = {
        "schema_version": 1,
        "success": True,
        "artifacts": [],
        "issues_created": [],
        "requested_mutations": [],
        "metrics": {},
        "error": None,
    }
    raw = "\n".join(
        [
            json.dumps({"type": "session.started", "sessionId": "grok-94-regression"}),
            json.dumps({"type": "text", "data": "```json\n"}),
            json.dumps({"type": "text", "data": json.dumps(result_payload)}),
            json.dumps({"type": "text", "data": "\n```"}),
            json.dumps(
                {
                    "type": "end",
                    "stopReason": "EndTurn",
                    "sessionId": "grok-94-regression",
                    "usage": {"input_tokens": 200, "output_tokens": 50},
                }
            ),
        ]
    )

    text, usage, session_id = _extract_text_from_grok_jsonl(raw)

    # Result block must be parseable — if this fails the play retries with
    # agent_json_retry (the #94 symptom).
    parsed = parse_skill_result(text)
    assert parsed.success is True
    assert session_id == "grok-94-regression"
    assert usage.tokens_in == 200
    assert usage.tokens_out == 50


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
            timeout=0.01,  # tiny â€” fires immediately on first poll
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

    # received_any stays False â€” no stdout ever arrived.
    activity = _StdoutActivity(last_stdout_at=time.monotonic() - 10.0)
    assert activity.received_any is False

    with pytest.raises(PlayTimeoutError) as excinfo:
        await _watch_stream_idle(
            activity,
            timeout=0.01,  # tiny â€” fires on first poll
            agent_id="agent-silent",
            agent_type="claude_code",
            model_tier="large",
        )
    msg = str(excinfo.value)
    assert "never produced any stdout" in msg
    assert "agent-silent" in msg


# ---------------------------------------------------------------------------
# #176 — missing dispatch cwd (reclaimed worktree) maps to a recoverable error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_cli_missing_cwd_raises_recoverable_not_filenotfound(
    tmp_path: Path,
) -> None:
    """A dispatch whose cwd was reclaimed (TOCTOU) raises AgentProcessCrashed.

    Regression for #176: a missing worktree cwd previously surfaced as a raw
    ``FileNotFoundError`` (an OSError) from ``create_subprocess_exec``, which the
    play executor's recoverable catch tuple does not match — so it logged
    ``unexpected_play_error`` (category ``code_error``). It must instead map to
    the typed, recoverable ``AgentProcessCrashed`` so the play fails cleanly.
    """
    from agentshore.errors import AgentProcessCrashed

    missing = tmp_path / "worktrees" / "pickup-159"  # never created
    assert not missing.exists()

    cfg = AgentConfig(enabled=True, binary="codex", timeout=10)
    handle = _make_handle(agent_type=AgentType.CODEX)

    with pytest.raises(AgentProcessCrashed) as exc_info:
        await dispatch_cli(handle, "prompt", cfg=cfg, cwd_override=missing)

    # Must NOT be a bare FileNotFoundError leaking through.
    assert not isinstance(exc_info.value, FileNotFoundError)
    assert "no longer exists" in str(exc_info.value)
    assert str(missing) in str(exc_info.value)
    assert handle.process is None


@pytest.mark.asyncio
async def test_dispatch_cli_cwd_is_file_raises_recoverable(tmp_path: Path) -> None:
    """A cwd that exists but is a file (not a dir) is also recoverable, not raw."""
    from agentshore.errors import AgentProcessCrashed

    not_a_dir = tmp_path / "cwd_is_a_file"
    not_a_dir.write_text("x", encoding="utf-8")

    cfg = AgentConfig(enabled=True, binary="codex", timeout=10)
    handle = _make_handle(agent_type=AgentType.CODEX)

    with pytest.raises(AgentProcessCrashed):
        await dispatch_cli(handle, "prompt", cfg=cfg, cwd_override=not_a_dir)


# ---------------------------------------------------------------------------
# #177 — launch-to-first-byte watchdog + stream_idle clamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_first_byte_kills_silent_launch() -> None:
    """A child that never produces a first byte is killed at the short deadline."""
    import time

    from agentshore.agents.cli_agent import _StdoutActivity, _watch_first_byte
    from agentshore.errors import ErrorClass, PlayTimeoutError

    activity = _StdoutActivity(last_stdout_at=time.monotonic())
    assert activity.received_any is False

    with pytest.raises(PlayTimeoutError) as excinfo:
        await _watch_first_byte(
            activity,
            deadline=0.05,  # tiny — fires fast
            agent_id="agent-wedged",
            agent_type="codex",
            model_tier="medium",
            prompt_bytes=1234,
        )
    msg = str(excinfo.value)
    assert "never produced first byte" in msg
    assert "launch wedge" in msg
    assert "agent-wedged" in msg
    assert "codex/medium" in msg
    assert excinfo.value.error_class == ErrorClass.TIMEOUT_STREAM_IDLE


@pytest.mark.asyncio
async def test_watch_first_byte_hands_off_after_first_byte() -> None:
    """Once a byte arrives, the first-byte watchdog must NOT fire (idle owns it)."""
    import time

    from agentshore.agents.cli_agent import _StdoutActivity, _watch_first_byte

    activity = _StdoutActivity(last_stdout_at=time.monotonic())
    activity.mark()  # first byte arrived
    assert activity.received_any is True

    # With a byte already received it parks indefinitely; a short wait must time
    # out (i.e. the watchdog did NOT raise its launch-wedge error).
    task = asyncio.create_task(_watch_first_byte(activity, deadline=0.02, agent_id="agent-live"))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_dispatch_cli_first_byte_watchdog_caps_silent_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a silent child is killed at ~first-byte bound, not wall-clock.

    Regression for #177: the wall-clock timeout is large (5s) but the child never
    emits a byte, so the first-byte watchdog (patched tiny) must fire well before
    the wall-clock deadline.
    """
    import time

    import agentshore.agents.cli_agent as ca
    from agentshore.errors import ErrorClass, PlayTimeoutError

    # Child sleeps long without ever writing to stdout.
    script = tmp_path / "silent_launch.py"
    script.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")

    # Tiny first-byte deadline; generous wall-clock + stream-idle so neither of
    # those fires first.
    monkeypatch.setattr(ca, "_FIRST_BYTE_DEADLINE_S", 0.1)
    cfg = AgentConfig(
        enabled=True,
        binary=str(script),
        timeout=5,
        stream_idle_timeout=5,
    )
    handle = _make_handle(agent_type=AgentType.CODEX)

    t0 = time.monotonic()
    with pytest.raises(PlayTimeoutError) as exc_info:
        await dispatch_cli(handle, "prompt", cfg=cfg, python_executable=sys.executable)
    elapsed = time.monotonic() - t0

    assert "never produced first byte" in str(exc_info.value)
    assert exc_info.value.error_class == ErrorClass.TIMEOUT_STREAM_IDLE
    # Fired at the first-byte bound, nowhere near the 5s wall-clock.
    assert elapsed < 2.0
    assert handle.process is None


@pytest.mark.asyncio
async def test_dispatch_cli_clamps_stream_idle_to_wallclock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream_idle_timeout larger than the wall-clock timeout is clamped down.

    Guards against a misconfig disabling early silence detection: with the clamp,
    a silent child still gets killed via the idle watcher at the (clamped) bound
    rather than only at the wall-clock force-kill.
    """
    import agentshore.agents.cli_agent as ca

    # Disable the first-byte watchdog so this test exercises the idle clamp alone.
    monkeypatch.setattr(ca, "_FIRST_BYTE_DEADLINE_S", 10_000.0)

    captured: dict[str, float] = {}
    real_await = ca._await_output_or_timeout

    async def spy_await(*args: Any, **kwargs: Any) -> Any:
        captured["stream_idle_timeout"] = kwargs["stream_idle_timeout"]
        return await real_await(*args, **kwargs)

    monkeypatch.setattr(ca, "_await_output_or_timeout", spy_await)

    script = tmp_path / "quick.py"
    script.write_text(
        "import json,sys\n"
        "sys.stdout.write(json.dumps({'type':'turn.completed',"
        "'usage':{'input_tokens':1,'output_tokens':1}})+'\\n')\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )
    # stream_idle_timeout (100) deliberately exceeds the wall-clock timeout (2).
    cfg = AgentConfig(
        enabled=True,
        binary=str(script),
        timeout=2,
        stream_idle_timeout=100,
    )
    handle = _make_handle(agent_type=AgentType.CODEX)

    await dispatch_cli(handle, "prompt", cfg=cfg, python_executable=sys.executable)

    # Clamped to the wall-clock timeout (2.0), not the configured 100.
    assert captured["stream_idle_timeout"] == 2.0


@pytest.mark.asyncio
async def test_dispatch_arms_both_watchdogs_unconditionally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#177: both the first-byte and stream-idle watchdogs are armed for every
    dispatch, with no play-type or agent-type gating.

    cli_agent.py has no play-type branching, so a generic successful dispatch
    exercises the exact same code path a ``cleanup`` (SkillBackedPlay) dispatch
    takes — both watchers must be created/armed regardless of which play
    requested the work."""
    import agentshore.agents.cli_agent as ca

    real_first_byte = ca._watch_first_byte
    real_stream_idle = ca._watch_stream_idle
    armed: set[str] = set()

    def spy_first_byte(*args: Any, **kwargs: Any) -> Any:
        armed.add("first_byte")
        return real_first_byte(*args, **kwargs)

    def spy_stream_idle(*args: Any, **kwargs: Any) -> Any:
        armed.add("stream_idle")
        return real_stream_idle(*args, **kwargs)

    monkeypatch.setattr(ca, "_watch_first_byte", spy_first_byte)
    monkeypatch.setattr(ca, "_watch_stream_idle", spy_stream_idle)

    script = tmp_path / "quick.py"
    script.write_text(
        "import json,sys\n"
        "sys.stdout.write(json.dumps({'type':'turn.completed',"
        "'usage':{'input_tokens':1,'output_tokens':1}})+'\\n')\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )
    cfg = AgentConfig(enabled=True, binary=str(script), timeout=5, stream_idle_timeout=5)
    handle = _make_handle(agent_type=AgentType.CODEX)

    await dispatch_cli(handle, "prompt", cfg=cfg, python_executable=sys.executable)

    assert armed == {"first_byte", "stream_idle"}
