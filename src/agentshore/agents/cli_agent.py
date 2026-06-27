"""CLI agent adapter — asyncio subprocess dispatch for supported CLI agents.

Driver module: owns ``dispatch_cli``, ``_read_output``, ``_kill_process``,
``_await_output_or_timeout``, ``_resolve_first_byte_deadline``, and the
handful of process-teardown helpers that must live here for the monkeypatch
constraint (tests patch these names on ``agentshore.agents.cli_agent``).
Pure concern groups live in the ``agents/cli/`` sub-package:

    agents/cli/errors.py    — marker tables + _classify_error / _process_error_detail
    agents/cli/argv.py      — build_argv / build_resume_argv + platform helpers
    agents/cli/watchdogs.py — _StdoutActivity / _StderrSniffer / _watch_* coroutines
    agents/cli/parsing.py   — _PARSERS / CliOutputFormat / _extract_* / _ReadOutput
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from typing import TYPE_CHECKING, cast

from agentshore import subprocess_env
from agentshore.agents import cli_antigravity
from agentshore.agents.cli import conpty
from agentshore.agents.cli.argv import (
    _DEFAULT_YOLO_FLAGS,
    _RESUMABLE_AGENT_TYPES,
    _apply_yolo_default,
    _prompt_on_stdin,
    _resolve_executable,
    _write_grok_prompt_file,
    build_argv,
    build_resume_argv,
)
from agentshore.agents.cli.errors import (
    _AUTH_PATTERNS,
    _AUTH_STDOUT,
    _CACHE_RENEWAL_MARKERS,
    _CODEX_ROLLOUT_PATTERNS,
    _ENOSPC_PATTERNS,
    _INVALID_MODEL_PATTERNS,
    _INVALID_MODEL_STDOUT,
    _OOM_PATTERNS,
    _PARSE_EOF_MARKERS,
    _RATE_LIMIT_PATTERNS,
    _RATE_LIMIT_STDOUT,
    _STDIN_CLOSED_AFTER_CACHE_RENEWAL_MARKERS,
    _TIMEOUT_PATTERNS,
    _TIMEOUT_STDOUT,
    _TRANSIENT_NETWORK_PATTERNS,
    _classify_error,
    _clean_stderr,
    _extract_cli_report_path,
    _is_cache_renewal_stdin_hang,
    _is_transient_cache_blip,
    _process_error_detail,
    is_post_response_hook_failure,
)
from agentshore.agents.cli.parsing import (
    _PARSERS,
    _TERMINAL_EVENT_TYPES,
    CliOutputFormat,
    _extract_session_id_from_jsonl,
    _extract_text_from_codex_jsonl,
    _extract_text_from_grok_jsonl,
    _extract_text_from_stream_json,
    _extract_text_value,
    _FunctionFormat,
    _is_terminal_event,
    _maybe_parse_usage,
    _parse_claude_output,
    _ReadOutput,
)
from agentshore.agents.cli.watchdogs import (
    _FIRST_BYTE_DEADLINE_BY_TYPE,
    _FIRST_BYTE_DEADLINE_S,
    _NEVER_S,
    _POST_RESPONSE_GRACE_S,
    _DispatchArgv,
    _ReadOutputFailed,
    _StderrSniffer,
    _StdoutActivity,
    _watch_first_byte,
    _watch_stderr_auth,
    _watch_stream_idle,
)
from agentshore.agents.costs import estimate_cost
from agentshore.agents.handle import AgentInvocationResult
from agentshore.agents.pricing import PricingQuote, default_quote
from agentshore.errors import (
    AgentOutputInvalid,
    AgentProcessCrashed,
    AgentProcessError,
    ErrorClass,
    PlayTimeoutError,
)
from agentshore.logging import get_logger
from agentshore.state import AgentType

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from agentshore.agents.handle import AgentHandle
    from agentshore.config import AgentConfig

_logger = get_logger(__name__)


_DEFAULT_TIMEOUT = 10800  # seconds (3h) — max-runtime wall-clock backstop when
# neither AgentConfig.timeout nor a resolved default_timeout is supplied. The
# primary kill is the silence-based stream_idle watchdog (default 1800s); this
# only bounds an agent that streams output for the full duration without
# finishing. See AgentManager.dispatch / RuntimeConfig.agent_timeout.
_SIGKILL_GRACE = 10  # seconds between SIGTERM and SIGKILL
_LINE_DRIFT_WARN_BYTES = 1_048_576  # warn once if any single line exceeds 1MB
_ARGV_PREVIEW_MAX_CHARS = 256  # log clamp; full prompt is reconstructible from skill+params


# ---------------------------------------------------------------------------
# Process I/O helpers (must stay in this module — monkeypatched by tests)
# ---------------------------------------------------------------------------


async def _feed_prompt_stdin(proc: asyncio.subprocess.Process, prompt: str) -> None:
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


# ---------------------------------------------------------------------------
# Core dispatch helper
# ---------------------------------------------------------------------------


def _build_dispatch_argv(
    handle: AgentHandle,
    prompt: str,
    *,
    cfg: AgentConfig,
    python_executable: str | None,
    resume_session_id: str | None,
    effective_cwd: Path,
    prompt_file: str | None = None,
) -> _DispatchArgv:
    """Build the subprocess argv list and log-preview fields for a single dispatch.

    Encapsulates the test-shim path (``python_executable``), the normal
    ``build_argv`` path, and the narrow JSON-retry ``--resume`` override
    (desktop-dy2j). *prompt_file*, when set, routes a Grok dispatch's prompt
    through ``--prompt-file`` instead of an argv element (issue #160).
    """
    prompt_on_stdin = _prompt_on_stdin(python_executable)
    if python_executable is not None:
        # Test shim: invoke cfg.binary as a Python script.
        argv: list[str] = [python_executable, cfg.binary or ""]
        # A resuming shim dispatch keeps the minimal claude-style --resume shape
        # (the shim is a python mock, not a real CLI — only the flag's presence
        # matters to the tests that exercise this path).
        if resume_session_id is not None and handle.agent_type == AgentType.CLAUDE_CODE:
            argv = [
                argv[0],
                "--resume",
                resume_session_id,
                "-p",
                "--verbose",
                "--output-format",
                "stream-json",
            ]
            if not prompt_on_stdin:
                argv.append(prompt)
    elif resume_session_id is not None and handle.agent_type in _RESUMABLE_AGENT_TYPES:
        # desktop-dy2j: narrow JSON-retry re-entry of the prior session so the
        # agent emits the structured trailer it missed. Per-agent resume shape.
        argv = build_resume_argv(
            handle.agent_type,
            prompt,
            resume_session_id,
            binary=cfg.binary,
            model=handle.model or cfg.model,
            reasoning_effort=handle.reasoning_effort or cfg.reasoning_effort,
            extra_flags=cfg.extra_flags,
            project_dir=str(effective_cwd),
            prompt_on_stdin=prompt_on_stdin,
            prompt_file=prompt_file,
        )
    else:
        argv = build_argv(
            handle.agent_type,
            prompt,
            binary=cfg.binary,
            model=handle.model or cfg.model,
            reasoning_effort=handle.reasoning_effort or cfg.reasoning_effort,
            extra_flags=cfg.extra_flags,
            project_dir=str(effective_cwd),
            prompt_on_stdin=prompt_on_stdin,
            prompt_file=prompt_file,
        )

    prompt_bytes = len(prompt.encode("utf-8"))

    # Clamp argv_preview: the last argv element is the full skill prompt (~7 KB);
    # logging it every dispatch bloats the log ~1 MB/session. Reconstructible from
    # skill template + PlayParams, so keep only the leading flags.
    argv_str = " ".join(argv[:10])
    if len(argv_str) > _ARGV_PREVIEW_MAX_CHARS:
        truncated = len(argv_str) - _ARGV_PREVIEW_MAX_CHARS
        argv_str = argv_str[:_ARGV_PREVIEW_MAX_CHARS] + f"…(+{truncated} chars truncated)"

    return _DispatchArgv(argv=argv, prompt_bytes=prompt_bytes, argv_str=argv_str)


# ---------------------------------------------------------------------------
# Internal helpers (must stay co-located with dispatch_cli for monkeypatching)
# ---------------------------------------------------------------------------


async def _read_output(
    proc: asyncio.subprocess.Process,
    agent_type: AgentType,
    max_bytes: int,
    *,
    line_limit: int,
    agent_id: str,
    stdout_activity: _StdoutActivity | None = None,
) -> _ReadOutput:
    """Stream stdout, accumulate output, extract token metadata.

    Returns raw text, billable token buckets, lightweight turn metrics, and
    the observed CLI session id.
    Raises ``AgentOutputInvalid`` if the output exceeds *max_bytes* or if a
    single line exceeds *line_limit* (the asyncio readline buffer cap).
    """
    from agentshore.agents._jsonl import _UsageTotals

    if proc.stdout is None:
        msg = "Subprocess stdout is None after create_subprocess_exec"
        raise RuntimeError(msg)
    chunks: list[bytes] = []
    total_bytes = 0
    drift_warned = False

    try:
        async for line in proc.stdout:
            # First stdout byte for this dispatch (#212). ``mark()`` returns True
            # only on the first line; emitting here (at the transition, not dispatch
            # end) keeps the event faithful to real first-byte time so monitors can
            # tell "slow to start" from "never started". ``dispatch_start`` is 0.0
            # only without a dispatch context (unit tests) — the guard suppresses a
            # garbage elapsed there.
            if (
                stdout_activity is not None
                and stdout_activity.mark()
                and stdout_activity.dispatch_start
                and stdout_activity.first_byte_at
            ):
                _logger.info(
                    "cli_first_byte",
                    agent_id=agent_id,
                    agent_type=str(agent_type),
                    elapsed_ms=int(
                        (stdout_activity.first_byte_at - stdout_activity.dispatch_start) * 1000
                    ),
                )
            total_bytes += len(line)
            if total_bytes > max_bytes:
                raise AgentOutputInvalid(
                    f"agent output exceeded {max_bytes} bytes (max_output_size)"
                )
            if not drift_warned and len(line) >= _LINE_DRIFT_WARN_BYTES:
                drift_warned = True
                _logger.warning(
                    "cli_agent_large_line",
                    agent_id=agent_id,
                    agent_type=str(agent_type),
                    line_bytes=len(line),
                    line_limit=line_limit,
                )
            chunks.append(line)

            if (
                stdout_activity is not None
                and not stdout_activity.response_complete
                and _is_terminal_event(line, agent_type)
            ):
                stdout_activity.mark_response_complete()
    except asyncio.LimitOverrunError as exc:
        raise AgentOutputInvalid(
            f"agent {agent_id!r} stream-json line exceeded {line_limit} bytes "
            f"(consumed={exc.consumed}); raise agents.<name>.line_limit_bytes "
            f"in agentshore.yaml"
        ) from exc
    except ValueError as exc:
        # StreamReader.readline() catches LimitOverrunError internally and
        # re-raises as a bare ValueError on Python 3.12+. Detect the chunk-
        # overflow signature and surface a structured error; otherwise re-raise.
        msg = str(exc)
        if "chunk" in msg and "limit" in msg:
            raise AgentOutputInvalid(
                f"agent {agent_id!r} stream-json line exceeded {line_limit} bytes; "
                f"raise agents.<name>.line_limit_bytes in agentshore.yaml"
            ) from exc
        raise

    raw = b"".join(chunks).decode("utf-8", errors="replace")

    parser = _PARSERS.get(agent_type)
    if parser is not None:
        raw, usage, session_id = parser.parse(raw)
    else:
        usage = _UsageTotals()
        session_id = None

    await proc.wait()
    return _ReadOutput(raw=raw, usage=usage, session_id=session_id)


async def _read_output_guarded(
    proc: asyncio.subprocess.Process,
    agent_type: AgentType,
    max_bytes: int,
    *,
    line_limit: int,
    agent_id: str,
    stdout_activity: _StdoutActivity,
) -> _ReadOutput | _ReadOutputFailed:
    try:
        return await _read_output(
            proc,
            agent_type,
            max_bytes,
            line_limit=line_limit,
            agent_id=agent_id,
            stdout_activity=stdout_activity,
        )
    except BaseException as exc:
        return _ReadOutputFailed(exc)


def _resolve_first_byte_deadline(
    agent_type: AgentType,
    cfg: AgentConfig,
    timeout: float,
    per_dispatch_override: float | None = None,
) -> float:
    """Resolve the armed first-byte deadline for one dispatch.

    Precedence: an explicit ``per_dispatch_override`` (a one-off short budget for
    a single call, e.g. the no-JSON resume-retry, #232), then the per-agent
    ``first_byte_timeout_seconds`` config override, then the per-agent-type
    built-in default, then the global ``_FIRST_BYTE_DEADLINE_S``. Always clamped
    to the wall-clock ``timeout`` so it never outlives the dispatch.

    References ``_FIRST_BYTE_DEADLINE_S`` and ``_FIRST_BYTE_DEADLINE_BY_TYPE`` as
    module-level globals so tests that monkeypatch ``ca._FIRST_BYTE_DEADLINE_S``
    observe the patched value on every call.
    """
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


async def _await_output_or_timeout(
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
    """Drive the read/idle/stderr-auth race and return ``(result, post_response_killed)``.

    Called inside ``dispatch_cli``'s outer ``try:`` block so that the caller's
    ``except``/``finally`` clauses remain responsible for process cleanup on
    error.  Raises ``PlayTimeoutError`` directly on wall-clock or idle expiry;
    re-raises ``_ReadOutputFailed.exc`` on output-parse errors. The
    ``stderr``-auth watcher raises ``PlayTimeoutError(error_class=AUTH)`` as soon
    as a backend session-token expiry signature lands on stderr, so a stdin-hang
    is killed in well under a second rather than after the full idle window.

    All watchdog coroutines and ``_read_output_guarded``/``_kill_process`` are
    referenced as module-level names so tests that monkeypatch
    ``agentshore.agents.cli_agent._watch_first_byte`` etc. observe the patch.
    """
    post_response_killed = False
    stdout_activity = _StdoutActivity(
        last_stdout_at=time.monotonic(), dispatch_start=dispatch_start
    )
    read_task = asyncio.create_task(
        _read_output_guarded(
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
    # Launch-to-first-byte watchdog (#177). Caps a silent-launch wedge at
    # ``_FIRST_BYTE_DEADLINE_S`` (~2 min) instead of the full wall-clock
    # ``timeout``. Clamped to ``timeout`` so it never outlives the dispatch.
    #
    # Both watchdogs below are armed unconditionally for EVERY dispatch — there
    # is no play-type or agent-type branching on this path, so ``cleanup`` (a
    # SkillBackedPlay, same path as ``issue_pickup``) is covered identically.
    #
    # Residual gap (#177): these watchdogs only see *byte arrival* via
    # ``_StdoutActivity`` (last_stdout_at / received_any). Model-API-call /
    # turn-count usage is parsed only AFTER the read loop drains (see
    # ``_read_output`` → ``parser.parse``), so a child that streams stdout NOISE
    # while making ZERO model calls keeps ``received_any`` True and ``last_stdout_at``
    # fresh, defeating both byte-watchdogs. No live call counter is exposed at
    # this layer; closing this residual needs a higher-layer (per-call/cost)
    # signal — do NOT plumb a new counter here.
    first_byte_task = asyncio.create_task(
        _watch_first_byte(
            stdout_activity,
            deadline=_resolve_first_byte_deadline(
                handle.agent_type, cfg, timeout, first_byte_timeout_override
            ),
            agent_id=handle.agent_id,
            agent_type=handle.agent_type.value,
            model_tier=handle.model_tier,
            prompt_bytes=prompt_bytes,
        )
    )
    # All non-read watchers, for uniform cancellation on every exit path.
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
        # desktop-awc: enrich the wall-clock timeout error with the
        # agent shape so operators can spot whether timeouts correlate
        # with model tier or huge prompts without having to rejoin the
        # play_id back to the agents table by hand.
        raise PlayTimeoutError(
            (
                f"agent {handle.agent_id!r} ({handle.agent_type.value}/"
                f"{handle.model_tier or '?'}) timed out after {timeout}s "
                f"(prompt_bytes={prompt_bytes})"
            ),
            error_class=ErrorClass.TIMEOUT_WALLCLOCK,
        ) from None
    # The stderr-auth watcher fired before stdout/idle resolved: kill the hung
    # process and surface the AUTH classification immediately. The watcher only
    # ever completes by raising ``PlayTimeoutError(AUTH)`` (post-EOF it sleeps
    # indefinitely), so a done auth_task that beat the read always carries an
    # exception to re-raise.
    if auth_task in done and read_task not in done:
        auth_exc = auth_task.exception()
        read_task.cancel()
        for watcher in watcher_tasks:
            if watcher is not auth_task:
                watcher.cancel()
        await asyncio.gather(
            read_task,
            *(w for w in watcher_tasks if w is not auth_task),
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
    else:
        # A watchdog fired before the read completed. The first-byte watchdog and
        # the idle watcher both raise PlayTimeoutError; the stderr-auth watcher is
        # handled above. Pick whichever of idle/first-byte finished and cancel the
        # rest. The first-byte deadline only fires when no stdout ever arrived, so
        # it shares the stream-idle handling below.
        fired_watcher = first_byte_task if first_byte_task in done else idle_task
        for watcher in watcher_tasks:
            if watcher is not fired_watcher:
                watcher.cancel()
        await asyncio.gather(
            *(w for w in watcher_tasks if w is not fired_watcher),
            return_exceptions=True,
        )
        idle_exc = fired_watcher.exception()
        if idle_exc is None:
            read_result = await read_task
        elif (
            isinstance(idle_exc, PlayTimeoutError)
            and getattr(idle_exc, "error_class", None) == ErrorClass.TIMEOUT_POST_RESPONSE
        ):
            post_response_killed = True
            _logger.info(
                "post_response_process_kill",
                agent_id=handle.agent_id,
                grace_s=_POST_RESPONSE_GRACE_S,
            )
            await _kill_process(proc, handle.agent_id)
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


async def _finalize_nonzero_exit(
    proc: asyncio.subprocess.Process,
    handle: AgentHandle,
    *,
    cfg: AgentConfig,
    rc: int,
    raw_output: str,
    sniffer: _StderrSniffer | None = None,
) -> bool:
    """Read stderr, classify, and either recover or raise ``AgentProcessError``.

    Returns ``True`` when the non-zero exit is a teardown-only SessionEnd-hook
    failure with output already on stdout (#253) — the response completed, so
    the caller should surface the captured output as a successful dispatch
    instead of discarding finished work. Otherwise classifies the failure and
    raises ``AgentProcessError`` (the common case).

    The live stderr sniffer has already drained ``proc.stderr`` for this
    dispatch, so prefer its captured text; only fall back to re-reading the pipe
    when no sniffer ran (defensive — keeps the classifier working if the live
    drain was ever skipped).
    """
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
    # A SessionEnd-hook failure runs after the model's response (with its result
    # block) is already on stdout, so a non-zero exit caused solely by it does
    # not invalidate the dispatch (#253). Surface the completed output and let
    # the normal result-parse path judge it rather than burning the work.
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
    _close_process_transport(proc)
    raise AgentProcessError(
        f"agent {handle.agent_id!r} exited with code {rc} [{error_class}]: {detail}"
    )


async def _kill_process(proc: asyncio.subprocess.Process, agent_id: str) -> None:
    """Send SIGTERM, wait up to _SIGKILL_GRACE seconds, then SIGKILL."""
    if not hasattr(os, "killpg"):
        # Windows: no process groups. ``start_new_session=True`` is a no-op
        # there, so there is nothing to ``os.killpg`` and ``os.getpgid`` is
        # absent entirely. Tear the tree down by PID via taskkill instead.
        if proc.pid is None:
            _close_process_transport(proc)
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
            _logger.warning(
                "taskkill_failed",
                agent_id=agent_id,
                pid=proc.pid,
            )
        _close_process_transport(proc)
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, TypeError):
        # ProcessLookupError: the process already exited. TypeError: proc.pid is
        # None (subprocess never spawned). Either way there is no group to kill.
        _close_process_transport(proc)
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        _close_process_transport(proc)
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=float(_SIGKILL_GRACE))
    except TimeoutError:
        _logger.warning("sending_sigkill", agent_id=agent_id)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
        # Bound the post-SIGKILL reap. An unbounded ``await proc.wait()`` here
        # hangs the entire dispatch coroutine forever when SIGKILL fails to
        # reap the group (an escaped grandchild still holding the group, or the
        # asyncio child-watcher never delivering exit). The hung coroutine never
        # re-raises, so ``manager.dispatch`` never catches the timeout and the
        # agent is pinned in BUSY indefinitely — which in turn suppresses every
        # session-end backstop (selector ``fleet_quiescent``). Observed
        # 2026-06-23, session a3202694: an agy first-byte timeout SIGKILL hung
        # for 2h+ and only the time-budget reserve could end the session. Give
        # up after the grace window and fall through to the survivor probe; a
        # truly-unreapable process becomes a logged OS leak, never a wedge.
        try:
            await asyncio.wait_for(proc.wait(), timeout=float(_SIGKILL_GRACE))
        except TimeoutError:
            _logger.warning("subprocess_unreaped_after_sigkill", agent_id=agent_id, pid=proc.pid)
    finally:
        try:
            os.killpg(pgid, 0)
            ps = await asyncio.create_subprocess_exec(
                "ps",
                "-g",
                str(pgid),
                "-o",
                "pid=",
                # Never inherit the sidecar's stdin (the live Tauri JSON-RPC
                # pipe); a child probing it can wedge teardown (#155).
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
        except ProcessLookupError:
            pass
        _close_process_transport(proc)


def _close_streams(proc: asyncio.subprocess.Process) -> None:
    """Signal EOF on open pipes so the transport can be GC'd cleanly."""
    if proc.stdout is not None:
        with contextlib.suppress(Exception):
            proc.stdout.feed_eof()
    if proc.stderr is not None:
        with contextlib.suppress(Exception):
            proc.stderr.feed_eof()


def _close_process_transport(proc: asyncio.subprocess.Process) -> None:
    """Close asyncio's subprocess transport when it has not closed itself."""
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.close()


# ---------------------------------------------------------------------------
# Core dispatch
# ---------------------------------------------------------------------------


async def dispatch_cli(
    handle: AgentHandle,
    prompt: str,
    *,
    cfg: AgentConfig,
    pricing: PricingQuote | None = None,
    default_timeout: int = _DEFAULT_TIMEOUT,
    python_executable: str | None = None,
    identity_env: dict[str, str] | None = None,
    on_subprocess_spawned: Callable[[int], Awaitable[None]] | None = None,
    on_subprocess_exited: Callable[[int, int | None], Awaitable[None]] | None = None,
    cwd_override: Path | None = None,
    resume_session_id: str | None = None,
    first_byte_timeout_override: float | None = None,
) -> AgentInvocationResult:
    """Invoke the agent CLI and return raw output + metadata.

    Parameters
    ----------
    handle:
        The AgentHandle owning this agent; used to read binary/type/working_dir.
    prompt:
        Pre-rendered skill prompt to pass to the agent.
    cfg:
        Per-agent configuration from ``RuntimeConfig.agents[name]``.
    pricing:
        Resolved per-model :class:`~agentshore.agents.pricing.PricingQuote` used
        to price this dispatch's token usage. ``None`` (direct/test callers)
        falls back to the bundled global-default quote; the manager always
        resolves the live quote from ``RuntimeConfig.pricebook``.
    python_executable:
        If set, ``cfg.binary`` is treated as a Python script path invoked with
        this interpreter.  Used by tests to run ``mock_agent.py`` through the
        production code path.
    identity_env:
        Optional env-var overlay (e.g. ``GIT_AUTHOR_*``, ``GH_TOKEN``) applied
        on top of ``os.environ`` for the spawned subprocess. ``None`` or empty
        preserves the inherit-parent-env behaviour.
    cwd_override:
        When supplied, replaces ``handle.working_dir`` for this single
        dispatch's cwd (and the ``--project-dir`` style flag in ``argv``).
        The handle is not mutated — concurrent dispatches against the same
        handle may each target a different worktree. ``AGENTSHORE_PROJECT_PATH``
        in ``identity_env`` continues to point at the main repo.
    first_byte_timeout_override:
        One-off launch-to-first-byte budget for this single dispatch, overriding
        the per-agent/per-type defaults (still clamped to the wall-clock timeout).
        Set on the no-JSON resume-retry (#232) so a re-emission can't inherit
        agy's 1800s fresh-task deadline and hang. ``None`` = default resolution.

    Each call spawns a fresh CLI session, except the narrow single-shot
    JSON-retry path: when *resume_session_id* is set, the prior session is
    re-entered once to recover an omitted result block (desktop-dy2j). General
    long-session ``--resume`` remains banned — see ``feedback_persistent_sessions``.
    """
    timeout = cfg.timeout if cfg.timeout is not None else default_timeout
    stream_idle_timeout = float(cfg.stream_idle_timeout)
    # Safety clamp (#177): a misconfigured ``stream_idle_timeout`` larger than the
    # wall-clock ``timeout`` would let the silence watchdog never fire before the
    # dispatch is force-killed — effectively disabling early silence detection. Cap
    # it at the dispatch timeout so the idle watcher always gets a chance to run.
    stream_idle_timeout = min(stream_idle_timeout, float(timeout))
    max_bytes = cfg.max_output_size

    effective_cwd = cwd_override if cwd_override is not None else handle.working_dir

    # Grok can't take the prompt over stdin (no stdin mode) and rejects an empty
    # ``-p`` ("--single: prompt is empty", issue #160). Where the other CLIs
    # would route the prompt over stdin (Windows arg-length limits), Grok
    # instead reads it from a temp file via ``--prompt-file``. Cleaned up in the
    # ``finally`` below.
    grok_prompt_file: Path | None = None
    if handle.agent_type == AgentType.GROK and _prompt_on_stdin(python_executable):
        grok_prompt_file = _write_grok_prompt_file(prompt)

    _argv = _build_dispatch_argv(
        handle,
        prompt,
        cfg=cfg,
        python_executable=python_executable,
        resume_session_id=resume_session_id,
        effective_cwd=effective_cwd,
        prompt_file=str(grok_prompt_file) if grok_prompt_file is not None else None,
    )
    argv, prompt_bytes, argv_str = _argv.argv, _argv.prompt_bytes, _argv.argv_str

    _logger.info(
        "cli_dispatch_start",
        agent_id=handle.agent_id,
        agent_type=str(handle.agent_type),
        argv_preview=argv_str,
        extra_flags=list(cfg.extra_flags),
        dispatch_num=handle.dispatches,
        prompt_bytes=prompt_bytes,
        identity=cfg.identity,
        identity_env_keys=sorted(identity_env) if identity_env else [],
    )

    t_start = time.monotonic()

    # Agent subprocesses run ``git rebase``/``git commit``/``git push`` inside
    # skills, and those invocations don't route through ``command.git``'s
    # hardened env. Route the agent's env through the SAME hardened,
    # non-interactive git policy AgentShore uses for its own git: a no-op editor
    # (so a rebase-internal ``git commit -e`` can't fall back to vim and hang,
    # leaking the worktree — #168) AND ``GIT_TERMINAL_PROMPT=0`` / no askpass (so
    # an agent git op that needs credentials fails *fast* instead of blocking on
    # a credential prompt with no TTY and hanging the full wall-clock — #177).
    # When the agent's identity carries a token, inject it as the per-identity
    # HTTPS credential so the agent authenticates AS ITS OWN identity — each
    # agent subprocess carries its own token, so multi-identity fleets stay
    # correctly attributed (codex pushes as its identity, claude_code as its).
    identity = identity_env or {}
    token = identity.get("GH_TOKEN") or identity.get("GITHUB_TOKEN")
    git_overlay = dict(identity)
    if token:
        git_overlay.update(subprocess_env.git_auth_config_overlay(token))
    env = subprocess_env.hardened_env(
        git_overlay,
        for_git=True,
        for_grok=(handle.agent_type == AgentType.GROK),
        for_antigravity=(handle.agent_type == AgentType.ANTIGRAVITY),
    )

    # Resolve npm-shim agent binaries (codex.cmd etc.) to a full path so they
    # spawn on Windows; CreateProcess only finds bare names ending in .exe.
    argv = _resolve_executable(argv)

    # On Windows the prompt is fed over stdin to dodge the cmd.exe command-line
    # limit (see build_argv); elsewhere stdin stays closed. Two exceptions keep
    # stdin closed because they never read the prompt from it: Grok (it's in
    # --prompt-file) and Antigravity (``agy`` has no stdin mode — the prompt is
    # always in ``-p``). Opening a PIPE and writing a prompt the child never
    # drains could block on a full pipe buffer.
    prompt_on_stdin = (
        _prompt_on_stdin(python_executable)
        and grok_prompt_file is None
        and handle.agent_type != AgentType.ANTIGRAVITY
    )

    # Backstop for the worktree-reclaim TOCTOU race (#176): the dispatch cwd is
    # an AgentShore-managed worktree that reconcile / collision-reclaim churn can
    # remove between allocation and spawn. POSIX surfaces a missing cwd as a raw
    # ``FileNotFoundError`` (an ``OSError``) from ``create_subprocess_exec``,
    # which ``manager.dispatch`` re-raises uncaught — the play executor then has
    # no recoverable match and logs ``unexpected_play_error``. Detect it here and
    # raise a *typed* recoverable error instead so the play fails cleanly and PPO
    # re-picks. Done before spawn so the diagnosis is unambiguous (a spawn-time
    # ``FileNotFoundError`` could otherwise mean a missing executable).
    if not os.path.isdir(effective_cwd):
        raise AgentProcessCrashed(f"dispatch cwd (worktree) no longer exists: {effective_cwd}")

    # Antigravity (``agy``) on Windows must run under a ConPTY: in ``-p`` mode it
    # writes a terminal Device-Attributes query and blocks for the reply before
    # emitting anything, so over plain pipes it deadlocks at zero bytes (every
    # dispatch no-ops). The ConPTY answers the query and agy proceeds. The
    # adapter quacks like ``asyncio.subprocess.Process`` for the read/kill path;
    # ``cast`` keeps the rest of the dispatch typing unchanged. Inert off-Windows
    # and for every other agent (see ``conpty.should_use_conpty``). The test shim
    # (``python_executable``) runs a Python mock with no terminal dependency, so
    # it always takes the plain-pipe path.
    use_conpty = python_executable is None and conpty.should_use_conpty(handle.agent_type)
    try:
        if use_conpty:
            proc = cast(
                "asyncio.subprocess.Process",
                await conpty.spawn(
                    argv, cwd=str(effective_cwd), env=env, limit=cfg.line_limit_bytes
                ),
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if prompt_on_stdin else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(effective_cwd),
                limit=cfg.line_limit_bytes,
                start_new_session=True,
                creationflags=subprocess_env.no_window_creationflags(),
                env=env,
            )
    except NotADirectoryError as exc:
        # cwd path exists but is a file, not a directory — same recoverable class
        # as the missing-cwd race above.
        raise AgentProcessCrashed(
            f"dispatch cwd (worktree) is not a directory: {effective_cwd}"
        ) from exc
    except FileNotFoundError as exc:
        # The pre-spawn ``isdir`` check ruled out a missing cwd, so a
        # ``FileNotFoundError`` here is the agent executable being absent. Still
        # recoverable (re-instantiate the agent), so map to the same typed class
        # rather than letting a raw ``OSError`` reach ``unexpected_play_error``.
        raise AgentProcessCrashed(
            f"agent {handle.agent_id!r} executable not found: {argv[0]!r}"
        ) from exc
    handle.process = proc
    # Drains stderr live so a backend-auth signature aborts the dispatch as AUTH
    # (instead of waiting out the idle timeout). It also retains the stderr text
    # for _finalize_nonzero_exit, which can no longer re-read a now-drained pipe.
    stderr_sniffer = _StderrSniffer()
    stdin_feeder: asyncio.Task[None] | None = None
    if prompt_on_stdin and proc.stdin is not None:
        stdin_feeder = asyncio.create_task(_feed_prompt_stdin(proc, prompt))
    if on_subprocess_spawned is not None and proc.pid is not None:
        await on_subprocess_spawned(proc.pid)

    try:
        read_result, post_response_killed = await _await_output_or_timeout(
            proc,
            handle,
            max_bytes=max_bytes,
            cfg=cfg,
            stream_idle_timeout=stream_idle_timeout,
            timeout=float(timeout),
            prompt_bytes=prompt_bytes,
            sniffer=stderr_sniffer,
            dispatch_start=t_start,
            first_byte_timeout_override=first_byte_timeout_override,
        )
        # Retained for the narrow JSON-retry path (desktop-dy2j). General
        # --resume dispatch is still banned (see feedback_persistent_sessions).
        raw_output, usage, _observed_session_id = (
            read_result.raw,
            read_result.usage,
            read_result.session_id,
        )
    except TimeoutError:
        await _kill_process(proc, handle.agent_id)
        _close_streams(proc)
        _close_process_transport(proc)
        raise PlayTimeoutError(
            f"agent {handle.agent_id!r} timed out after {timeout}s",
            error_class=ErrorClass.TIMEOUT_WALLCLOCK,
        ) from None
    except asyncio.CancelledError:
        # Task cancellation — clean up the child process before propagating.
        with contextlib.suppress(Exception):
            await _kill_process(proc, handle.agent_id)
        _close_streams(proc)
        _close_process_transport(proc)
        raise
    except Exception:
        # AgentOutputInvalid and other standard errors — ensure the process is cleaned up.
        with contextlib.suppress(Exception):
            await _kill_process(proc, handle.agent_id)
        _close_streams(proc)
        _close_process_transport(proc)
        raise
    finally:
        if stdin_feeder is not None:
            stdin_feeder.cancel()
            with contextlib.suppress(Exception):
                await stdin_feeder
        if grok_prompt_file is not None:
            grok_prompt_file.unlink(missing_ok=True)
        if on_subprocess_exited is not None and proc.pid is not None:
            with contextlib.suppress(Exception):
                await on_subprocess_exited(proc.pid, proc.returncode)
        handle.process = None

    duration_ms = int((time.monotonic() - t_start) * 1000)

    # agy wraps actual output inside a task-status block; unwrap it so
    # parse_skill_result sees the model's content, not the status envelope.
    if handle.agent_type == AgentType.ANTIGRAVITY:
        raw_output = cli_antigravity.extract_output(raw_output)
        # agy reveals no session id on stdout (no parser), so resolve its
        # conversation id from the on-disk cache keyed by this dispatch's cwd.
        # This gives agy a resumable id for the narrow JSON-retry path
        # (desktop-dy2j), exactly like the parsed agents. Best-effort: None on
        # any miss, which simply means no retry — never fatal.
        if _observed_session_id is None:
            _observed_session_id = cli_antigravity.resolve_conversation_id(
                effective_cwd, home=env.get("HOME")
            )

    rc = proc.returncode
    if rc != 0 and not post_response_killed:
        recovered = await _finalize_nonzero_exit(
            proc, handle, cfg=cfg, rc=rc or 1, raw_output=raw_output, sniffer=stderr_sniffer
        )
        if recovered:
            # Teardown-only SessionEnd-hook failure (#253): the model's response
            # is already on stdout. Normalise the exit so the dispatch flows
            # through the success path and the result block parses, instead of
            # discarding finished work as error_class=unknown.
            rc = 0

    if usage.reported_cost > 0:
        # Vendor-authoritative cost (Claude Code's total_cost_usd). See _UsageTotals.
        dollar_cost = usage.reported_cost
        cost_source = "vendor_reported"
    else:
        dollar_cost = estimate_cost(
            usage.tokens_in,
            usage.tokens_out,
            pricing if pricing is not None else default_quote(),
            cached_tokens_in=usage.cached_tokens_in,
            cache_write_tokens_in=usage.cache_write_tokens_in,
        )
        cost_source = "token_derived"
    _logger.info(
        "cli_dispatch_done",
        cost_source=cost_source,
        agent_id=handle.agent_id,
        duration_ms=duration_ms,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        cached_tokens_in=usage.cached_tokens_in,
        cache_write_tokens_in=usage.cache_write_tokens_in,
        turn_count=usage.turn_count,
        max_turn_input_tokens=usage.max_turn_input_tokens,
        dollar_cost=dollar_cost,
        prompt_bytes=prompt_bytes,
        output_length=len(raw_output),
        output_tail=raw_output[-500:] if raw_output else "(empty)",
    )
    _close_process_transport(proc)
    return AgentInvocationResult(
        raw_output=raw_output,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        cached_tokens_in=usage.cached_tokens_in,
        cache_write_tokens_in=usage.cache_write_tokens_in,
        turn_count=usage.turn_count,
        max_turn_input_tokens=usage.max_turn_input_tokens,
        dollar_cost=dollar_cost,
        duration_ms=duration_ms,
        exit_code=rc or 0,
        session_id=_observed_session_id,
    )


# ---------------------------------------------------------------------------
# Re-exports — keep agentshore.agents.cli_agent.<symbol> resolving for all
# external callers (manager.py, cli_grok.py, cli_antigravity.py, tests).
# ---------------------------------------------------------------------------

__all__ = [
    # errors.py
    "_AUTH_PATTERNS",
    "_AUTH_STDOUT",
    "_CACHE_RENEWAL_MARKERS",
    "_classify_error",
    "_clean_stderr",
    "_CODEX_ROLLOUT_PATTERNS",
    "_ENOSPC_PATTERNS",
    "_extract_cli_report_path",
    "_INVALID_MODEL_PATTERNS",
    "_INVALID_MODEL_STDOUT",
    "_is_cache_renewal_stdin_hang",
    "_is_transient_cache_blip",
    "is_post_response_hook_failure",
    "_OOM_PATTERNS",
    "_PARSE_EOF_MARKERS",
    "_process_error_detail",
    "_RATE_LIMIT_PATTERNS",
    "_RATE_LIMIT_STDOUT",
    "_STDIN_CLOSED_AFTER_CACHE_RENEWAL_MARKERS",
    "_TIMEOUT_PATTERNS",
    "_TIMEOUT_STDOUT",
    "_TRANSIENT_NETWORK_PATTERNS",
    # argv.py
    "_apply_yolo_default",
    "_DEFAULT_YOLO_FLAGS",
    "_prompt_on_stdin",
    "_resolve_executable",
    "_RESUMABLE_AGENT_TYPES",
    "_write_grok_prompt_file",
    "build_argv",
    "build_resume_argv",
    # watchdogs.py
    "_DispatchArgv",
    "_FIRST_BYTE_DEADLINE_BY_TYPE",
    "_FIRST_BYTE_DEADLINE_S",
    "_NEVER_S",
    "_POST_RESPONSE_GRACE_S",
    "_ReadOutputFailed",
    "_StderrSniffer",
    "_StdoutActivity",
    "_watch_first_byte",
    "_watch_stderr_auth",
    "_watch_stream_idle",
    # driver (this module) — watchdog orchestrators
    "_await_output_or_timeout",
    "_resolve_first_byte_deadline",
    # parsing.py
    "_extract_session_id_from_jsonl",
    "_extract_text_from_codex_jsonl",
    "_extract_text_from_grok_jsonl",
    "_extract_text_from_stream_json",
    "_extract_text_value",
    "_FunctionFormat",
    "_is_terminal_event",
    "_maybe_parse_usage",
    "_parse_claude_output",
    "_PARSERS",
    "_ReadOutput",
    "_TERMINAL_EVENT_TYPES",
    "CliOutputFormat",
    # driver (this module)
    "_build_dispatch_argv",
    "_close_process_transport",
    "_close_streams",
    "_feed_prompt_stdin",
    "_finalize_nonzero_exit",
    "_kill_process",
    "_read_output",
    "_read_output_guarded",
    "dispatch_cli",
]
