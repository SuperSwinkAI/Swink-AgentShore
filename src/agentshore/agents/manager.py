"""Agent lifecycle operations: instantiate, dispatch, clear, and authorship tracking."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agentshore.agents import model_resolver
from agentshore.agents.auth_probe import probe_cli_auth
from agentshore.agents.circuit_breaker import CircuitBreaker
from agentshore.agents.cli_agent import dispatch_cli
from agentshore.agents.handle import AgentHandle, AgentInvocationResult, is_noop_invocation
from agentshore.agents.identity import (
    IdentityResolutionError,
    resolve_identity_env,
    resolved_github_login_for_agent,
    verify_identity_repo_access,
)
from agentshore.agents.model_tiers import DEFAULT_MODEL_TIER, effective_model_tier_config
from agentshore.agents.worktree import WorktreeManager
from agentshore.config import AgentConfig
from agentshore.data.store import AgentRecord
from agentshore.errors import (
    AgentAuthError,
    AgentOutputInvalid,
    AgentTimeout,
    ErrorClass,
    OrchestratorError,
    PreconditionFailed,
)
from agentshore.logging import get_logger
from agentshore.state import CLI_AGENT_TYPES, AgentStatus, AgentType, PlayType

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from agentshore.config import RuntimeConfig
    from agentshore.data.store import DataStore

_logger = get_logger(__name__)

_PLACEHOLDER_COLD_START_COST: float = 0.05

_GROK_LAUNCH_WEDGE_MARKERS: tuple[str, ...] = (
    "never produced first byte",
    "launch wedge",
)

_GROK_WEDGE_COOLDOWN_TICKS = 20

_STREAM_HANG_CLUSTER_LIMIT = 3

_BUSY_WATCHDOG_MARGIN_S = 120.0


def _is_grok_launch_wedge_timeout(handle: AgentHandle, exc: BaseException) -> bool:
    if handle.agent_type != AgentType.GROK:
        return False
    if handle.last_error_class != ErrorClass.TIMEOUT_STREAM_IDLE:
        return False
    message = str(exc).lower()
    return all(marker in message for marker in _GROK_LAUNCH_WEDGE_MARKERS)


class AgentManager:
    """Lifecycle manager for all coding agents in an AgentShore session.

    Holds live ``AgentHandle`` objects and per-agent ``CircuitBreaker`` instances.
    Persists every lifecycle event to the ``DataStore``.
    """

    def __init__(
        self,
        *,
        session_id: str,
        store: DataStore,
        cfg: RuntimeConfig,
        working_dir: Path,
        python_executable: str | None = None,
        on_subprocess_spawned: Callable[[str, AgentType, int], Awaitable[None]] | None = None,
        on_subprocess_exited: (
            Callable[[str, AgentType, int, int | None], Awaitable[None]] | None
        ) = None,
        worktree_manager: WorktreeManager | None = None,
    ) -> None:
        self._session_id = session_id
        self._store = store
        self._cfg = cfg
        self._working_dir = working_dir
        if worktree_manager is None:
            from agentshore.agents.worktree import default_worktree_root

            resolved_dir = working_dir.resolve()
            worktree_manager = WorktreeManager(
                session_id=session_id,
                store=store,
                main_repo=resolved_dir,
                worktree_root=default_worktree_root(resolved_dir, cfg),
                cfg=cfg,
            )
        self._worktrees = worktree_manager
        self._python_executable = python_executable
        self._handles: dict[str, AgentHandle] = {}
        self._circuit_breakers: dict[str, CircuitBreaker] = {}
        self._break_recovery_cancels: dict[str, asyncio.Event] = {}
        self.wedge_cooldown_types: set[str] = set()
        self.wedge_cooldown_reasons: dict[str, str] = {}
        self._type_stream_hang_streak: dict[str, int] = {}
        self.branch_exposure: dict[str, str] = {}
        self._on_subprocess_spawned = on_subprocess_spawned
        self._on_subprocess_exited = on_subprocess_exited

        self._measured_cost_total: float = 0.0
        self._measured_play_count: int = 0

        import atexit

        atexit.register(self._kill_tracked_subprocesses_atexit)

    def _kill_tracked_subprocesses_atexit(self) -> None:
        """Best-effort SIGTERM all live agent subprocesses on Python exit.

        Skips any agent whose ``current_play_id`` is non-None — that agent is
        mid-play and the atexit path should not forcibly kill it.  In practice
        the session teardown (drain) has already cancelled in-flight asyncio
        tasks before this fires, but the atexit handler has no asyncio context,
        so we treat a set ``current_play_id`` as the authoritative "still live"
        marker and leave those processes alone rather than risk a mid-play
        SIGTERM that looks like a crash.
        """
        import signal

        for handle in list(self._handles.values()):
            if handle.current_play_id is not None:
                _logger.debug(
                    "atexit_skip_active_play_agent",
                    agent_id=handle.agent_id,
                    current_play_id=handle.current_play_id,
                )
                continue
            proc = getattr(handle, "process", None)
            if proc is None:
                continue
            pid = getattr(proc, "pid", None)
            if pid is None or pid <= 0:
                continue
            try:
                if proc.returncode is None:
                    os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                continue

    async def instantiate(
        self,
        agent_type: AgentType,
        *,
        model_tier: str | None = None,
    ) -> AgentHandle:
        """Create a new agent, register it in the DataStore, and return its handle."""
        agent_id = str(uuid.uuid4())
        agent_cfg = self._cfg.agents.get(agent_type.value, AgentConfig())
        tier = model_tier or DEFAULT_MODEL_TIER
        await model_resolver.ensure_discovered(agent_type)
        tier_cfg = effective_model_tier_config(agent_type, agent_cfg, tier)

        from agentshore.agents.handle import _generate_display_name

        ident_name = agent_cfg.identity
        github_login: str | None = None
        if ident_name:
            try:
                github_login = resolved_github_login_for_agent(self._cfg, agent_cfg)
            except IdentityResolutionError as exc:
                _logger.warning(
                    "agent_identity_validation_failed",
                    agent_type=agent_type.value,
                    identity=ident_name,
                    error=str(exc),
                )

        handle = AgentHandle(
            agent_id=agent_id,
            agent_type=agent_type,
            status=AgentStatus.IDLE,
            working_dir=self._working_dir,
            display_name=_generate_display_name(agent_type, tier_cfg.model),
            model=tier_cfg.model,
            model_tier=tier,
            reasoning_effort=tier_cfg.reasoning_effort,
            github_identity=github_login,
        )

        if agent_type in CLI_AGENT_TYPES and ident_name:
            try:
                identity_env = resolve_identity_env(self._cfg, agent_cfg, strict=True)
                await asyncio.to_thread(
                    verify_identity_repo_access,
                    self._working_dir,
                    identity_env,
                )
            except (IdentityResolutionError, AgentAuthError) as exc:
                handle.last_error_class = ErrorClass.AUTH
                handle.transition_to(AgentStatus.ERROR)
                _logger.warning(
                    "agent_repo_access_validation_failed",
                    agent_id=agent_id,
                    agent_type=agent_type.value,
                    model_tier=tier,
                    github_identity=github_login,
                    error=str(exc),
                )
                self.wedge_cooldown_types.add(agent_type.value)
                self.wedge_cooldown_reasons[agent_type.value] = "identity_auth"
                return handle
            handle.identity_env = identity_env

        cb_cfg = self._cfg.circuit_breaker
        self._circuit_breakers[agent_id] = CircuitBreaker(
            failures=cb_cfg.failures,
            window_seconds=cb_cfg.window_seconds,
            cooldown_seconds=cb_cfg.cooldown_seconds,
        )

        await self._store.register_agent(
            AgentRecord(
                agent_id=agent_id,
                session_id=self._session_id,
                agent_type=agent_type.value,
                created_at=datetime.now(UTC).isoformat(),
                model_tier=handle.model_tier,
                display_name=handle.display_name,
            )
        )

        self._handles[agent_id] = handle
        _logger.info(
            "agent_instantiated",
            agent_id=agent_id,
            agent_type=agent_type.value,
            model=tier_cfg.model,
            model_tier=tier,
            reasoning_effort=tier_cfg.reasoning_effort,
            status=handle.status.value,
        )
        return handle

    async def dispatch(
        self,
        agent_id: str,
        prompt: str,
        *,
        capability: str | None = None,
        play_type: str | None = None,
        cwd_override: Path | None = None,
        resume_session_id: str | None = None,
        first_byte_timeout_override: float | None = None,
        disallowed_tools: tuple[str, ...] = (),
        result_schema: str | None = None,
    ) -> AgentInvocationResult:
        """Route *prompt* to the agent's adapter and return the raw result.

        Updates the circuit breaker, handle counters, and DataStore on every call.
        Raises ``PreconditionFailed`` if the circuit breaker is OPEN.

        ``play_type`` (when supplied) lets the manager pick a per-play
        timeout override from ``RuntimeConfig.play_timeouts`` and surface
        it in the ``agent_dispatch_timeout_classified`` event when the
        dispatch times out (desktop-3fiu).

        ``cwd_override`` (when supplied) replaces ``handle.working_dir`` as
        the subprocess cwd for this single dispatch. The handle itself is
        never mutated — concurrent dispatches on the same handle can each
        target a different worktree. ``AGENTSHORE_PROJECT_PATH`` continues to
        point at the main repo regardless of the override.

        ``disallowed_tools`` carries the executing play's tool-denial policy
        (``Play.disallowed_tools``) down to the CLI adapter, which denies those
        tools at the child's own permission layer if it can express that. The
        manager only forwards it: which tools a play may reach is the play's
        call, not the manager's.
        """
        handle = self._get_handle(agent_id)
        cb = self._circuit_breakers[agent_id]

        if not cb.allows_dispatch:
            raise PreconditionFailed(
                f"Circuit breaker OPEN for agent {agent_id!r} — too many recent failures"
            )

        agent_cfg = self._cfg.agents.get(handle.agent_type.value, AgentConfig())
        effective_timeout = (
            agent_cfg.timeout
            if agent_cfg.timeout is not None
            else self._cfg.effective_play_timeout(play_type)
        )
        dispatch_started_at = time.perf_counter()
        handle.dispatch_deadline_monotonic = (
            time.monotonic() + float(effective_timeout) + _BUSY_WATCHDOG_MARGIN_S
        )
        handle.transition_to(AgentStatus.BUSY)

        handle.dispatches += 1
        await self._store.increment_agent_dispatch_count(agent_id)

        identity_env = dict(handle.identity_env)
        identity_env["AGENTSHORE_PROJECT_PATH"] = str(self._working_dir.resolve())

        try:
            on_spawned = None
            if self._on_subprocess_spawned is not None:
                spawned_cb = self._on_subprocess_spawned

                async def on_spawned(pid: int) -> None:
                    await spawned_cb(handle.agent_id, handle.agent_type, pid)

            on_exited = None
            if self._on_subprocess_exited is not None:
                exited_cb = self._on_subprocess_exited

                async def on_exited(pid: int, exit_code: int | None) -> None:
                    await exited_cb(handle.agent_id, handle.agent_type, pid, exit_code)

            result = await dispatch_cli(
                handle,
                prompt,
                cfg=agent_cfg,
                pricing=self._cfg.pricebook.quote(handle.agent_type.value, handle.model),
                default_timeout=effective_timeout,
                python_executable=self._python_executable,
                identity_env=identity_env,
                on_subprocess_spawned=on_spawned,
                on_subprocess_exited=on_exited,
                cwd_override=cwd_override,
                resume_session_id=resume_session_id,
                first_byte_timeout_override=first_byte_timeout_override,
                disallowed_tools=disallowed_tools,
                result_schema=result_schema,
            )
        except (OrchestratorError, OSError, RuntimeError) as exc:
            cb.record_failure()
            if isinstance(exc, AgentTimeout):
                raw_error_class = getattr(exc, "error_class", ErrorClass.TIMEOUT_TRANSIENT)
                handle.last_error_class = ErrorClass.coerce(raw_error_class)
                if _is_grok_launch_wedge_timeout(handle, exc):
                    self.wedge_cooldown_types.add(handle.agent_type.value)
                    self.wedge_cooldown_reasons[handle.agent_type.value] = "launch_wedge"
                elif handle.last_error_class == ErrorClass.TIMEOUT_STREAM_IDLE:
                    atype = handle.agent_type.value
                    streak = self._type_stream_hang_streak.get(atype, 0) + 1
                    self._type_stream_hang_streak[atype] = streak
                    if streak >= _STREAM_HANG_CLUSTER_LIMIT:
                        self.wedge_cooldown_types.add(atype)
                        self.wedge_cooldown_reasons[atype] = "stream_hang_cluster"
                        self._type_stream_hang_streak[atype] = 0
                handle.timeout_count += 1
                handle.consecutive_timeouts += 1
                if handle.last_error_class == ErrorClass.AUTH:
                    handle.transition_to(AgentStatus.ERROR)
                else:
                    handle.transition_to(AgentStatus.IDLE)
                await self._store.increment_agent_tasks(agent_id, failed=1)
                elapsed_seconds = round(time.perf_counter() - dispatch_started_at, 3)
                _logger.warning(
                    "agent_dispatch_timed_out",
                    agent_id=agent_id,
                    timeout_count=handle.timeout_count,
                    error_class=handle.last_error_class,
                    error=str(exc),
                )
                _logger.warning(
                    "agent_dispatch_timeout_classified",
                    agent_id=agent_id,
                    agent_type=handle.agent_type.value,
                    play_type=play_type,
                    tier=handle.model_tier,
                    elapsed_seconds=elapsed_seconds,
                    effective_timeout=effective_timeout,
                    error_class=handle.last_error_class,
                    timeout_count=handle.timeout_count,
                )
                raise
            if isinstance(exc, AgentOutputInvalid):
                handle.last_error_class = ErrorClass.OUTPUT_INVALID
            handle.transition_to(AgentStatus.ERROR)
            await self._store.increment_agent_tasks(agent_id, failed=1)
            _logger.warning(
                "agent_dispatch_failed",
                agent_id=agent_id,
                error_class=handle.last_error_class,
                error=str(exc),
            )
            raise
        except Exception as exc:
            cb.record_failure()
            handle.last_error_class = ErrorClass.UNKNOWN
            handle.transition_to(AgentStatus.ERROR)
            await self._store.increment_agent_tasks(agent_id, failed=1)
            _logger.warning(
                "agent_dispatch_failed",
                agent_id=agent_id,
                error_class=handle.last_error_class,
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            raise

        cb.record_success()
        handle.consecutive_timeouts = 0
        self._type_stream_hang_streak[handle.agent_type.value] = 0
        if is_noop_invocation(result):
            handle.noop_count += 1
        result = self._apply_placeholder_cost(result, agent_type=handle.agent_type)
        handle.accumulate(
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            dollar_cost=result.dollar_cost,
        )
        handle.transition_to(AgentStatus.IDLE)

        await self._store.update_agent_stats(
            agent_id,
            tokens=result.tokens_in + result.tokens_out,
            cost=result.dollar_cost,
        )
        await self._store.increment_agent_tasks(agent_id, completed=1)

        return result

    def _apply_placeholder_cost(
        self, result: AgentInvocationResult, *, agent_type: AgentType
    ) -> AgentInvocationResult:
        """Bill no-usage agents the running mean measured cost-per-play.

        Grok and Antigravity emit no token-usage block, so the adapter returns a
        parsed cost of $0; charging them nothing drags the session total far
        below reality. A dispatch that reported real usage (``dollar_cost > 0``)
        feeds the running mean and passes through unchanged. A dispatch that
        reported nothing is re-billed at that mean (or a cold-start default until
        the first measured play completes). Keyed on the *absence of measured
        cost*, not the agent type, so a future Grok/agy build that starts
        reporting usage bills its real cost automatically.
        """
        if result.dollar_cost > 0:
            self._measured_cost_total += result.dollar_cost
            self._measured_play_count += 1
            return result
        placeholder = (
            self._measured_cost_total / self._measured_play_count
            if self._measured_play_count > 0
            else _PLACEHOLDER_COLD_START_COST
        )
        _logger.info(
            "dispatch_cost_placeholder",
            agent_type=agent_type.value,
            placeholder_cost=placeholder,
            measured_plays=self._measured_play_count,
        )
        return replace(result, dollar_cost=placeholder)

    def active_play_agent_ids(self) -> frozenset[str]:
        """Return the set of agent IDs that currently have an active in-flight play.

        An agent is considered active when its ``current_play_id`` is non-None
        (set by ``AgentHandle.start_play()`` and cleared by ``AgentHandle.clear_play()``).
        This is the authoritative in-process signal for "agent is mid-work right now"
        and is the cross-check that ``reconcile_state``'s zombie-kill path must consult
        before issuing any ``kill`` to a PID backed by a known agent.
        """
        return frozenset(
            aid for aid, handle in self._handles.items() if handle.current_play_id is not None
        )

    async def clear(self, agent_id: str, *, force: bool = False) -> None:
        """Terminate an agent, persist final stats, and remove it from the manager.

        ``force=False`` (the default): raises ``PreconditionFailed`` when the
        agent still has an active in-flight play (``current_play_id`` is not
        None).  This prevents ``reconcile_state`` and other housekeeping code
        from killing an agent that is legitimately executing a play.

        ``force=True``: skip the active-play guard.  Use this only from
        session-teardown paths (drain, completion mixin) where in-flight asyncio
        tasks have already been cancelled and the session is being wound down.

        Exception: an agent whose in-flight play *is* ``END_AGENT`` is always
        clearable.  The executor marks the retirement target with the end_agent
        play's own marker before the play body runs, so that marker must never
        block the retirement it belongs to (#154).
        """
        handle = self._get_handle(agent_id)

        if (
            not force
            and handle.current_play_id is not None
            and handle.current_play_type is not PlayType.END_AGENT
        ):
            raise PreconditionFailed(
                f"Cannot clear agent {agent_id!r}: it has an active in-flight play "
                f"(current_play_id={handle.current_play_id!r}, "
                f"play_type={handle.current_play_type!r}). "
                "Pass force=True only from session teardown paths."
            )

        def _ms(t0: float) -> float:
            return round((time.perf_counter() - t0) * 1000, 1)

        _t = time.perf_counter()
        proc = handle.process
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            handle.process = None
        _logger.info(
            "agent_clear_step", step="process_terminate", agent_id=agent_id, elapsed_ms=_ms(_t)
        )

        handle.transition_to(AgentStatus.TERMINATED)

        _t = time.perf_counter()
        await self._store.update_agent_terminated(agent_id, datetime.now(UTC).isoformat())
        _logger.info(
            "agent_clear_step", step="db_update_terminated", agent_id=agent_id, elapsed_ms=_ms(_t)
        )

        del self._handles[agent_id]
        del self._circuit_breakers[agent_id]
        self.cancel_break_recovery(agent_id, reason="agent_cleared")
        _logger.info("agent_cleared", agent_id=agent_id)

    def register_break_recovery(self, agent_id: str) -> asyncio.Event:
        """Register (and return) the cancel signal for a pending break recovery.

        Called by ``TakeBreakPlay`` before it sleeps. The event is set by
        ``clear`` when the target agent is torn down, so the play can abandon
        the wait instead of firing against a dead agent ~30 min later. Only one
        break can be pending per agent; a re-registration replaces the previous
        signal (the older play unregisters itself as a no-op).
        """
        event = asyncio.Event()
        self._break_recovery_cancels[agent_id] = event
        return event

    def unregister_break_recovery(self, agent_id: str, event: asyncio.Event) -> None:
        """Drop *event* as the pending break-recovery signal for *agent_id*.

        No-op when a newer registration has replaced it, so a finishing play can
        never unregister a successor's signal.
        """
        if self._break_recovery_cancels.get(agent_id) is event:
            del self._break_recovery_cancels[agent_id]

    def cancel_break_recovery(self, agent_id: str, *, reason: str) -> bool:
        """Fire the pending break-recovery signal for *agent_id*, if any.

        Returns True when a pending break was signalled. Idempotent: the entry
        is popped, so a second call is a no-op.
        """
        event = self._break_recovery_cancels.pop(agent_id, None)
        if event is None:
            return False
        event.set()
        _logger.info("break_recovery_cancelled", agent_id=agent_id, reason=reason)
        return True

    async def _auth_restored(self, handle: AgentHandle) -> bool:
        """Re-check an AUTH-errored agent's credentials after its break.

        Re-resolves the GitHub identity token (refreshing the cached overlay, so
        a `gh auth login` during the break takes effect) and re-probes the CLI
        backend session. Only a definitive probe failure blocks; an
        unprobeable/timeout result lets the agent back to work.
        """
        agent_cfg = self._cfg.agents.get(handle.agent_type.value, AgentConfig())
        env = handle.identity_env
        try:
            if agent_cfg.identity:
                env = resolve_identity_env(self._cfg, agent_cfg, strict=True)
                await asyncio.to_thread(verify_identity_repo_access, self._working_dir, env)
            probe = await asyncio.to_thread(
                probe_cli_auth, handle.agent_type, env, binary=agent_cfg.binary
            )
        except (IdentityResolutionError, AgentAuthError) as exc:
            _logger.warning(
                "agent_recovery_auth_still_failing", agent_id=handle.agent_id, error=str(exc)
            )
            return False
        if probe.blocks_launch:
            _logger.warning(
                "agent_recovery_auth_still_failing", agent_id=handle.agent_id, error=probe.detail
            )
            return False
        handle.identity_env = env
        return True

    async def attempt_recovery(self, agent_id: str) -> bool:
        """Try to transition an ERROR agent back to IDLE if the breaker allows it.

        Returns True when the agent was recovered, False otherwise — including
        when *agent_id* is no longer registered (e.g. cleared by a concurrent
        ``end_agent``/reap while a caller such as ``TakeBreakPlay`` held a stale
        reference across a long sleep, #332). Defense-in-depth: the primary
        guard lives at the ``take_break.py`` call site, which checks the target
        against current state before calling this at all; this fallback just
        ensures an unknown id degrades to "not recovered" here too, rather than
        propagating ``PreconditionFailed`` as an unhandled crash.
        """
        try:
            handle = self._get_handle(agent_id)
        except PreconditionFailed:
            _logger.debug("agent_recovery_skipped_unknown_agent", agent_id=agent_id)
            return False
        cb = self._circuit_breakers[agent_id]

        if handle.status != AgentStatus.ERROR:
            return False
        if handle.last_error_class == ErrorClass.INVALID_MODEL:
            _logger.debug(
                "agent_recovery_skipped_config_error",
                agent_id=agent_id,
                error_class=handle.last_error_class,
            )
            return False
        if handle.last_error_class == ErrorClass.AUTH and not await self._auth_restored(handle):
            return False

        if cb.allows_dispatch:
            cb.record_recovery_attempt()
            handle.last_error_class = None
            handle.transition_to(AgentStatus.IDLE)
            _logger.info(
                "agent_recovered",
                agent_id=agent_id,
                recovery_attempts=cb._recovery_attempts,
            )
            return True

        return False

    async def mark_agent_error(
        self,
        agent_id: str,
        error_class: ErrorClass | str,
        reason: str,
        *,
        increment_failed: bool = False,
    ) -> None:
        """Attach a semantic runtime/config failure to one concrete agent.

        ``error_class`` accepts a bare string at the boundary (current callers
        pass ``"auth"``) and is coerced to :class:`ErrorClass`, collapsing any
        unrecognised value to :attr:`ErrorClass.UNKNOWN`.
        """
        handle = self._get_handle(agent_id)
        cb = self._circuit_breakers[agent_id]
        cb.record_failure()
        coerced = ErrorClass.coerce(error_class)
        error_class = coerced
        handle.last_error_class = coerced
        handle.transition_to(AgentStatus.ERROR)
        if increment_failed:
            await self._store.increment_agent_tasks(agent_id, failed=1)
        _logger.warning(
            "agent_marked_error",
            agent_id=agent_id,
            agent_type=handle.agent_type.value,
            model_tier=handle.model_tier,
            model=handle.model,
            github_identity=handle.github_identity,
            error_class=error_class,
            reason=reason[:500],
        )

    def record_branch_exposure(self, branch: str, agent_id: str) -> None:
        """Record that *agent_id* last worked on *branch* (branch-exposure signal)."""
        self.branch_exposure[branch] = agent_id

    def record_branch_commit(self, branch: str, agent_id: str, sha: str) -> None:
        """Record that *agent_id* committed *sha* to *branch*."""
        self.branch_exposure[branch] = agent_id
        _logger.debug("branch_commit_recorded", branch=branch, agent_id=agent_id, sha=sha)

    def get_handle(self, agent_id: str) -> AgentHandle:
        """Return the live handle for *agent_id*; raises ``PreconditionFailed`` if unknown."""
        return self._get_handle(agent_id)

    @property
    def worktrees(self) -> WorktreeManager:
        return self._worktrees

    @property
    def handles(self) -> dict[str, AgentHandle]:
        return self._handles

    @property
    def circuit_breakers(self) -> dict[str, CircuitBreaker]:
        return self._circuit_breakers

    def _get_handle(self, agent_id: str) -> AgentHandle:
        try:
            return self._handles[agent_id]
        except KeyError:
            raise PreconditionFailed(f"Unknown agent_id: {agent_id!r}") from None
