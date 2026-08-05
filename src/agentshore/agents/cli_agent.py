"""CLI agent adapter — asyncio subprocess dispatch for supported CLI agents.

The top-level ``dispatch_cli`` function coordinates two injected boundaries:
provider-specific ``CliDriver`` hooks and a ``ProcessSupervisor`` that owns the
child lifecycle. Pure concern groups live in the ``agents/cli/`` sub-package:

    agents/cli/errors.py    — marker tables + _classify_error / _process_error_detail
    agents/cli/argv.py      — build_argv / build_resume_argv + platform helpers
    agents/cli/watchdogs.py — _StdoutActivity / _StderrSniffer / _watch_* coroutines
    agents/cli/parsing.py   — _PARSERS / CliOutputFormat / _extract_* / _ReadOutput
    agents/cli/drivers.py   — provider prepare/finalize hooks
    agents/cli/supervisor.py — spawn/input/callback/cleanup lifecycle
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from agentshore import subprocess_env
from agentshore.agents.cli import argv as cli_argv
from agentshore.agents.cli import supervisor as process_supervisor
from agentshore.agents.cli.drivers import DEFAULT_CLI_DRIVERS, CliDriverRegistry
from agentshore.agents.cli.supervisor import (
    ProcessRunner,
    ProcessRunRequest,
)
from agentshore.agents.cli.watchdogs import _DispatchArgv
from agentshore.agents.costs import estimate_cost
from agentshore.agents.handle import AgentInvocationResult
from agentshore.agents.pricing import PricingQuote, default_quote
from agentshore.beads import ensure_bd_on_agent_path
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
_ARGV_PREVIEW_MAX_CHARS = 256  # log clamp; full prompt is reconstructible from skill+params


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
    pinned_session_id: str | None = None,
    disallowed_tools: tuple[str, ...] = (),
) -> _DispatchArgv:
    """Build the subprocess argv list and log-preview fields for a single dispatch.

    Encapsulates the test-shim path (``python_executable``), the normal
    ``build_argv`` path, and the narrow JSON-retry ``--resume`` override
    (desktop-dy2j). *prompt_file*, when set, routes a Grok dispatch's prompt
    through ``--prompt-file`` instead of an argv element (issue #160).
    *pinned_session_id*, when set, pins the new run's durable session id for the
    CLIs that support it (see ``_PINNABLE_SESSION_AGENT_TYPES``); it is never
    forwarded on the resume path, which already has an id.
    *disallowed_tools* carries the executing play's tool denials and — unlike
    the pin — IS forwarded on the resume path, since the retry continues the
    same play. The test-shim path ignores it: the shim is a Python mock with no
    tool surface to deny.
    """
    prompt_on_stdin = cli_argv._prompt_on_stdin(python_executable)
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
    elif resume_session_id is not None and handle.agent_type in cli_argv._RESUMABLE_AGENT_TYPES:
        # desktop-dy2j: narrow JSON-retry re-entry of the prior session so the
        # agent emits the structured trailer it missed. Per-agent resume shape.
        argv = cli_argv.build_resume_argv(
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
            model_tier=handle.model_tier,
            disallowed_tools=disallowed_tools,
        )
    else:
        argv = cli_argv.build_argv(
            handle.agent_type,
            prompt,
            binary=cfg.binary,
            model=handle.model or cfg.model,
            reasoning_effort=handle.reasoning_effort or cfg.reasoning_effort,
            extra_flags=cfg.extra_flags,
            project_dir=str(effective_cwd),
            prompt_on_stdin=prompt_on_stdin,
            prompt_file=prompt_file,
            model_tier=handle.model_tier,
            session_id=pinned_session_id,
            disallowed_tools=disallowed_tools,
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
    disallowed_tools: tuple[str, ...] = (),
    supervisor: ProcessRunner | None = None,
    driver_registry: CliDriverRegistry = DEFAULT_CLI_DRIVERS,
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
    disallowed_tools:
        The executing play's tool-denial policy (``Play.disallowed_tools``),
        passed to the CLI so the named tools are denied at its own permission
        layer. Honoured by the agent types in
        ``_TOOL_DENIAL_CAPABLE_AGENT_TYPES`` and accepted-but-ignored by the
        rest. Empty (the default) leaves the agent's full tool surface intact.
    supervisor:
        Injectable owner of subprocess spawn, callbacks, watchdog completion,
        and cleanup. Production constructs the default supervisor; tests can
        supply a fake without patching process globals.
    driver_registry:
        Provider-specific prepare/finalize hooks. The default registry contains
        one driver for every supported CLI agent type.

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

    # Build the subprocess environment before the provider driver creates any
    # temporary resource, so preparation cleanup only needs to surround argv
    # construction and process execution.
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
    env = ensure_bd_on_agent_path(env)

    driver = driver_registry.driver_for(handle.agent_type)
    preparation = driver.prepare(
        prompt,
        python_executable=python_executable,
        resume_session_id=resume_session_id,
    )

    _argv = _build_dispatch_argv(
        handle,
        prompt,
        cfg=cfg,
        python_executable=python_executable,
        resume_session_id=resume_session_id,
        effective_cwd=effective_cwd,
        prompt_file=(
            str(preparation.prompt_file) if preparation.prompt_file is not None else None
        ),
        pinned_session_id=preparation.pinned_session_id,
        disallowed_tools=disallowed_tools,
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

    # Resolve npm-shim agent binaries (codex.cmd etc.) to a full path so they
    # spawn on Windows; CreateProcess only finds bare names ending in .exe.
    argv = cli_argv._resolve_executable(argv)

    # On Windows the prompt is fed over stdin to dodge the cmd.exe command-line
    # limit (see build_argv); elsewhere stdin stays closed. Two exceptions keep
    # stdin closed because they never read the prompt from it: Grok (it's in
    # --prompt-file) and Antigravity (``agy`` has no stdin mode — the prompt is
    # always in ``-p``). Opening a PIPE and writing a prompt the child never
    # drains could block on a full pipe buffer.
    prompt_on_stdin = (
        cli_argv._prompt_on_stdin(python_executable)
        and preparation.prompt_file is None
        and handle.agent_type != AgentType.ANTIGRAVITY
    )

    process_runner = supervisor or process_supervisor.ProcessSupervisor()
    request = ProcessRunRequest(
        argv=tuple(argv),
        cwd=effective_cwd,
        env=env,
        prompt=prompt,
        prompt_on_stdin=prompt_on_stdin,
        # A Python test shim has no terminal dependency and must stay on pipes.
        allow_conpty=python_executable is None,
        max_bytes=max_bytes,
        stream_idle_timeout=stream_idle_timeout,
        timeout=float(timeout),
        prompt_bytes=prompt_bytes,
        dispatch_start=t_start,
        first_byte_timeout_override=first_byte_timeout_override,
    )
    try:
        supervised = await process_runner.run(
            request,
            handle=handle,
            cfg=cfg,
            on_subprocess_spawned=on_subprocess_spawned,
            on_subprocess_exited=on_subprocess_exited,
        )
    finally:
        driver.cleanup(preparation)

    proc = supervised.process
    post_response_killed = supervised.post_response_killed
    stderr_sniffer = supervised.stderr_sniffer
    raw_output, usage, observed_session_id = (
        supervised.output.raw,
        supervised.output.usage,
        supervised.output.session_id,
    )

    duration_ms = int((time.monotonic() - t_start) * 1000)

    provider_output = driver.finalize(
        raw_output,
        observed_session_id,
        preparation=preparation,
        effective_cwd=effective_cwd,
        env=env,
    )
    raw_output = provider_output.raw_output
    observed_session_id = provider_output.session_id

    rc = proc.returncode
    if rc != 0 and not post_response_killed:
        recovered = await process_supervisor.finalize_nonzero_exit(
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
    process_supervisor.close_process_transport(proc)
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
        session_id=observed_session_id,
    )
