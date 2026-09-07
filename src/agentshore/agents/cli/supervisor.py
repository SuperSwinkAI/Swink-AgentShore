"""Injected subprocess lifecycle boundary for CLI agent dispatches."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from agentshore import subprocess_env
from agentshore.agents.cli import conpty
from agentshore.agents.cli.errors import (
    _classify_error,
    _process_error_detail,
    is_post_response_hook_failure,
)
from agentshore.agents.cli.parsing import _ReadOutput
from agentshore.agents.cli.stream import read_output_guarded
from agentshore.agents.cli.watchdogs import (
    _FIRST_BYTE_DEADLINE_BY_TYPE,
    _FIRST_BYTE_DEADLINE_S,
    _POST_RESPONSE_GRACE_S,
    _ReadOutputFailed,
    _StderrSniffer,
    _StdoutActivity,
    _watch_first_byte,
    _watch_stderr_auth,
    _watch_stream_idle,
)
from agentshore.errors import (
    AgentProcessCrashed,
    AgentProcessError,
    ErrorClass,
    PlayTimeoutError,
)
from agentshore.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.agents.handle import AgentHandle
    from agentshore.config import AgentConfig
    from agentshore.state import AgentType

_logger = get_logger(__name__)
_SIGKILL_GRACE = 10

OutputWaiter = Callable[..., Awaitable[tuple[_ReadOutput, bool]]]
ProcessKiller = Callable[[asyncio.subprocess.Process, str], Awaitable[None]]
PromptFeeder = Callable[
    [asyncio.subprocess.Process, str],
    Coroutine[Any, Any, None],
]
ProcessCloser = Callable[[asyncio.subprocess.Process], None]


async def feed_prompt_stdin(proc: asyncio.subprocess.Process, prompt: str) -> None:
    """Write a Windows-routed prompt to stdin and close the pipe."""
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


def resolve_first_byte_deadline(
    agent_type: AgentType,
    cfg: AgentConfig,
    timeout: float,
    per_dispatch_override: float | None = None,
) -> float:
    """Resolve and wall-clock-clamp the first-byte watchdog deadline."""
    if per_dispatch_override is not None:
        base = float(per_dispatch_override)
    else:
        override = cfg.first_byte_timeout_seconds
        base = (
            float(override)
            if override is not None
            else _FIRST_BYTE_DEADLINE_BY_TYPE.get(agent_type, _FIRST_BYTE_DEADLINE_S)
        )
    return min(base, timeout)


async def await_output_or_timeout(
    proc: asyncio.subprocess.Process,
    handle: AgentHandle,
    *,
    max_bytes: int,
    cfg: AgentConfig,
    stream_idle_timeout: float,
    timeout: float,
    prompt_bytes: int,
    sniffer: _StderrSniffer,
    dispatch_start: float,
    first_byte_timeout_override: float | None = None,
) -> tuple[_ReadOutput, bool]:
    """Race output against auth, first-byte, idle, and wall-clock watchdogs."""
    post_response_killed = False
    stdout_activity = _StdoutActivity(
        last_stdout_at=time.monotonic(),
        dispatch_start=dispatch_start,
    )
    read_task = asyncio.create_task(
        read_output_guarded(
            proc,
            handle.agent_type,
            max_bytes,
            line_limit=cfg.line_limit_bytes,
            agent_id=handle.agent_id,
            stdout_activity=stdout_activity,
        )
    )
    idle_task = asyncio.create_task(
        _watch_stream_idle(
            stdout_activity,
            timeout=stream_idle_timeout,
            agent_id=handle.agent_id,
            agent_type=handle.agent_type.value,
            model_tier=handle.model_tier,
            prompt_bytes=prompt_bytes,
        )
    )
    auth_task = asyncio.create_task(
        _watch_stderr_auth(
            proc,
            sniffer,
            agent_id=handle.agent_id,
            agent_type=handle.agent_type.value,
        )
    )
    first_byte_task = asyncio.create_task(
        _watch_first_byte(
            stdout_activity,
            deadline=resolve_first_byte_deadline(
                handle.agent_type,
                cfg,
                timeout,
                first_byte_timeout_override,
            ),
            agent_id=handle.agent_id,
            agent_type=handle.agent_type.value,
            model_tier=handle.model_tier,
            prompt_bytes=prompt_bytes,
        )
    )
    watcher_tasks = (idle_task, auth_task, first_byte_task)

    done, _pending = await asyncio.wait(
        {read_task, *watcher_tasks},
        timeout=float(timeout),
        return_when=asyncio.FIRST_COMPLETED,
    )
    if not done:
        read_task.cancel()
        for watcher in watcher_tasks:
            watcher.cancel()
        await asyncio.gather(read_task, *watcher_tasks, return_exceptions=True)
        raise PlayTimeoutError(
            (
                f"agent {handle.agent_id!r} ({handle.agent_type.value}/"
                f"{handle.model_tier or '?'}) timed out after {timeout}s "
                f"(prompt_bytes={prompt_bytes})"
            ),
            error_class=ErrorClass.TIMEOUT_WALLCLOCK,
        ) from None

    if auth_task in done and read_task not in done:
        auth_exc = auth_task.exception()
        read_task.cancel()
        for watcher in watcher_tasks:
            if watcher is not auth_task:
                watcher.cancel()
        await asyncio.gather(
            read_task,
            *(watcher for watcher in watcher_tasks if watcher is not auth_task),
            return_exceptions=True,
        )
        raise (
            auth_exc
            if auth_exc is not None
            else AssertionError("stderr-auth watcher completed without raising")
        )

    if read_task in done:
        for watcher in watcher_tasks:
            watcher.cancel()
        await asyncio.gather(*watcher_tasks, return_exceptions=True)
        read_result = await read_task
        if isinstance(read_result, _ReadOutputFailed):
            raise read_result.exc
        return read_result, post_response_killed

    fired_watcher = first_byte_task if first_byte_task in done else idle_task
    for watcher in watcher_tasks:
        if watcher is not fired_watcher:
            watcher.cancel()
    await asyncio.gather(
        *(watcher for watcher in watcher_tasks if watcher is not fired_watcher),
        return_exceptions=True,
    )
    idle_exc = fired_watcher.exception()
    if idle_exc is None:
        read_result = await read_task
    elif (
        isinstance(idle_exc, PlayTimeoutError)
        and idle_exc.error_class == ErrorClass.TIMEOUT_POST_RESPONSE
    ):
        post_response_killed = True
        _logger.info(
            "post_response_process_kill",
            agent_id=handle.agent_id,
            grace_s=_POST_RESPONSE_GRACE_S,
        )
        await kill_process(proc, handle.agent_id)
        try:
            read_result = await asyncio.wait_for(read_task, timeout=5.0)
        except TimeoutError:
            read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)
            raise idle_exc from None
    else:
        grace_s = min(stream_idle_timeout, 0.25)
        try:
            await asyncio.wait_for(asyncio.shield(read_task), timeout=grace_s)
        except TimeoutError:
            read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)
            raise idle_exc from None
        read_result = await read_task

    if isinstance(read_result, _ReadOutputFailed):
        raise read_result.exc
    return read_result, post_response_killed


async def finalize_nonzero_exit(
    proc: asyncio.subprocess.Process,
    handle: AgentHandle,
    *,
    cfg: AgentConfig,
    rc: int,
    raw_output: str,
    sniffer: _StderrSniffer | None = None,
) -> bool:
    """Classify non-zero exits, preserving completed SessionEnd-hook output."""
    stderr_text = sniffer.captured if sniffer is not None else ""
    if not stderr_text and proc.stderr:
        try:
            raw_err = await proc.stderr.read()
            stderr_text = raw_err.decode("utf-8", errors="replace")
        except (OSError, EOFError) as exc:
            _logger.warning(
                "cli_agent_stderr_read_failed",
                agent_id=handle.agent_id,
                error=str(exc),
            )
    if raw_output.strip() and is_post_response_hook_failure(stderr_text):
        _logger.warning(
            "cli_agent_post_response_hook_failure",
            agent_id=handle.agent_id,
            exit_code=rc,
            stderr_tail=stderr_text[:500],
            output_length=len(raw_output),
        )
        return True
    error_class = _classify_error(rc, stderr_text, raw_output)
    handle.last_error_class = error_class
    _logger.warning(
        "cli_agent_nonzero_exit",
        agent_id=handle.agent_id,
        exit_code=rc,
        error_class=error_class,
        stderr_tail=stderr_text[:500],
        stdout_tail=raw_output[-500:] if raw_output else "(empty)",
    )
    detail = _process_error_detail(
        agent_type=handle.agent_type,
        model=handle.model or cfg.model,
        error_class=error_class,
        stderr=stderr_text,
        stdout=raw_output,
    )
    close_process_transport(proc)
    raise AgentProcessError(
        f"agent {handle.agent_id!r} exited with code {rc} [{error_class}]: {detail}"
    )


async def kill_process(proc: asyncio.subprocess.Process, agent_id: str) -> None:
    """Terminate a child process group and bound both graceful and hard reaping."""
    _logger.info(
        "agent_process_terminating",
        agent_id=agent_id,
        pid=proc.pid,
        signal="SIGTERM",
    )
    if not hasattr(os, "killpg"):
        if proc.pid is None:
            close_process_transport(proc)
            return
        subprocess_env.kill_tree_sync(proc.pid)
        try:
            await asyncio.wait_for(proc.wait(), timeout=float(_SIGKILL_GRACE))
        except TimeoutError:
            _logger.warning("sending_sigkill", agent_id=agent_id)
            subprocess_env.kill_tree_sync(proc.pid)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=float(_SIGKILL_GRACE))
        if proc.returncode is None:
            _logger.warning("taskkill_failed", agent_id=agent_id, pid=proc.pid)
        close_process_transport(proc)
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, TypeError):
        close_process_transport(proc)
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        close_process_transport(proc)
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=float(_SIGKILL_GRACE))
    except TimeoutError:
        _logger.warning("sending_sigkill", agent_id=agent_id)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
        try:
            await asyncio.wait_for(proc.wait(), timeout=float(_SIGKILL_GRACE))
        except TimeoutError:
            _logger.warning(
                "subprocess_unreaped_after_sigkill",
                agent_id=agent_id,
                pid=proc.pid,
            )
    finally:
        try:
            os.killpg(pgid, 0)
            ps = await asyncio.create_subprocess_exec(
                "ps",
                "-g",
                str(pgid),
                "-o",
                "pid=",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await ps.communicate()
            survivors = [
                int(line.strip())
                for line in stdout.decode("utf-8", errors="ignore").splitlines()
                if line.strip().isdigit()
            ]
            _logger.warning(
                "subprocess_zombie_detected",
                agent_id=agent_id,
                pgid=pgid,
                survivors=survivors,
            )
        except (ProcessLookupError, PermissionError):
            pass
        close_process_transport(proc)


def close_streams(proc: asyncio.subprocess.Process) -> None:
    """Signal EOF on open pipes so the transport can be collected cleanly."""
    if proc.stdout is not None:
        with contextlib.suppress(Exception):
            proc.stdout.feed_eof()
    if proc.stderr is not None:
        with contextlib.suppress(Exception):
            proc.stderr.feed_eof()


def close_process_transport(proc: asyncio.subprocess.Process) -> None:
    """Close asyncio's private subprocess transport when still open."""
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.close()


@dataclass(frozen=True, slots=True)
class ProcessRunRequest:
    """Everything the supervisor needs to launch and observe one child."""

    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    prompt: str
    prompt_on_stdin: bool
    allow_conpty: bool
    max_bytes: int
    stream_idle_timeout: float
    timeout: float
    prompt_bytes: int
    dispatch_start: float
    first_byte_timeout_override: float | None = None


@dataclass(frozen=True, slots=True)
class SupervisedProcessResult:
    """Process and parsed stream state returned after lifecycle cleanup."""

    process: asyncio.subprocess.Process
    output: _ReadOutput
    post_response_killed: bool
    stderr_sniffer: _StderrSniffer


class ProcessRunner(Protocol):
    """Injectable process boundary consumed by ``dispatch_cli``."""

    async def run(
        self,
        request: ProcessRunRequest,
        *,
        handle: AgentHandle,
        cfg: AgentConfig,
        on_subprocess_spawned: Callable[[int], Awaitable[None]] | None = None,
        on_subprocess_exited: Callable[[int, int | None], Awaitable[None]] | None = None,
    ) -> SupervisedProcessResult: ...


class ProcessSupervisor:
    """Own child spawn, callbacks, timeout cleanup, and handle process state."""

    def __init__(
        self,
        *,
        await_output: OutputWaiter | None = None,
        process_killer: ProcessKiller | None = None,
        feed_prompt: PromptFeeder | None = None,
        stream_closer: ProcessCloser | None = None,
        transport_closer: ProcessCloser | None = None,
    ) -> None:
        self._await_output = await_output or await_output_or_timeout
        self._kill_process = process_killer or kill_process
        self._feed_prompt = feed_prompt or feed_prompt_stdin
        self._close_streams = stream_closer or close_streams
        self._close_transport = transport_closer or close_process_transport

    async def run(
        self,
        request: ProcessRunRequest,
        *,
        handle: AgentHandle,
        cfg: AgentConfig,
        on_subprocess_spawned: Callable[[int], Awaitable[None]] | None = None,
        on_subprocess_exited: Callable[[int, int | None], Awaitable[None]] | None = None,
    ) -> SupervisedProcessResult:
        if not os.path.isdir(request.cwd):
            raise AgentProcessCrashed(
                f"dispatch cwd (worktree) no longer exists: {request.cwd}"
            )

        try:
            if request.allow_conpty and conpty.should_use_conpty(handle.agent_type):
                proc = cast(
                    "asyncio.subprocess.Process",
                    await conpty.spawn(
                        list(request.argv),
                        cwd=str(request.cwd),
                        env=request.env,
                        limit=cfg.line_limit_bytes,
                    ),
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *request.argv,
                    stdin=(
                        asyncio.subprocess.PIPE
                        if request.prompt_on_stdin
                        else asyncio.subprocess.DEVNULL
                    ),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(request.cwd),
                    limit=cfg.line_limit_bytes,
                    start_new_session=True,
                    creationflags=subprocess_env.no_window_creationflags(),
                    env=request.env,
                )
        except NotADirectoryError as exc:
            raise AgentProcessCrashed(
                f"dispatch cwd (worktree) is not a directory: {request.cwd}"
            ) from exc
        except FileNotFoundError as exc:
            raise AgentProcessCrashed(
                f"agent {handle.agent_id!r} executable not found: {request.argv[0]!r}"
            ) from exc

        handle.process = proc
        sniffer = _StderrSniffer()
        stdin_feeder: asyncio.Task[None] | None = None
        if request.prompt_on_stdin and proc.stdin is not None:
            stdin_feeder = asyncio.create_task(self._feed_prompt(proc, request.prompt))
        if on_subprocess_spawned is not None and proc.pid is not None:
            await on_subprocess_spawned(proc.pid)

        try:
            output, post_response_killed = await self._await_output(
                proc,
                handle,
                max_bytes=request.max_bytes,
                cfg=cfg,
                stream_idle_timeout=request.stream_idle_timeout,
                timeout=request.timeout,
                prompt_bytes=request.prompt_bytes,
                sniffer=sniffer,
                dispatch_start=request.dispatch_start,
                first_byte_timeout_override=request.first_byte_timeout_override,
            )
        except TimeoutError:
            await self._kill_process(proc, handle.agent_id)
            self._close_streams(proc)
            self._close_transport(proc)
            raise PlayTimeoutError(
                f"agent {handle.agent_id!r} timed out after {request.timeout:g}s",
                error_class=ErrorClass.TIMEOUT_WALLCLOCK,
            ) from None
        except asyncio.CancelledError:
            _logger.warning(
                "dispatch_cancelled",
                agent_id=handle.agent_id,
                play_id=handle.current_play_id,
            )
            with contextlib.suppress(Exception):
                await self._kill_process(proc, handle.agent_id)
            self._close_streams(proc)
            self._close_transport(proc)
            raise
        except Exception:
            with contextlib.suppress(Exception):
                await self._kill_process(proc, handle.agent_id)
            self._close_streams(proc)
            self._close_transport(proc)
            raise
        finally:
            if stdin_feeder is not None:
                stdin_feeder.cancel()
                with contextlib.suppress(Exception):
                    await stdin_feeder
            if on_subprocess_exited is not None and proc.pid is not None:
                with contextlib.suppress(Exception):
                    await on_subprocess_exited(proc.pid, proc.returncode)
            handle.process = None

        return SupervisedProcessResult(
            process=proc,
            output=output,
            post_response_killed=post_response_killed,
            stderr_sniffer=sniffer,
        )


__all__ = [
    "ProcessRunRequest",
    "ProcessRunner",
    "ProcessSupervisor",
    "SupervisedProcessResult",
]
