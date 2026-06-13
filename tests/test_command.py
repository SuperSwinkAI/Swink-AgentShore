"""Tests for bounded async subprocess helpers."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agentshore import command, subprocess_env
from agentshore.command import (
    TIMEOUT_RETURN_CODE,
    TOOL_NOT_FOUND_RETURN_CODE,
    CommandResult,
    CommandStatus,
    CommandTimeoutError,
    git,
    git_sync,
    run_command,
)


@pytest.mark.asyncio
async def test_run_command_captures_stdout(tmp_path: Path) -> None:
    result = await run_command(
        sys.executable,
        "-c",
        "print('ok')",
        cwd=tmp_path,
        timeout_seconds=5,
        resolve_executable=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "ok"
    assert result.status is CommandStatus.OK


@pytest.mark.asyncio
async def test_run_command_kills_on_timeout(tmp_path: Path) -> None:
    with pytest.raises(CommandTimeoutError):
        await run_command(
            sys.executable,
            "-c",
            "import time; time.sleep(10)",
            cwd=tmp_path,
            timeout_seconds=0.01,
            resolve_executable=False,
        )


@pytest.mark.asyncio
async def test_run_command_passes_env_overlay(tmp_path: Path) -> None:
    env = {**os.environ, "AGENTSHORE_TEST_VAR": "from-env"}
    result = await run_command(
        sys.executable,
        "-c",
        "import os; print(os.environ.get('AGENTSHORE_TEST_VAR', ''))",
        cwd=tmp_path,
        timeout_seconds=5,
        resolve_executable=False,
        env=env,
    )
    assert result.stdout.strip() == "from-env"


@pytest.mark.asyncio
async def test_git_returns_tool_not_found_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess_env, "resolve_tool", lambda _name: None)
    result = await git("rev-parse", "HEAD")
    assert result.status is CommandStatus.TOOL_NOT_FOUND
    assert result.returncode == TOOL_NOT_FOUND_RETURN_CODE


@pytest.mark.asyncio
async def test_git_injects_credential_neutralizing_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, tuple[str, ...]] = {}

    async def fake_run(*args: str, **_kwargs: object) -> CommandResult:
        captured["args"] = args
        return CommandResult(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess_env, "resolve_tool", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(command, "run_command", fake_run)
    await git("status", "--porcelain")
    args = captured["args"]
    assert args[0] == "/usr/bin/git"
    joined = " ".join(args)
    assert "credential.helper=" in joined
    assert "status" in args
    assert "--porcelain" in args


@pytest.mark.asyncio
async def test_git_converts_timeout_to_structured_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run(*args: str, **_kwargs: object) -> CommandResult:
        raise CommandTimeoutError(args, 1.0, stderr=b"slow")

    monkeypatch.setattr(subprocess_env, "resolve_tool", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(command, "run_command", fake_run)
    result = await git("fetch", op_class="git.network")
    assert result.status is CommandStatus.TIMEOUT
    assert result.returncode == TIMEOUT_RETURN_CODE


def test_git_sync_returns_tool_not_found_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess_env, "resolve_tool", lambda _name: None)
    result = git_sync("rev-parse", "HEAD", cwd=Path.cwd())
    assert result.status is CommandStatus.TOOL_NOT_FOUND


def test_run_sync_command_never_inherits_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A git/gh child must never inherit the parent's stdin.

    In the desktop sidecar that stdin is the live Tauri JSON-RPC pipe; git's
    MSYS2 runtime wedges at 0 CPU probing it. ``run_sync_command`` must pass
    ``stdin=DEVNULL`` when it is not feeding ``input_text`` (regression guard for
    the Windows worktree/git_safety/bootstrap hang).
    """
    import subprocess

    captured: dict[str, object] = {}

    def fake_run(_argv: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured.clear()
        captured.update(kwargs)
        return subprocess.CompletedProcess([], 0, b"", b"")

    monkeypatch.setattr(command.subprocess, "run", fake_run)

    command.run_sync_command("git", "status", resolve_executable=False)
    assert captured.get("stdin") is subprocess.DEVNULL

    # When input_text is supplied, subprocess.run wires stdin itself via input=.
    command.run_sync_command(
        "git", "hash-object", "--stdin", input_text="x", resolve_executable=False
    )
    assert captured.get("stdin") is None
    assert captured.get("input") == b"x"
