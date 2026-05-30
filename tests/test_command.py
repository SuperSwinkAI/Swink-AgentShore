"""Tests for bounded async subprocess helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentshore.command import CommandTimeoutError, run_command


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
