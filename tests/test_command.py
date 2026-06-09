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
