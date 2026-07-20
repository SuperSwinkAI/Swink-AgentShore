"""Tests for model_discovery: real subprocess spawn against fake CLI binaries.

Mirrors tests/test_auth_probe_spawn.py's approach — drive the real
subprocess.Popen path against a tiny fake executable rather than mocking
subprocess internals, so the timeout tree-kill and parsing are both covered
for real.

POSIX-only: the fake "binary" is a ``chmod +x`` shebang script, which is not
directly executable via ``CreateProcess`` on Windows.
"""

from __future__ import annotations

import json
import sys
import time
from typing import TYPE_CHECKING

import pytest

from agentshore.agents.model_discovery import (
    discover_all,
    discover_antigravity_models,
    discover_codex_models,
    discover_grok_models,
    discover_swink_coding_models,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="fake CLI binary is a POSIX chmod+x shebang script",
)


def _make_fake_cli(tmp_path: Path, name: str, *, body: str) -> str:
    script = tmp_path / name
    script.write_text(f"#!{sys.executable}\nimport sys, time\n{body}\n")
    script.chmod(0o755)
    return str(script)


# ---------------------------------------------------------------------------
# codex
# ---------------------------------------------------------------------------


def test_discover_codex_ok_filters_hidden_models(tmp_path: Path) -> None:
    payload = json.dumps(
        {
            "models": [
                {"slug": "gpt-5.5", "visibility": "list"},
                {"slug": "codex-auto-review", "visibility": "hide"},
                {"slug": "gpt-5.4", "visibility": "list"},
            ]
        }
    )
    binary = _make_fake_cli(tmp_path, "fake-codex", body=f"print({payload!r}); sys.exit(0)")
    result = discover_codex_models(binary=binary)
    assert result.status == "ok"
    assert result.models == ("gpt-5.5", "gpt-5.4")


def test_discover_codex_unparseable_json_is_error(tmp_path: Path) -> None:
    binary = _make_fake_cli(tmp_path, "fake-codex", body="print('not json'); sys.exit(0)")
    result = discover_codex_models(binary=binary)
    assert result.status == "error"
    assert result.models == ()


def test_discover_codex_nonzero_exit_is_error(tmp_path: Path) -> None:
    binary = _make_fake_cli(tmp_path, "fake-codex", body="sys.stderr.write('boom\\n'); sys.exit(1)")
    result = discover_codex_models(binary=binary)
    assert result.status == "error"
    assert "exit 1" in result.detail


def test_discover_codex_missing_binary_is_unavailable(tmp_path: Path) -> None:
    result = discover_codex_models(binary=str(tmp_path / "does-not-exist"))
    assert result.status == "unavailable"
    assert result.models == ()


def test_discover_codex_timeout_returns_promptly_and_tree_kills(tmp_path: Path) -> None:
    binary = _make_fake_cli(tmp_path, "fake-codex", body="time.sleep(30)")
    started = time.monotonic()
    result = discover_codex_models(binary=binary, timeout=0.5)
    elapsed = time.monotonic() - started
    assert result.status == "timeout"
    assert elapsed < 10.0, f"probe blocked for {elapsed:.1f}s instead of tree-killing"


# ---------------------------------------------------------------------------
# grok
# ---------------------------------------------------------------------------


def test_discover_grok_ok_parses_bullets_and_strips_default_marker(tmp_path: Path) -> None:
    body = (
        "print('You are logged in with grok.com.')\n"
        "print()\n"
        "print('Default model: grok-4.5')\n"
        "print()\n"
        "print('Available models:')\n"
        "print('  * grok-4.5 (default)')\n"
        "print('  - grok-composer-2.5-fast')\n"
        "sys.exit(0)"
    )
    binary = _make_fake_cli(tmp_path, "fake-grok", body=body)
    result = discover_grok_models(binary=binary)
    assert result.status == "ok"
    assert result.models == ("grok-4.5", "grok-composer-2.5-fast")


def test_discover_grok_no_parseable_lines_is_error(tmp_path: Path) -> None:
    binary = _make_fake_cli(tmp_path, "fake-grok", body="print('nothing useful'); sys.exit(0)")
    result = discover_grok_models(binary=binary)
    assert result.status == "error"


def test_discover_grok_missing_binary_is_unavailable(tmp_path: Path) -> None:
    result = discover_grok_models(binary=str(tmp_path / "does-not-exist"))
    assert result.status == "unavailable"


# ---------------------------------------------------------------------------
# antigravity
# ---------------------------------------------------------------------------


def test_discover_antigravity_ok_parses_plain_lines(tmp_path: Path) -> None:
    body = (
        "print('Gemini 3.5 Flash (Medium)')\n"
        "print('Gemini 3.1 Pro (High)')\n"
        "print()\n"
        "print('Claude Sonnet 4.6 (Thinking)')\n"
        "sys.exit(0)"
    )
    binary = _make_fake_cli(tmp_path, "fake-agy", body=body)
    result = discover_antigravity_models(binary=binary)
    assert result.status == "ok"
    assert result.models == (
        "Gemini 3.5 Flash (Medium)",
        "Gemini 3.1 Pro (High)",
        "Claude Sonnet 4.6 (Thinking)",
    )


def test_discover_antigravity_empty_output_is_error(tmp_path: Path) -> None:
    binary = _make_fake_cli(tmp_path, "fake-agy", body="sys.exit(0)")
    result = discover_antigravity_models(binary=binary)
    assert result.status == "error"


def test_discover_antigravity_missing_binary_is_unavailable(tmp_path: Path) -> None:
    result = discover_antigravity_models(binary=str(tmp_path / "does-not-exist"))
    assert result.status == "unavailable"


# ---------------------------------------------------------------------------
# swink-coding
# ---------------------------------------------------------------------------


def test_discover_swink_coding_ok_returns_aliases_then_reachable_provider_models(
    tmp_path: Path,
) -> None:
    payload = json.dumps(
        [
            {
                "provider": "ollama",
                "endpoint": "http://localhost:11434",
                "reachable": True,
                "detail": None,
                "models": [
                    {"name": "qwen3.5:4b", "mapped_to": ["small", "medium", "large"]},
                    # Colon-bearing model names must not confuse the
                    # provider:model join — Ollama tags routinely look like this.
                    {"name": "qwen2.5-coder:7b", "mapped_to": ["medium"]},
                ],
            },
            {
                "provider": "vllm",
                "endpoint": "http://vllm.internal:8000",
                "reachable": False,
                "detail": "connection refused",
                "models": None,
            },
        ]
    )
    binary = _make_fake_cli(tmp_path, "fake-swink", body=f"print({payload!r}); sys.exit(0)")
    result = discover_swink_coding_models(binary=binary)
    assert result.status == "ok"
    # Tier aliases lead (always selectable), then reachable concrete models —
    # now dispatchable directly via `provider:model` (SuperSwink-Coding#282/#283).
    assert result.models == (
        "small",
        "medium",
        "large",
        "ollama:qwen3.5:4b",
        "ollama:qwen2.5-coder:7b",
    )
    assert "vllm@http://vllm.internal:8000: unreachable" in result.detail


def test_discover_swink_coding_all_unreachable_is_still_ok_with_aliases_only(
    tmp_path: Path,
) -> None:
    payload = json.dumps(
        [
            {
                "provider": "ollama",
                "endpoint": "http://localhost:11434",
                "reachable": False,
                "detail": "connection refused",
                "models": None,
            },
            {
                "provider": "vllm",
                "endpoint": "http://vllm.internal:8000",
                "reachable": False,
                "detail": "timeout",
                "models": None,
            },
        ]
    )
    binary = _make_fake_cli(tmp_path, "fake-swink", body=f"print({payload!r}); sys.exit(0)")
    result = discover_swink_coding_models(binary=binary)
    # No endpoint reachable is not an error — dispatch via alias still works.
    assert result.status == "ok"
    assert result.models == ("small", "medium", "large")
    assert "ollama@http://localhost:11434: unreachable" in result.detail
    assert "vllm@http://vllm.internal:8000: unreachable" in result.detail


def test_discover_swink_coding_unparseable_json_is_error(tmp_path: Path) -> None:
    binary = _make_fake_cli(tmp_path, "fake-swink", body="print('not json'); sys.exit(0)")
    result = discover_swink_coding_models(binary=binary)
    assert result.status == "error"
    assert result.models == ()


def test_discover_swink_coding_nonzero_exit_is_error(tmp_path: Path) -> None:
    binary = _make_fake_cli(tmp_path, "fake-swink", body="sys.stderr.write('bad\\n'); sys.exit(1)")
    result = discover_swink_coding_models(binary=binary)
    assert result.status == "error"
    assert "exit 1" in result.detail


def test_discover_swink_coding_missing_binary_is_unavailable(tmp_path: Path) -> None:
    result = discover_swink_coding_models(binary=str(tmp_path / "does-not-exist"))
    assert result.status == "unavailable"


# ---------------------------------------------------------------------------
# discover_all
# ---------------------------------------------------------------------------


def test_discover_all_covers_the_free_harnesses_and_excludes_claude() -> None:
    # Real PATH lookup (no fakes) — just verifies shape/keys, not live content,
    # since CI has no guarantee any of these binaries are installed.
    results = discover_all(timeout=1.0)
    assert set(results) == {"codex", "grok", "antigravity", "swink_coding"}
    assert "claude_code" not in results
    for key, result in results.items():
        assert result.agent_key == key
        assert result.status in ("ok", "unavailable", "timeout", "error")
