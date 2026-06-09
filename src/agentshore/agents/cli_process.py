"""Platform-specific helpers for CLI agent subprocesses."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
from typing import Protocol


class _Logger(Protocol):
    def warning(self, event: str, **fields: object) -> None: ...


def no_window_creationflags() -> int:
    """Return CREATE_NO_WINDOW on Windows, otherwise zero."""
    if sys.platform == "win32":
        import subprocess

        return subprocess.CREATE_NO_WINDOW
    return 0


def resolve_executable(argv: list[str]) -> list[str]:
    """On Windows, resolve argv[0] to its full path so .cmd/.bat shims run."""
    if sys.platform != "win32" or not argv or os.path.isabs(argv[0]):
        return argv
    resolved = shutil.which(argv[0])
    if resolved is None:
        return argv
    return [resolved, *argv[1:]]


def prompt_on_stdin(python_executable: str | None) -> bool:
    """Return true when Windows npm shims should receive the prompt over stdin."""
    return python_executable is None and sys.platform == "win32"


async def feed_prompt_stdin(proc: asyncio.subprocess.Process, prompt: str) -> None:
    """Write *prompt* to the child's stdin and close it."""
    stdin = proc.stdin
    if stdin is None:
        return
    try:
        stdin.write(prompt.encode("utf-8"))
        await stdin.drain()
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            stdin.close()


async def kill_process_windows(
    proc: asyncio.subprocess.Process,
    agent_id: str,
    *,
    sigkill_grace: float,
    logger: _Logger,
    close_transport: object,
) -> None:
    """Terminate a process tree on Windows via ``taskkill``."""
    if proc.pid is None:
        _call_close_transport(close_transport, proc)
        return

    async def taskkill(*, force: bool) -> int:
        args = ["taskkill", "/PID", str(proc.pid), "/T"]
        if force:
            args.append("/F")
        tk = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await tk.wait()
        return tk.returncode if tk.returncode is not None else 0

    returncode = await taskkill(force=False)
    try:
        await asyncio.wait_for(proc.wait(), timeout=float(sigkill_grace))
    except TimeoutError:
        logger.warning("sending_sigkill", agent_id=agent_id)
        returncode = await taskkill(force=True)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=float(sigkill_grace))

    if returncode != 0 and proc.returncode is None:
        logger.warning(
            "taskkill_failed",
            agent_id=agent_id,
            pid=proc.pid,
            returncode=returncode,
        )
    _call_close_transport(close_transport, proc)


def _call_close_transport(close_transport: object, proc: asyncio.subprocess.Process) -> None:
    if callable(close_transport):
        close_transport(proc)
