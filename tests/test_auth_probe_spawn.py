"""Focused tests for the real ``probe_cli_auth`` subprocess spawn path.

The gate-logic tests in ``test_preflight_cli_agent_auth`` mock
``probe_cli_auth``/``probe_configured_cli_auth`` at the function boundary, so
the actual ``subprocess.Popen`` spawn — including the hardened
``creationflags`` and the timeout tree-kill — has no direct coverage. These
tests drive the real spawn against a tiny fake agent binary so the OK /
expired-marker / nonzero / timeout / missing-binary outcomes are all exercised.

POSIX-only: the fake "binary" is a ``chmod +x`` shebang script, which is not
directly executable via ``CreateProcess`` on Windows. The hardening branches
the tests cover (``no_window_creationflags() == 0`` on POSIX, ``kill_tree_sync``
via ``os.kill``) are the POSIX paths; the Windows-specific flags/taskkill are
guarded by ``sys.platform`` in the code under test and exercised on Windows CI.
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING

import pytest

from agentshore.agents.auth_probe import (
    AUTH_ERROR,
    AUTH_EXPIRED,
    AUTH_OK,
    AUTH_TIMEOUT,
    probe_cli_auth,
)
from agentshore.state import AgentType

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="fake agent binary is a POSIX chmod+x shebang script",
)


def _make_fake_agent(tmp_path: Path, *, body: str) -> str:
    """Write an executable fake agent CLI and return its absolute path.

    ``probe_cli_auth`` only probes :data:`AgentType.CODEX` today and appends a
    fixed ``login status`` argv tail; the fake ignores its args and just
    produces the requested stdout/stderr/exit code.
    """
    script = tmp_path / "fake-codex"
    script.write_text(f"#!{sys.executable}\nimport sys\n{body}\n")
    script.chmod(0o755)
    return str(script)


def test_probe_ok(tmp_path: Path) -> None:
    binary = _make_fake_agent(tmp_path, body="print('Logged in as octocat'); sys.exit(0)")
    result = probe_cli_auth(AgentType.CODEX, binary=binary)
    assert result.status == AUTH_OK
    assert result.ok


def test_probe_expired_marker_classifies_regardless_of_exit(tmp_path: Path) -> None:
    binary = _make_fake_agent(
        tmp_path,
        body="sys.stderr.write('Not logged in. Please run `codex login`.\\n'); sys.exit(0)",
    )
    result = probe_cli_auth(AgentType.CODEX, binary=binary)
    assert result.status == AUTH_EXPIRED
    assert result.blocks_launch
    assert "not logged in" in result.detail.lower()


def test_probe_nonzero_without_marker_is_error(tmp_path: Path) -> None:
    binary = _make_fake_agent(
        tmp_path, body="sys.stderr.write('transient network blip\\n'); sys.exit(3)"
    )
    result = probe_cli_auth(AgentType.CODEX, binary=binary)
    assert result.status == AUTH_ERROR
    assert not result.blocks_launch
    assert "exited 3" in result.detail


def test_probe_timeout_returns_promptly_and_tree_kills(tmp_path: Path) -> None:
    binary = _make_fake_agent(tmp_path, body="import time; time.sleep(30)")
    started = time.monotonic()
    result = probe_cli_auth(AgentType.CODEX, binary=binary, timeout=0.5)
    elapsed = time.monotonic() - started
    assert result.status == AUTH_TIMEOUT
    assert not result.blocks_launch
    assert elapsed < 10.0, f"probe blocked for {elapsed:.1f}s instead of tree-killing"


def test_probe_missing_binary_is_error(tmp_path: Path) -> None:
    result = probe_cli_auth(AgentType.CODEX, binary=str(tmp_path / "does-not-exist"))
    assert result.status == AUTH_ERROR
    assert not result.blocks_launch


def test_probe_unprobeable_agent_type_never_spawns() -> None:
    result = probe_cli_auth(AgentType.GROK, binary="/nonexistent/grok")
    assert result.status == "unprobeable"
    assert result.ok


def test_claude_probe_logged_in_is_ok(tmp_path: Path) -> None:
    binary = _make_fake_agent(
        tmp_path, body='print(\'{"loggedIn": true, "authMethod": "claude.ai"}\'); sys.exit(0)'
    )
    result = probe_cli_auth(AgentType.CLAUDE_CODE, binary=binary)
    assert result.status == AUTH_OK


def test_claude_probe_logged_out_blocks_launch(tmp_path: Path) -> None:
    binary = _make_fake_agent(
        tmp_path, body='print(\'{"loggedIn": false, "authMethod": "none"}\'); sys.exit(1)'
    )
    result = probe_cli_auth(AgentType.CLAUDE_CODE, binary=binary)
    assert result.status == AUTH_EXPIRED
    assert result.blocks_launch


@pytest.mark.parametrize(
    ("body"),
    [
        "print('not json'); sys.exit(1)",
        "print('[1, 2]'); sys.exit(0)",
        "print('{\"loggedIn\": false}'); sys.exit(2)",
    ],
)
def test_claude_probe_unparseable_is_nonblocking_error(tmp_path: Path, body: str) -> None:
    binary = _make_fake_agent(tmp_path, body=body)
    result = probe_cli_auth(AgentType.CLAUDE_CODE, binary=binary)
    assert result.status == AUTH_ERROR
    assert not result.blocks_launch


def _make_fake_agy(tmp_path: Path, *, body: str) -> str:
    script = tmp_path / "fake-agy"
    script.write_text(f"#!{sys.executable}\nimport sys, time\n{body}\n")
    script.chmod(0o755)
    return str(script)


def test_agy_probe_ok(tmp_path: Path) -> None:
    binary = _make_fake_agy(tmp_path, body="print('OK'); sys.exit(0)")
    result = probe_cli_auth(AgentType.ANTIGRAVITY, binary=binary)
    assert result.status == AUTH_OK
    assert result.ok


def test_agy_probe_timeout_is_expired_not_timeout(tmp_path: Path) -> None:
    binary = _make_fake_agy(tmp_path, body="time.sleep(30)")
    started = time.monotonic()
    result = probe_cli_auth(AgentType.ANTIGRAVITY, binary=binary, timeout=0.5)
    elapsed = time.monotonic() - started
    assert result.status == AUTH_EXPIRED
    assert result.blocks_launch
    assert elapsed < 10.0, f"probe blocked for {elapsed:.1f}s instead of tree-killing"
    assert "no response" in result.detail.lower()


def test_agy_probe_not_authed_marker_is_expired(tmp_path: Path) -> None:
    binary = _make_fake_agy(
        tmp_path,
        body="sys.stderr.write('You are not logged into Antigravity.\\n'); sys.exit(0)",
    )
    result = probe_cli_auth(AgentType.ANTIGRAVITY, binary=binary)
    assert result.status == AUTH_EXPIRED
    assert result.blocks_launch


def test_agy_probe_empty_output_is_nonblocking_error(tmp_path: Path) -> None:
    binary = _make_fake_agy(tmp_path, body="sys.exit(0)")
    result = probe_cli_auth(AgentType.ANTIGRAVITY, binary=binary)
    assert result.status == AUTH_ERROR
    assert not result.blocks_launch


def test_agy_probe_missing_binary_is_error(tmp_path: Path) -> None:
    result = probe_cli_auth(AgentType.ANTIGRAVITY, binary=str(tmp_path / "does-not-exist"))
    assert result.status == AUTH_ERROR
    assert not result.blocks_launch
