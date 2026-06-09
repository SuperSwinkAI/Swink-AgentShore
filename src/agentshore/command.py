"""Async (and sync) subprocess helpers with bounded timeout and cleanup.

``run_command`` is the canonical async runner; ``run_sync_command`` is its
synchronous twin for callers that already run inside a thread pool (e.g. the
``project.inspect`` probe fan-out) or on a synchronous orchestrator boundary
(``core.git_safety``). Both apply the Windows-hardening policy from
:mod:`agentshore.subprocess_env`: absolute-path tool resolution,
``CREATE_NO_WINDOW`` + new process group, a fully non-interactive git/gh
environment, schannel TLS on Windows, and explicit utf-8 decoding.

The thin ``git``/``gh`` (async) and ``git_sync``/``gh_sync`` wrappers are what
call sites should use — they resolve the tool, inject the credential-neutralizing
``-c`` flags / hardened env, and return a structured :class:`CommandResult`
(never raising on timeout or a missing tool, so callers degrade gracefully).
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from agentshore import subprocess_env

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

# Return code used for a tool that timed out, mirroring the historical
# convention in ``sidecar/project.py`` (124 == coreutils ``timeout`` exit).
TIMEOUT_RETURN_CODE = 124
# Return code used for a tool that could not be located on the box.
TOOL_NOT_FOUND_RETURN_CODE = 127


class CommandStatus(StrEnum):
    """Outcome class for a completed (or failed-to-run) subprocess."""

    OK = "ok"
    NONZERO = "nonzero"
    TIMEOUT = "timeout"
    TOOL_NOT_FOUND = "tool_not_found"


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Completed subprocess result with decoded stdio.

    ``status`` and ``elapsed_seconds`` are appended (with defaults) so existing
    construction sites — which pass ``args``/``returncode``/``stdout``/``stderr``
    by keyword — keep working unchanged.
    """

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    status: CommandStatus = CommandStatus.OK
    elapsed_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.status is CommandStatus.OK

    @property
    def timed_out(self) -> bool:
        return self.status is CommandStatus.TIMEOUT

    @property
    def tool_missing(self) -> bool:
        return self.status is CommandStatus.TOOL_NOT_FOUND


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
        self.stdout = stdout.decode("utf-8", errors="replace")
        self.stderr = stderr.decode("utf-8", errors="replace")


def _status_for(returncode: int) -> CommandStatus:
    return CommandStatus.OK if returncode == 0 else CommandStatus.NONZERO


async def run_command(
    *args: str,
    cwd: Path | None = None,
    stdin_data: bytes | None = None,
    timeout_seconds: float = 30.0,
    stdout: int | None = asyncio.subprocess.PIPE,
    stderr: int | None = asyncio.subprocess.PIPE,
    resolve_executable: bool = True,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run a subprocess, killing its tree and awaiting it if the timeout expires."""
    if not args:
        raise ValueError("run_command requires at least one argument")

    executable = shutil.which(args[0]) if resolve_executable else args[0]
    if executable is None:
        raise FileNotFoundError(args[0])

    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        executable,
        *args[1:],
        cwd=str(cwd) if cwd is not None else None,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        env=dict(env) if env is not None else None,
        # CREATE_NO_WINDOW + new process group on Windows (0 elsewhere). POSIX
        # session/group handling is left at the default so macOS/Linux process
        # behavior is unchanged; the timeout path kills the child directly.
        creationflags=subprocess_env.no_window_creationflags(),
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=stdin_data),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        # Kill the whole tree (git → credential helper / ssh), not just the
        # direct child, so nothing lingers holding a lock.
        if proc.pid is not None:
            subprocess_env.kill_tree_sync(proc.pid)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        killed_stdout, killed_stderr = await proc.communicate()
        raise CommandTimeoutError(
            args,
            timeout_seconds,
            stdout=killed_stdout or b"",
            stderr=killed_stderr or b"",
        ) from exc

    returncode = proc.returncode or 0
    return CommandResult(
        args=args,
        returncode=returncode,
        stdout=(stdout_bytes or b"").decode("utf-8", errors="replace"),
        stderr=(stderr_bytes or b"").decode("utf-8", errors="replace"),
        status=_status_for(returncode),
        elapsed_seconds=time.monotonic() - started,
    )


def run_sync_command(
    *args: str,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout_seconds: float = 30.0,
    resolve_executable: bool = True,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Synchronous twin of :func:`run_command` for thread-pool / sync callers."""
    if not args:
        raise ValueError("run_sync_command requires at least one argument")

    executable = shutil.which(args[0]) if resolve_executable else args[0]
    if executable is None:
        raise FileNotFoundError(args[0])

    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 — resolved exe, no shell
            [executable, *args[1:]],
            cwd=str(cwd) if cwd is not None else None,
            input=input_text.encode("utf-8") if input_text is not None else None,
            capture_output=True,
            timeout=timeout_seconds,
            env=dict(env) if env is not None else None,
            check=False,
            creationflags=subprocess_env.no_window_creationflags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandTimeoutError(
            tuple(args),
            timeout_seconds,
            stdout=_as_bytes(exc.stdout),
            stderr=_as_bytes(exc.stderr),
        ) from exc

    returncode = completed.returncode or 0
    return CommandResult(
        args=tuple(args),
        returncode=returncode,
        stdout=_as_bytes(completed.stdout).decode("utf-8", errors="replace"),
        stderr=_as_bytes(completed.stderr).decode("utf-8", errors="replace"),
        status=_status_for(returncode),
        elapsed_seconds=time.monotonic() - started,
    )


def _as_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace")
    return b""


def _tool_missing_result(argv: Sequence[str]) -> CommandResult:
    tool = argv[0] if argv else "<tool>"
    return CommandResult(
        args=tuple(argv),
        returncode=TOOL_NOT_FOUND_RETURN_CODE,
        stdout="",
        stderr=f"{tool} not found on PATH",
        status=CommandStatus.TOOL_NOT_FOUND,
    )


def _timeout_result(argv: Sequence[str], exc: CommandTimeoutError) -> CommandResult:
    return CommandResult(
        args=tuple(argv),
        returncode=TIMEOUT_RETURN_CODE,
        stdout=exc.stdout,
        stderr=exc.stderr or str(exc),
        status=CommandStatus.TIMEOUT,
    )


async def git(
    *args: str,
    cwd: Path | None = None,
    op_class: str = "git.read",
    env_overlay: Mapping[str, str] | None = None,
    timeout_seconds: float | None = None,
) -> CommandResult:
    """Run git through the hardened layer. Never raises; returns a CommandResult."""
    exe = subprocess_env.resolve_tool("git")
    if exe is None:
        return _tool_missing_result(("git", *args))
    timeout = (
        timeout_seconds if timeout_seconds is not None else subprocess_env.timeout_for(op_class)
    )
    full = (exe, *subprocess_env.git_global_args(), *args)
    try:
        return await run_command(
            *full,
            cwd=cwd,
            timeout_seconds=timeout,
            resolve_executable=False,
            env=subprocess_env.hardened_env(env_overlay, for_git=True),
        )
    except CommandTimeoutError as exc:
        return _timeout_result(full, exc)


async def gh(
    *args: str,
    cwd: Path | None = None,
    op_class: str = "gh",
    env_overlay: Mapping[str, str] | None = None,
    timeout_seconds: float | None = None,
) -> CommandResult:
    """Run gh through the hardened layer. Never raises; returns a CommandResult."""
    exe = subprocess_env.resolve_tool("gh")
    if exe is None:
        return _tool_missing_result(("gh", *args))
    timeout = (
        timeout_seconds if timeout_seconds is not None else subprocess_env.timeout_for(op_class)
    )
    full = (exe, *args)
    try:
        return await run_command(
            *full,
            cwd=cwd,
            timeout_seconds=timeout,
            resolve_executable=False,
            env=subprocess_env.hardened_env(env_overlay, for_gh=True),
        )
    except CommandTimeoutError as exc:
        return _timeout_result(full, exc)


def git_sync(
    *args: str,
    cwd: Path | None = None,
    op_class: str = "git.read",
    env_overlay: Mapping[str, str] | None = None,
    timeout_seconds: float | None = None,
) -> CommandResult:
    """Synchronous git through the hardened layer. Never raises."""
    exe = subprocess_env.resolve_tool("git")
    if exe is None:
        return _tool_missing_result(("git", *args))
    timeout = (
        timeout_seconds if timeout_seconds is not None else subprocess_env.timeout_for(op_class)
    )
    full = (exe, *subprocess_env.git_global_args(), *args)
    try:
        return run_sync_command(
            *full,
            cwd=cwd,
            timeout_seconds=timeout,
            resolve_executable=False,
            env=subprocess_env.hardened_env(env_overlay, for_git=True),
        )
    except CommandTimeoutError as exc:
        return _timeout_result(full, exc)


def gh_sync(
    *args: str,
    cwd: Path | None = None,
    op_class: str = "gh",
    env_overlay: Mapping[str, str] | None = None,
    input_text: str | None = None,
    timeout_seconds: float | None = None,
) -> CommandResult:
    """Synchronous gh through the hardened layer. Never raises."""
    exe = subprocess_env.resolve_tool("gh")
    if exe is None:
        return _tool_missing_result(("gh", *args))
    timeout = (
        timeout_seconds if timeout_seconds is not None else subprocess_env.timeout_for(op_class)
    )
    full = (exe, *args)
    try:
        return run_sync_command(
            *full,
            cwd=cwd,
            input_text=input_text,
            timeout_seconds=timeout,
            resolve_executable=False,
            env=subprocess_env.hardened_env(env_overlay, for_gh=True),
        )
    except CommandTimeoutError as exc:
        return _timeout_result(full, exc)
