"""Async subprocess helpers with bounded timeout and cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Completed subprocess result with decoded stdio."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandTimeoutError(TimeoutError):
    """Raised after a subprocess is killed for exceeding its timeout."""

    def __init__(
        self,
        args: tuple[str, ...],
        timeout_seconds: float,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        super().__init__(f"{' '.join(args)} timed out after {timeout_seconds:g}s")
        self.args_tuple = args
        self.timeout_seconds = timeout_seconds
        self.stdout = stdout.decode(errors="replace")
        self.stderr = stderr.decode(errors="replace")


async def run_command(
    *args: str,
    cwd: Path,
    stdin_data: bytes | None = None,
    timeout_seconds: float = 30.0,
    stdout: int | None = asyncio.subprocess.PIPE,
    stderr: int | None = asyncio.subprocess.PIPE,
    resolve_executable: bool = True,
) -> CommandResult:
    """Run a subprocess, killing and awaiting it if the timeout expires."""
    if not args:
        raise ValueError("run_command requires at least one argument")

    executable = shutil.which(args[0]) if resolve_executable else args[0]
    if executable is None:
        raise FileNotFoundError(args[0])

    proc = await asyncio.create_subprocess_exec(
        executable,
        *args[1:],
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=stdin_data),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        killed_stdout, killed_stderr = await proc.communicate()
        raise CommandTimeoutError(
            args,
            timeout_seconds,
            stdout=killed_stdout or b"",
            stderr=killed_stderr or b"",
        ) from exc

    return CommandResult(
        args=args,
        returncode=proc.returncode or 0,
        stdout=(stdout_bytes or b"").decode(errors="replace"),
        stderr=(stderr_bytes or b"").decode(errors="replace"),
    )
