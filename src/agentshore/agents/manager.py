"""Agent lifecycle operations: instantiate, dispatch, clear, and authorship tracking."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agentshore.agents.circuit_breaker import CircuitBreaker
from agentshore.agents.cli_agent import dispatch_cli
from agentshore.agents.handle import AgentHandle, AgentInvocationResult
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
from agentshore.state import AgentStatus, AgentType

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from agentshore.config import RuntimeConfig
    from agentshore.data.store import DataStore

_logger = get_logger(__name__)

_CLI_AGENT_TYPES: frozenset[AgentType] = frozenset(
    {AgentType.CLAUDE_CODE, AgentType.CODEX, AgentType.GEMINI, AgentType.GROK}
)


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
        # WorktreeManager: when not injected, build one anchored on the
        # canonical worktree root (project-local ``<repo>/.agentshore/worktrees/``
        # by default, or ``cfg.worktrees.root`` when set). Constructed lazily-
        # cheap, no I/O — heavy work happens inside allocate_for_dispatch.
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
        # Test shim: when set, CLI dispatch invokes cfg.binary as a Python script
        # through this interpreter rather than executing the binary directly.
        self._python_executable = python_executable
        self._handles: dict[str, AgentHandle] = {}
        self._circuit_breakers: dict[str, CircuitBreaker] = {}
        # Phase-1 in-memory cache — written by record_branch_exposure / record_branch_commit,
        # read by _selection.py to bias away from branch-exposed agents.
        self.branch_exposure: dict[str, str] = {}  # branch → agent_id
        self._on_subprocess_spawned = on_subprocess_spawned
        self._on_subprocess_exited = on_subprocess_exited

        # Safety net for desktop-ieql: if the sidecar process dies for any
        # reason (signal, crash, lost stdio pipe) without the Tauri
        # shell's RunEvent::ExitRequested handler getting to
        # kill_all_agents first, atexit fires and we walk the tracked
        # subprocess PIDs and SIGTERM them. SIGKILL doesn't run atexit
        # so this isn't a complete guarantee, but it covers
        # graceful-shutdown paths the Rust side might miss (SIGTERM
        # from OS sleep, manual `kill -TERM <sidecar_pid>`, etc.).
        import atexit

        atexit.register(self._kill_tracked_subprocesses_atexit)

    def _kill_tracked_subprocesses_atexit(self) -> None:
        """Best-effort SIGTERM all live agent subprocesses on Python exit."""
        import signal

        for handle in list(self._handles.values()):
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
                # Already dead or never spawned — fine.
                continue

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

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

        # Resolve the identity overlay exactly once and verify repo access
        # *before* registering the handle into circuit breakers / DataStore /
        # _handles. On preflight failure we mark the (unregistered) handle ERROR
        # and return it without leaving a half-constructed agent live in the
        # manager. On success the validated overlay is cached on the handle so
        # dispatch() never re-resolves the token or re-runs `gh repo view`.
        if agent_type in _CLI_AGENT_TYPES and ident_name:
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
        """
        handle = self._get_handle(agent_id)
        cb = self._circuit_breakers[agent_id]

        if not cb.allows_dispatch:
            raise PreconditionFailed(
                f"Circuit breaker OPEN for agent {agent_id!r} — too many recent failures"
            )

        agent_cfg = self._cfg.agents.get(handle.agent_type.value, AgentConfig())
        # Per-play-type timeout overrides (desktop-3fiu). ``play_timeouts``
        # only fires when there's no explicit ``AgentConfig.timeout``;
        # per-agent overrides still win to preserve existing behaviour.
        effective_timeout = (
            agent_cfg.timeout
            if agent_cfg.timeout is not None
            else self._cfg.effective_play_timeout(play_type)
        )
        dispatch_started_at = time.perf_counter()
        handle.transition_to(AgentStatus.BUSY)

        # desktop-31h2: Increment cumulative dispatch counter before invoking
        # the adapter. Counted regardless of success/failure/timeout so the
        # per-agent `dispatch_share` reflects work attempts, not just
        # completions. ``increment_agent_tasks`` (further down) still tracks
        # the verdict-based counters separately. The handle's ``dispatches``
        # counter is also bumped here so ``build_agent_snapshots`` can read
        # the live value without a per-tick DB round-trip (cli_agent.py used
        # to bump this only on CLI dispatches; centralising it in the
        # manager makes API agents tracked too).
        handle.dispatches += 1
        await self._store.increment_agent_dispatch_count(agent_id)

        # The identity overlay was resolved and repo-access-verified once at
        # instantiate(); reuse the cached copy rather than re-shelling `gh` on
        # the dispatch hot path. Copy before adding the per-dispatch
        # AGENTSHORE_PROJECT_PATH key so the handle's cached overlay stays pristine.
        identity_env = dict(handle.identity_env)
        # Inject the canonical absolute project root so skill agents can anchor
        # `MAIN_REPO` against a value AgentShore controls instead of the
        # subprocess's pwd (which can be a leftover worktree path). See
        # agentshore-issue-pickup/SKILL.md and siblings for the
        # `${AGENTSHORE_PROJECT_PATH:-$(pwd)}` consumer pattern.
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
                default_timeout=effective_timeout,
                python_executable=self._python_executable,
                identity_env=identity_env,
                on_subprocess_spawned=on_spawned,
                on_subprocess_exited=on_exited,
                cwd_override=cwd_override,
                resume_session_id=resume_session_id,
            )
        except (OrchestratorError, OSError, RuntimeError) as exc:
            cb.record_failure()
            if isinstance(exc, AgentTimeout):
                raw_error_class = getattr(exc, "error_class", ErrorClass.TIMEOUT_TRANSIENT)
                # PlayTimeoutError.error_class carries the precise timeout
                # sub-class (timeout_wallclock / _stream_idle / _post_response);
                # a bare AgentTimeout has no attribute, so the default applies.
                # Coerce to ErrorClass, collapsing any unexpected value to
                # UNKNOWN rather than persisting an unclassified string.
                handle.last_error_class = (
                    ErrorClass(raw_error_class)
                    if raw_error_class in ErrorClass._value2member_map_
                    else ErrorClass.UNKNOWN
                )
                handle.timeout_count += 1
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
                # desktop-3fiu: classified companion event so the
                # histogram of timeouts can be sliced by play type +
                # tier without re-parsing the freeform ``error`` field.
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

        cb.record_success()
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

    async def clear(self, agent_id: str) -> None:
        """Terminate an agent, persist final stats, and remove it from the manager."""
        handle = self._get_handle(agent_id)

        def _ms(t0: float) -> float:
            return round((time.perf_counter() - t0) * 1000, 1)

        # Kill any running subprocess. Capture the handle's process into a
        # local; a concurrent dispatch_cli finally-block can null
        # handle.process between the guard and the second .returncode read,
        # which used to raise `'NoneType' object has no attribute 'returncode'`.
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
        _logger.info("agent_cleared", agent_id=agent_id)

    # -------------------------------------------------------------------------
    # Error recovery
    # -------------------------------------------------------------------------

    async def attempt_recovery(self, agent_id: str) -> bool:
        """Try to transition an ERROR agent back to IDLE if the breaker allows it.

        Returns True when the agent was recovered, False otherwise.
        """
        handle = self._get_handle(agent_id)
        cb = self._circuit_breakers[agent_id]

        if handle.status != AgentStatus.ERROR:
            return False
        if handle.last_error_class in {ErrorClass.AUTH, ErrorClass.INVALID_MODEL}:
            _logger.debug(
                "agent_recovery_skipped_config_error",
                agent_id=agent_id,
                error_class=handle.last_error_class,
            )
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
        coerced = (
            ErrorClass(error_class)
            if error_class in ErrorClass._value2member_map_
            else ErrorClass.UNKNOWN
        )
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

    # -------------------------------------------------------------------------
    # Branch tracking (written here, read by _selection.py for branch exposure affinity)
    # -------------------------------------------------------------------------

    def record_branch_exposure(self, branch: str, agent_id: str) -> None:
        """Record that *agent_id* last worked on *branch* (branch-exposure signal)."""
        self.branch_exposure[branch] = agent_id

    def record_branch_commit(self, branch: str, agent_id: str, sha: str) -> None:
        """Record that *agent_id* committed *sha* to *branch*."""
        self.branch_exposure[branch] = agent_id
        _logger.debug("branch_commit_recorded", branch=branch, agent_id=agent_id, sha=sha)

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

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
