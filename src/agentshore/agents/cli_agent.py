"""CLI agent adapter — asyncio subprocess dispatch for Claude Code, Codex, and Gemini."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, NoReturn

from agentshore.agents.costs import estimate_cost
from agentshore.agents.handle import AgentInvocationResult
from agentshore.errors import AgentOutputInvalid, AgentProcessError, PlayTimeoutError
from agentshore.logging import get_logger
from agentshore.state import AgentType

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from agentshore.agents.handle import AgentHandle
    from agentshore.config import AgentConfig

_logger = get_logger(__name__)

type _ReadOutputResult = tuple[str, int, int, int, int, int, int, str | None]

_DEFAULT_TIMEOUT = 3600  # seconds — fallback when AgentConfig.timeout is None
_SIGKILL_GRACE = 10  # seconds between SIGTERM and SIGKILL
_LINE_DRIFT_WARN_BYTES = 1_048_576  # warn once if any single line exceeds 1MB
_ARGV_PREVIEW_MAX_CHARS = 256  # log clamp; full prompt is reconstructible from skill+params

# YOLO permission flags applied by default per agent type. AgentShore is an
# autonomous orchestrator — agents can't pause for human approval on each
# `gh` call, so we bypass the per-tool permission gates the CLIs ship with.
# The user can opt out by explicitly setting any non-empty extra_flags in
# agentshore.yaml; that signals "I'm managing flags myself."
_RATE_LIMIT_PATTERNS = (
    "rate limit",
    "rate_limit",
    "429",
    "too many requests",
    "overloaded",
    "capacity",
    "retry after",
    "throttl",
)
_AUTH_PATTERNS = (
    "unauthorized",
    "401",
    "authentication",
    "invalid api key",
    "bad credentials",
    "forbidden",
    "403",
    "irrecoverable github access failure",
    "github connector returned 404",
    "connector repo 404",
    "could not resolve to a repository with the name",
    "could not resolve to a repository",
    "repository/pr is not accessible",
    "not found/could not resolve repository",
    "repository is not resolvable to this token",
    "not resolvable to this token/session",
    "lacks access to repository",
    "cannot access repository metadata",
)
_TIMEOUT_PATTERNS = ("timeout", "timed out", "deadline exceeded", "context deadline")
_INVALID_MODEL_PATTERNS = (
    "modelnotfounderror",
    "model not found",
    "requested entity was not found",
    "not found for api version",
    "not found or is not supported",
    "not supported when using codex with a chatgpt account",
    "invalid_request_error",
)
# Codex CLI internal error: its rollout-recording layer references a session
# thread id it can't find on disk. desktop-yxlj observed one occurrence in
# 4600+ plays. The error is permanent for the current codex process but a
# fresh `codex exec` lands a new thread id, so spawning again recovers.
# Pulling this out of the "unknown" bucket gives operators a queryable signal
# for recurrence rate and lets the existing take_break recovery path fire
# under a typed name instead of the generic catch-all.
_CODEX_ROLLOUT_PATTERNS = ("failed to record rollout items",)
# Out-of-memory signatures. An OS OOM kill usually arrives as SIGKILL (rc -9)
# with little/no agent output, but some runtimes log a signature too. Matching
# either routes the exit to ``crash_oom`` (#7) so it is NOT treated as a
# rate-limit.
_OOM_PATTERNS = (
    "out of memory",
    "oomkilled",
    "enomem",
    "cannot allocate memory",
    "memory exhausted",
)


def _classify_error(rc: int, stderr: str, stdout: str) -> str:
    """Classify a non-zero CLI exit into a semantic error bucket.

    Returns one of ``"rate_limit"``, ``"auth"``, ``"timeout"``,
    ``"invalid_model"``, ``"codex_rollout"``, ``"crash_oom"``,
    ``"crash_signal"``, or ``"unknown"``.
    Inspects both *stderr* and the trailing 1 000 chars of *stdout* because some
    CLIs (notably Claude Code) exit with code 1 on rate limits without writing
    anything to stderr. ``rc`` is inspected for signal deaths last, after the
    content classifiers, so an explicit rate-limit/auth/timeout message still
    wins over the raw return code.
    """
    combined = (stderr + stdout[-1000:]).lower()
    if any(p in combined for p in _RATE_LIMIT_PATTERNS):
        return "rate_limit"
    if any(p in combined for p in _AUTH_PATTERNS):
        return "auth"
    if any(p in combined for p in _TIMEOUT_PATTERNS):
        return "timeout"
    if any(p in combined for p in _INVALID_MODEL_PATTERNS):
        return "invalid_model"
    if any(p in combined for p in _CODEX_ROLLOUT_PATTERNS):
        return "codex_rollout"
    if any(p in combined for p in _OOM_PATTERNS):
        return "crash_oom"
    # Negative return codes are POSIX signal deaths. SIGKILL (-9) from the OS
    # OOM killer or an external kill is a crash, NOT a rate limit — bucketing it
    # as "unknown" routed it into rate-limit take_break recovery (#7). SIGTERM
    # (-15) and SIGINT (-2) are graceful AgentShore/OS-initiated stops and keep
    # falling through to "unknown".
    if rc < 0 and rc not in (-2, -15):
        return "crash_signal"
    return "unknown"


def _process_error_detail(
    *,
    agent_type: AgentType,
    model: str | None,
    error_class: str,
    stderr: str,
    stdout: str,
) -> str:
    """Return a concise user-facing subprocess error detail."""
    if error_class == "invalid_model":
        model_text = f" model {model!r}" if model else ""
        report = _extract_cli_report_path(stderr)
        suffix = f" Full report: {report}" if report else ""
        return (
            f"{agent_type.value}{model_text} is not available to the CLI/API "
            f"(invalid or unsupported model). "
            f"Check agents.{agent_type.value}.model_tiers in agentshore.yaml.{suffix}"
        )

    cleaned = _clean_stderr(stderr)
    if cleaned:
        return cleaned[:500]
    return stdout[-200:] if stdout else "(no output)"


def _extract_cli_report_path(stderr: str) -> str | None:
    marker = "Full report available at:"
    if marker not in stderr:
        return None
    tail = stderr.split(marker, 1)[1].strip()
    return tail.split(None, 1)[0] if tail else None


def _clean_stderr(stderr: str) -> str:
    noisy_prefixes = (
        "YOLO mode is enabled.",
        "Ripgrep is not available.",
        "Falling back to GrepTool.",
    )
    lines = [
        line.strip()
        for line in stderr.splitlines()
        if line.strip() and not line.strip().startswith(noisy_prefixes)
    ]
    return "\n".join(lines)


_DEFAULT_YOLO_FLAGS: dict[AgentType, tuple[str, ...]] = {
    AgentType.CLAUDE_CODE: ("--dangerously-skip-permissions",),
    AgentType.CODEX: (
        "--ignore-user-config",
        "--ignore-rules",
        "--dangerously-bypass-approvals-and-sandbox",
    ),
    AgentType.GEMINI: ("--approval-mode=yolo", "--skip-trust"),
}


@dataclass(frozen=True, slots=True)
class _UsageTotals:
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens_in: int = 0
    cache_write_tokens_in: int = 0
    turn_count: int = 0
    max_turn_input_tokens: int = 0


_POST_RESPONSE_GRACE_S: Final[float] = 60.0


@dataclass(slots=True)
class _StdoutActivity:
    last_stdout_at: float
    received_any: bool = False
    response_complete: bool = False

    def mark(self) -> None:
        self.last_stdout_at = time.monotonic()
        self.received_any = True

    def mark_response_complete(self) -> None:
        self.response_complete = True
        self.last_stdout_at = time.monotonic()


@dataclass(frozen=True, slots=True)
class _ReadOutputFailed:
    exc: BaseException


def _apply_yolo_default(agent_type: AgentType, extra_flags: tuple[str, ...]) -> tuple[str, ...]:
    """Return YOLO defaults for *agent_type* when the user provided no flags."""
    if extra_flags:
        return extra_flags
    return _DEFAULT_YOLO_FLAGS.get(agent_type, ())


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def build_argv(
    agent_type: AgentType,
    prompt: str,
    *,
    binary: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    extra_flags: tuple[str, ...] = (),
    context_path: str | None = None,
    project_dir: str | None = None,
) -> list[str]:
    """Return the argv list for invoking *agent_type* with *prompt*.

    Each dispatch spawns a fresh CLI session — see
    `feedback_persistent_sessions` memory: ``--resume`` was buggy in
    production (silent state-rot late in long sessions) and is no longer
    used.

    Exported so tests can assert command shape without spawning a subprocess.
    """
    extra_flags = _apply_yolo_default(agent_type, tuple(extra_flags))
    if agent_type == AgentType.CLAUDE_CODE:
        binary = binary or "claude"
        args = [binary, "-p", "--verbose", "--output-format", "stream-json"]
        if model:
            args += ["--model", model]
        args.extend(extra_flags)
        if context_path:
            args += ["--append-system-prompt", f"Context file: {context_path}"]
        args.append(prompt)
        return args

    if agent_type == AgentType.CODEX:
        binary = binary or "codex"
        yolo = "--dangerously-bypass-approvals-and-sandbox" in extra_flags
        args = [binary, "exec", "--json"]
        if not yolo:
            args.append("--full-auto")
        if model:
            args += ["-m", model]
        if reasoning_effort:
            args += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
        # desktop-pxg: without this, codex's shell tool runs subprocesses (gh,
        # git, etc.) with a stripped env, so the GH_TOKEN we inject for the
        # codex process never reaches `gh api user`. Result: identity mismatch
        # and refused mutations. Setting inherit=all passes our env (including
        # the per-identity GH_TOKEN/GH_CONFIG_DIR) through to every codex
        # shell-tool invocation.
        args += ["-c", "shell_environment_policy.inherit=all"]
        args.extend(extra_flags)
        if project_dir:
            args += ["-C", project_dir]
        args.append(prompt)
        return args

    if agent_type == AgentType.GEMINI:
        binary = binary or "gemini"
        args = [binary, "--output-format", "stream-json"]
        if model:
            args += ["--model", model]
        args.extend(extra_flags)
        args += ["-p", prompt]
        return args

    msg = f"build_argv: unsupported CLI agent type {agent_type!r}"
    raise ValueError(msg)


# ---------------------------------------------------------------------------
# Core dispatch
# ---------------------------------------------------------------------------


async def dispatch_cli(
    handle: AgentHandle,
    prompt: str,
    *,
    cfg: AgentConfig,
    default_timeout: int = _DEFAULT_TIMEOUT,
    python_executable: str | None = None,
    identity_env: dict[str, str] | None = None,
    on_subprocess_spawned: Callable[[int], Awaitable[None]] | None = None,
    on_subprocess_exited: Callable[[int, int | None], Awaitable[None]] | None = None,
    cwd_override: Path | None = None,
    resume_session_id: str | None = None,
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

    Each call spawns a fresh CLI session. ``--resume`` is intentionally not
    supported: see ``feedback_persistent_sessions`` memory.
    """
    timeout = cfg.timeout if cfg.timeout is not None else default_timeout
    stream_idle_timeout = float(cfg.stream_idle_timeout)
    max_bytes = cfg.max_output_size

    effective_cwd = cwd_override if cwd_override is not None else handle.working_dir

    if python_executable is not None:
        # Test shim: invoke cfg.binary as a Python script.
        argv = [python_executable, cfg.binary or ""]
    else:
        argv = build_argv(
            handle.agent_type,
            prompt,
            binary=cfg.binary,
            model=handle.model or cfg.model,
            reasoning_effort=handle.reasoning_effort or cfg.reasoning_effort,
            extra_flags=cfg.extra_flags,
            project_dir=str(effective_cwd),
        )

    # desktop-dy2j: narrow JSON-retry path injects --resume so the agent
    # re-enters the same session and emits the structured trailer it missed.
    if resume_session_id is not None and handle.agent_type == AgentType.CLAUDE_CODE:
        argv = [
            argv[0],
            "--resume",
            resume_session_id,
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            prompt,
        ]

    prompt_bytes = len(prompt.encode("utf-8"))

    # Clamp argv_preview to keep the log readable. The last argv element is the
    # full skill prompt (~7 KB), and embedding it in every dispatch event bloats
    # the orchestrator log by ~1 MB per session. The full prompt is
    # reconstructible from the skill template plus the PlayParams that hit the
    # dispatcher, so keep only the leading flags here.
    argv_str = " ".join(argv[:10])
    if len(argv_str) > _ARGV_PREVIEW_MAX_CHARS:
        truncated = len(argv_str) - _ARGV_PREVIEW_MAX_CHARS
        argv_str = argv_str[:_ARGV_PREVIEW_MAX_CHARS] + f"…(+{truncated} chars truncated)"

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

    env = {**os.environ, **identity_env} if identity_env else None

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(effective_cwd),
        limit=cfg.line_limit_bytes,
        start_new_session=True,
        env=env,
    )
    handle.process = proc
    if on_subprocess_spawned is not None and proc.pid is not None:
        await on_subprocess_spawned(proc.pid)

    post_response_killed = False
    try:
        stdout_activity = _StdoutActivity(last_stdout_at=time.monotonic())
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
        done, _pending = await asyncio.wait(
            {read_task, idle_task},
            timeout=float(timeout),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            read_task.cancel()
            idle_task.cancel()
            await asyncio.gather(read_task, idle_task, return_exceptions=True)
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
                error_class="timeout_wallclock",
            ) from None
        if read_task in done:
            idle_task.cancel()
            await asyncio.gather(idle_task, return_exceptions=True)
            read_result = await read_task
            if isinstance(read_result, _ReadOutputFailed):
                raise read_result.exc
            (
                raw_output,
                tokens_in,
                tokens_out,
                cached_tokens_in,
                cache_write_tokens_in,
                turn_count,
                max_turn_input_tokens,
                observed_session_id,
            ) = read_result
        else:
            idle_exc = idle_task.exception()
            if idle_exc is None:
                read_result = await read_task
            elif (
                isinstance(idle_exc, PlayTimeoutError)
                and getattr(idle_exc, "error_class", None) == "timeout_post_response"
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
            (
                raw_output,
                tokens_in,
                tokens_out,
                cached_tokens_in,
                cache_write_tokens_in,
                turn_count,
                max_turn_input_tokens,
                observed_session_id,
            ) = read_result
        # Retained for the narrow JSON-retry path (desktop-dy2j). General
        # --resume dispatch is still banned (see feedback_persistent_sessions).
        _observed_session_id = observed_session_id
    except TimeoutError:
        await _kill_process(proc, handle.agent_id)
        _close_streams(proc)
        _close_process_transport(proc)
        raise PlayTimeoutError(
            f"agent {handle.agent_id!r} timed out after {timeout}s",
            error_class="timeout_wallclock",
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
        if on_subprocess_exited is not None and proc.pid is not None:
            with contextlib.suppress(Exception):
                await on_subprocess_exited(proc.pid, proc.returncode)
        handle.process = None

    duration_ms = int((time.monotonic() - t_start) * 1000)

    rc = proc.returncode
    if rc != 0 and not post_response_killed:
        stderr_text = ""
        if proc.stderr:
            try:
                raw_err = await proc.stderr.read()
                stderr_text = raw_err.decode("utf-8", errors="replace")
            except (OSError, EOFError) as exc:
                _logger.warning(
                    "cli_agent_stderr_read_failed",
                    agent_id=handle.agent_id,
                    error=str(exc),
                )
        error_class = _classify_error(rc or 1, stderr_text, raw_output)
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

    dollar_cost = estimate_cost(
        tokens_in,
        tokens_out,
        cfg,
        cached_tokens_in=cached_tokens_in,
        cache_write_tokens_in=cache_write_tokens_in,
    )
    _logger.info(
        "cli_dispatch_done",
        agent_id=handle.agent_id,
        duration_ms=duration_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cached_tokens_in=cached_tokens_in,
        cache_write_tokens_in=cache_write_tokens_in,
        turn_count=turn_count,
        max_turn_input_tokens=max_turn_input_tokens,
        dollar_cost=dollar_cost,
        prompt_bytes=prompt_bytes,
        output_length=len(raw_output),
        output_tail=raw_output[-500:] if raw_output else "(empty)",
    )
    _close_process_transport(proc)
    return AgentInvocationResult(
        raw_output=raw_output,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cached_tokens_in=cached_tokens_in,
        cache_write_tokens_in=cache_write_tokens_in,
        turn_count=turn_count,
        max_turn_input_tokens=max_turn_input_tokens,
        dollar_cost=dollar_cost,
        duration_ms=duration_ms,
        exit_code=rc or 0,
        session_id=_observed_session_id,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _read_output(
    proc: asyncio.subprocess.Process,
    agent_type: AgentType,
    max_bytes: int,
    *,
    line_limit: int,
    agent_id: str,
    stdout_activity: _StdoutActivity | None = None,
) -> _ReadOutputResult:
    """Stream stdout, accumulate output, extract token metadata.

    Returns raw text, billable token buckets, lightweight turn metrics, and
    the observed CLI session id.
    Raises ``AgentOutputInvalid`` if the output exceeds *max_bytes* or if a
    single line exceeds *line_limit* (the asyncio readline buffer cap).
    """
    if proc.stdout is None:
        msg = "Subprocess stdout is None after create_subprocess_exec"
        raise RuntimeError(msg)
    chunks: list[bytes] = []
    total_bytes = 0
    usage = _UsageTotals()
    drift_warned = False

    try:
        async for line in proc.stdout:
            if stdout_activity is not None:
                stdout_activity.mark()
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

            if agent_type == AgentType.CLAUDE_CODE:
                usage = _maybe_parse_usage(line, usage)
                if (
                    stdout_activity is not None
                    and not stdout_activity.response_complete
                    and _is_claude_result_event(line)
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

    if agent_type == AgentType.CLAUDE_CODE:
        session_id = _extract_session_id_from_jsonl(raw)
        raw = _extract_text_from_stream_json(raw)
    elif agent_type == AgentType.CODEX:
        raw, usage, session_id = _extract_text_from_codex_jsonl(raw)
    elif agent_type == AgentType.GEMINI:
        raw, usage, session_id = _extract_text_from_gemini_jsonl(raw)
    else:
        session_id = None

    await proc.wait()
    return (
        raw,
        usage.tokens_in,
        usage.tokens_out,
        usage.cached_tokens_in,
        usage.cache_write_tokens_in,
        usage.turn_count,
        usage.max_turn_input_tokens,
        session_id,
    )


async def _read_output_guarded(
    proc: asyncio.subprocess.Process,
    agent_type: AgentType,
    max_bytes: int,
    *,
    line_limit: int,
    agent_id: str,
    stdout_activity: _StdoutActivity,
) -> _ReadOutputResult | _ReadOutputFailed:
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


async def _watch_stream_idle(
    stdout_activity: _StdoutActivity,
    *,
    timeout: float,
    agent_id: str,
    agent_type: str = "?",
    model_tier: str | None = None,
    prompt_bytes: int | None = None,
) -> NoReturn:
    """Kill the dispatch if the agent goes silent for ``timeout`` seconds.

    ``last_stdout_at`` is initialised at dispatch start, so an agent that
    never produces a single byte still gets killed after ``timeout``. The
    prior implementation guarded the check with ``if not received_any:
    continue``, which meant a fully-silent subprocess would never be
    killed by this watcher — observed 2026-05-28 session 08a948ed when
    calibrate_alignment ran for 20+ minutes emitting zero events while
    holding the trunk lock. The bug fix preserves the grace handling
    for legitimate startup latency (the configured ``stream_idle_timeout``
    default of 1800s gives any agent a full window to produce output)
    while ensuring the timeout is the *upper bound*, not "best effort
    if any output appeared."
    """
    sleep_s = min(5.0, max(timeout / 10.0, 0.01))
    while True:
        await asyncio.sleep(sleep_s)
        idle_for = time.monotonic() - stdout_activity.last_stdout_at
        effective = _POST_RESPONSE_GRACE_S if stdout_activity.response_complete else timeout
        if idle_for >= effective:
            tier_label = model_tier or "?"
            extra = f" ({agent_type}/{tier_label}"
            if prompt_bytes is not None:
                extra += f", prompt_bytes={prompt_bytes}"
            extra += ")"
            if stdout_activity.response_complete:
                raise PlayTimeoutError(
                    f"agent {agent_id!r}{extra} response complete but process "
                    f"did not exit within {_POST_RESPONSE_GRACE_S:g}s grace period",
                    error_class="timeout_post_response",
                )
            silence_qualifier = (
                "produced no stdout"
                if stdout_activity.received_any
                else "never produced any stdout"
            )
            raise PlayTimeoutError(
                f"agent {agent_id!r}{extra} {silence_qualifier} for {timeout:g}s",
                error_class="timeout_stream_idle",
            )


# ---------------------------------------------------------------------------
# CliOutputParser — per-agent-type JSONL/stream output parsing
# ---------------------------------------------------------------------------


class CliOutputParser:
    """Parses CLI agent output streams into text, usage totals, and session IDs.

    Groups the three per-agent-type parsers (Claude, Codex, Gemini) and their
    shared helpers into a cohesive class. All methods are static since parsers
    are stateless — the class provides namespace cohesion and discoverability.
    """

    @staticmethod
    def extract_session_id(raw: str) -> str | None:
        for line in map(str.strip, raw.splitlines()):
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            for key in ("session_id", "thread_id"):
                value = event.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    @staticmethod
    def maybe_parse_usage(line: bytes, current: _UsageTotals) -> _UsageTotals:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return current

        usage: object = {}
        if event.get("type") == "result":
            usage = event.get("usage", {})
        elif event.get("type") == "assistant":
            message = event.get("message", {})
            usage = message.get("usage", {}) if isinstance(message, dict) else {}
        elif event.get("type") == "message_delta":
            usage = event.get("usage", {})

        if not isinstance(usage, dict):
            return current
        parsed = CliOutputParser.usage_totals_from_dict(usage, input_includes_cache=False)
        return CliOutputParser.max_usage(current, parsed)

    @staticmethod
    def usage_totals_from_dict(
        usage: dict[str, object], *, input_includes_cache: bool
    ) -> _UsageTotals:
        total_usage = usage.get("total_token_usage")
        last_usage = usage.get("last_token_usage")
        turn_usage: dict[str, object] | None = None
        if isinstance(total_usage, dict):
            if isinstance(last_usage, dict):
                turn_usage = last_usage
            usage = total_usage
            input_includes_cache = True
        elif isinstance(last_usage, dict):
            usage = last_usage
            turn_usage = last_usage
            input_includes_cache = True

        input_tokens = _safe_int(usage.get("input_tokens"))
        cache_read_tokens = _safe_int(usage.get("cached_input_tokens")) + _safe_int(
            usage.get("cache_read_input_tokens")
        )
        cache_write_tokens = _safe_int(usage.get("cache_creation_input_tokens"))
        output_tokens = _safe_int(usage.get("output_tokens"))
        reasoning_tokens = _safe_int(usage.get("reasoning_output_tokens"))

        tokens_in = input_tokens if input_includes_cache else input_tokens + cache_read_tokens
        if not input_includes_cache:
            tokens_in += cache_write_tokens

        tokens_out = output_tokens if output_tokens > 0 else reasoning_tokens
        max_turn_input_tokens = (
            _safe_int(turn_usage.get("input_tokens")) if turn_usage else tokens_in
        )
        return _UsageTotals(
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cached_tokens_in=cache_read_tokens,
            cache_write_tokens_in=cache_write_tokens,
            max_turn_input_tokens=max_turn_input_tokens,
        )

    @staticmethod
    def max_usage(left: _UsageTotals, right: _UsageTotals) -> _UsageTotals:
        return _UsageTotals(
            tokens_in=max(left.tokens_in, right.tokens_in),
            tokens_out=max(left.tokens_out, right.tokens_out),
            cached_tokens_in=max(left.cached_tokens_in, right.cached_tokens_in),
            cache_write_tokens_in=max(left.cache_write_tokens_in, right.cache_write_tokens_in),
            turn_count=max(left.turn_count, right.turn_count),
            max_turn_input_tokens=max(left.max_turn_input_tokens, right.max_turn_input_tokens),
        )

    @staticmethod
    def parse_codex(raw: str) -> tuple[str, _UsageTotals, str | None]:
        session_id: str | None = None
        usage_totals = _UsageTotals()
        turn_count = 0
        max_turn_input_tokens = 0
        messages: list[str] = []

        for line in map(str.strip, raw.splitlines()):
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id")
                if isinstance(thread_id, str) and thread_id:
                    session_id = thread_id
                continue

            if event.get("type") in {"turn.completed", "token_count"}:
                usage = event.get("usage" if event.get("type") == "turn.completed" else "info", {})
                if isinstance(usage, dict):
                    turn_count += 1
                    parsed = CliOutputParser.usage_totals_from_dict(
                        usage, input_includes_cache=True
                    )
                    max_turn_input_tokens = max(
                        max_turn_input_tokens,
                        parsed.max_turn_input_tokens,
                    )
                    usage_totals = CliOutputParser.max_usage(usage_totals, parsed)
                continue

            if event.get("type") != "item.completed":
                continue
            item = event.get("item", {})
            if not isinstance(item, dict) or item.get("type") != "agent_message":
                continue
            text = item.get("text")
            if isinstance(text, str):
                messages.append(text)

        usage_totals = _UsageTotals(
            tokens_in=usage_totals.tokens_in,
            tokens_out=usage_totals.tokens_out,
            cached_tokens_in=usage_totals.cached_tokens_in,
            cache_write_tokens_in=usage_totals.cache_write_tokens_in,
            turn_count=turn_count,
            max_turn_input_tokens=max_turn_input_tokens,
        )
        return (messages[-1] if messages else raw), usage_totals, session_id

    @staticmethod
    def parse_gemini(raw: str) -> tuple[str, _UsageTotals, str | None]:
        session_id: str | None = None
        usage_totals = _UsageTotals()
        messages: list[str] = []
        final_response: str | None = None

        for line in map(str.strip, raw.splitlines()):
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            session_id = session_id or CliOutputParser._extract_gemini_session_id(event)

            event_type = event.get("type")
            if event_type == "message":
                role = event.get("role")
                message = event.get("message")
                if role is None and isinstance(message, dict):
                    role = message.get("role")
                if isinstance(role, str) and role.lower() not in {"assistant", "model"}:
                    continue
                text = CliOutputParser._extract_text_value(event.get("message"))
                if text is None:
                    text = CliOutputParser._extract_text_value(event)
                if text:
                    messages.append(text)
                continue

            if event_type == "result" or "response" in event:
                text = CliOutputParser._extract_text_value(event.get("response"))
                if text is None:
                    text = CliOutputParser._extract_text_value(event.get("result"))
                if text:
                    final_response = text

                stats = event.get("stats") or event.get("usage") or event.get("usageMetadata")
                if isinstance(stats, dict):
                    usage_totals = CliOutputParser.max_usage(
                        usage_totals, CliOutputParser._usage_totals_from_gemini_stats(stats)
                    )

        return (final_response or "".join(messages) or raw), usage_totals, session_id

    @staticmethod
    def parse_claude(raw: str) -> str:
        for line in map(str.strip, reversed(raw.splitlines())):
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result" and "result" in event:
                return str(event["result"])

        parts: list[str] = []
        for line in map(str.strip, raw.splitlines()):
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "assistant":
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(str(block.get("text", "")))
            elif event.get("type") == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    parts.append(str(delta.get("text", "")))
        return "".join(parts)

    @staticmethod
    def _extract_gemini_session_id(event: dict[str, object]) -> str | None:
        for key in ("session_id", "sessionId", "thread_id", "id"):
            value = event.get(key)
            if isinstance(value, str) and value:
                return value
        metadata = event.get("metadata")
        if isinstance(metadata, dict):
            for key in ("session_id", "sessionId", "thread_id", "id"):
                value = metadata.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    @staticmethod
    def _extract_text_value(value: object) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = [CliOutputParser._extract_text_value(item) for item in value]
            return "".join(part for part in parts if part)
        if not isinstance(value, dict):
            return None

        for key in ("text", "content", "response", "result"):
            text = CliOutputParser._extract_text_value(value.get(key))
            if text:
                return text

        value_parts = value.get("parts")
        if isinstance(value_parts, list):
            text_parts = [CliOutputParser._extract_text_value(part) for part in value_parts]
            return "".join(part for part in text_parts if part)
        return None

    @staticmethod
    def _usage_totals_from_gemini_stats(stats: dict[str, object]) -> _UsageTotals:
        usage = stats.get("usageMetadata")
        if isinstance(usage, dict):
            stats = usage

        tokens_in = _first_int(
            stats, "input_tokens", "prompt_tokens", "inputTokenCount", "promptTokenCount"
        )
        cached_tokens_in = _first_int(
            stats, "cached_input_tokens", "cache_read_input_tokens", "cachedContentTokenCount"
        )
        tokens_out = _first_int(
            stats, "output_tokens", "completion_tokens", "outputTokenCount", "candidatesTokenCount"
        )

        if tokens_in == 0 and tokens_out == 0:
            nested = [
                CliOutputParser._usage_totals_from_gemini_stats(value)
                for value in stats.values()
                if isinstance(value, dict)
            ]
            if nested:
                return _UsageTotals(
                    tokens_in=sum(item.tokens_in for item in nested),
                    tokens_out=sum(item.tokens_out for item in nested),
                    cached_tokens_in=sum(item.cached_tokens_in for item in nested),
                    cache_write_tokens_in=sum(item.cache_write_tokens_in for item in nested),
                    max_turn_input_tokens=max(item.max_turn_input_tokens for item in nested),
                )

        return _UsageTotals(
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cached_tokens_in=cached_tokens_in,
            max_turn_input_tokens=tokens_in,
        )


# ---------------------------------------------------------------------------
# Stream event detection
# ---------------------------------------------------------------------------


def _is_claude_result_event(line: bytes) -> bool:
    """Return True if *line* is a Claude Code ``type: "result"`` stream event.

    This is the final event Claude Code emits after completing a response.
    Detecting it lets the idle watcher switch to a short grace period so
    lingering background tasks don't block process exit for 30 minutes.
    """
    if b'"result"' not in line:
        return False
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return False
    return bool(event.get("type") == "result")


# ---------------------------------------------------------------------------
# Backward-compatible free functions
# ---------------------------------------------------------------------------


def _extract_session_id_from_jsonl(raw: str) -> str | None:
    return CliOutputParser.extract_session_id(raw)


def _maybe_parse_usage(line: bytes, current: _UsageTotals) -> _UsageTotals:
    return CliOutputParser.maybe_parse_usage(line, current)


def _usage_totals_from_dict(
    usage: dict[str, object], *, input_includes_cache: bool
) -> _UsageTotals:
    return CliOutputParser.usage_totals_from_dict(usage, input_includes_cache=input_includes_cache)


def _max_usage(left: _UsageTotals, right: _UsageTotals) -> _UsageTotals:
    return CliOutputParser.max_usage(left, right)


def _extract_text_from_codex_jsonl(raw: str) -> tuple[str, _UsageTotals, str | None]:
    return CliOutputParser.parse_codex(raw)


def _extract_text_from_gemini_jsonl(raw: str) -> tuple[str, _UsageTotals, str | None]:
    return CliOutputParser.parse_gemini(raw)


def _extract_text_from_stream_json(raw: str) -> str:
    return CliOutputParser.parse_claude(raw)


def _extract_gemini_session_id(event: dict[str, object]) -> str | None:
    return CliOutputParser._extract_gemini_session_id(event)


def _extract_text_value(value: object) -> str | None:
    return CliOutputParser._extract_text_value(value)


def _usage_totals_from_gemini_stats(stats: dict[str, object]) -> _UsageTotals:
    return CliOutputParser._usage_totals_from_gemini_stats(stats)


def _first_int(values: dict[str, object], *keys: str) -> int:
    for key in keys:
        parsed = _safe_int(values.get(key))
        if parsed:
            return parsed
    return 0


def _safe_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str | bytes | bytearray):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


async def _kill_process(proc: asyncio.subprocess.Process, agent_id: str) -> None:
    """Send SIGTERM, wait up to _SIGKILL_GRACE seconds, then SIGKILL."""
    with contextlib.suppress(ProcessLookupError):
        pgid = os.getpgid(proc.pid)
    if "pgid" not in locals():
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
        await proc.wait()
    finally:
        try:
            os.killpg(pgid, 0)
            ps = await asyncio.create_subprocess_exec(
                "ps",
                "-g",
                str(pgid),
                "-o",
                "pid=",
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
