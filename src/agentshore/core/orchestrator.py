"""``Orchestrator`` — the AgentShore RL loop, a composition root.

The class declaration is intentionally minimal: it inherits only
``_OrchestratorBase`` (which provides ``__init__`` and constructs every owned
component/collaborator) and delegates each public method to the component that
owns the behaviour. The 7-mixin MRO has been fully dissolved into composition
(TNQA 03 C2): the orchestrator now *owns* its components as ``self._loop``,
``self._dispatcher``, ``self._completion``, ``self._drain``, ``self._lifecycle``,
``self._state_builder``, ``self._snapshots`` and forwards to them.

Behavioural code lives in the components so each file stays under the LOC
budget; the public-API methods that are short and not naturally grouped with a
single responsibility live here, plus the thin delegators that keep the host
Protocols' cross-component method references (``run_until_idle``,
``_initiate_autonomous_stop``, ``_check_stagnation_escalation``,
``start_loop_liveness_watchdog``, ``stop_loop_liveness_watchdog``) resolving on
the composition root.
"""

from __future__ import annotations

import time
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING

from agentshore.agents.health import HealthMonitor
from agentshore.config.models import PolicyMode
from agentshore.core.base import _OrchestratorBase
from agentshore.core.helpers import (
    _bootstrap_phase_publisher,
    _emit_weights_dir_inventory,
)
from agentshore.core.mixins.drain import SHUTDOWN_GRACE_PERIOD_SECONDS

# NOTE: bootstrap calls phase functions via the ``phases`` module object
# (imported lazily inside ``bootstrap``) so tests patch them at their binding
# home, ``agentshore.core.phases._phase_X``. ``setup_logging`` is patched at
# ``agentshore.core.orchestrator.setup_logging``.
from agentshore.data.store import SessionRecord
from agentshore.logging import setup_logging
from agentshore.paths import project_db_path, project_dir
from agentshore.plays.base import PlayParams
from agentshore.plays.override import OverrideEntry, OverrideKind
from agentshore.rl.mask_reason import MaskClassification
from agentshore.state import (
    NullStateProvider,
    OrchestratorState,
    PlayType,
)
from agentshore.utils import now_iso

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from agentshore.config import RuntimeConfig
    from agentshore.plays.selector import PlaySelector
    from agentshore.state import (
        PlayOutcome,
        StateProvider,
    )

    NaturalExitCallback = Callable[[str], Awaitable[None]]


class Orchestrator(_OrchestratorBase):
    """The AgentShore RL loop: observe → select → execute → repeat.

    Usage::

        async with await Orchestrator.bootstrap(cfg=cfg, repo_root=...) as orch:
            await orch.run_until_idle()
    """

    @classmethod
    async def bootstrap(
        cls,
        *,
        cfg: RuntimeConfig,
        repo_root: Path,
        seed_path: Path | None = None,
        selector: PlaySelector | None = None,
        state_provider: StateProvider | None = None,
        session_id: str | None = None,
        policy_path: Path | None = None,
        policy_mode: PolicyMode = PolicyMode.LEARNING,
        config_path: Path | None = None,
        embedded_mode: bool = False,
    ) -> Orchestrator:
        """Construct and wire all components.

        Returns an Orchestrator ready to use as an async context manager.

        The bootstrap pipeline is split into named phases so each can be
        unit-tested in isolation. Phase ordering is load-bearing — DB must
        exist before manager + executor; metrics must exist before PPO
        selector; the session row must be inserted before any FK-referencing
        write (skills install, GitHub cache, learnings load) runs.
        """
        sid = session_id or str(uuid.uuid4())

        # Setup logging first so all subsequent steps emit structured logs.
        # Phase functions are reached via the ``phases`` module object so tests
        # patch them at ``agentshore.core.phases._phase_X``.
        from agentshore.core import phases

        log_path = (
            repo_root / cfg.logging.log_dir / f"agentshore-{sid}.log" if cfg.logging.file else None
        )
        setup_logging(
            level=cfg.logging.level,
            log_dir=log_path.parent if log_path is not None else None,
            session_id=sid,
        )

        # A transient seed_path (CLI --seed / sidecar session.start) always
        # wins; otherwise fall back to the persisted ``intake.seed_paths`` so
        # every start path (CLI, sidecar, desktop Quick Start, TUI) honors a
        # configured seed. (policy_path has the analogous fallback inside
        # ``_resolve_policy_path``.) Resolved once and threaded everywhere.
        effective_seed = phases._resolve_seed_path(cfg, seed_path, repo_root)

        provider: StateProvider = state_provider or NullStateProvider()

        async def _publish_bootstrap_phase(phase: str, status: str, elapsed_ms: float) -> None:
            await provider.on_bootstrap_phase(phase, status, elapsed_ms)

        token = _bootstrap_phase_publisher.set(_publish_bootstrap_phase)
        try:
            store = await phases._phase_init_datastore(repo_root)
            await phases._phase_reset_session_scoped_tables(store)
            manager, gh, executor, registry = await phases._phase_init_executor(
                cfg=cfg, repo_root=repo_root, sid=sid, store=store, provider=provider
            )

            # Selector is set to a temporary placeholder; PPO init below replaces it
            # unless a test explicitly passes a selector.
            orch = cls(
                cfg=cfg,
                repo_root=repo_root,
                session_id=sid,
                store=store,
                manager=manager,
                executor=executor,
                selector=selector,
                state_provider=provider,
            )
            orch._seed_path = effective_seed
            orch._config_path = config_path
            orch._registry = registry
            orch._embedded_mode = embedded_mode
            orch._log_path = log_path

            # Wire the requeue callback now that orch owns the override queue.
            executor._requeue_callback = lambda pt, p: orch._overrides.put_nowait(
                OverrideEntry(
                    play_type=pt,
                    params=p,
                    kind=OverrideKind.EXECUTOR_REQUEUE,
                    enqueue_classification=MaskClassification.TRANSIENT,
                )
            )

            await phases._phase_init_metrics(orch=orch, cfg=cfg, store=store, sid=sid)
            _emit_weights_dir_inventory(orch._weights_dir(), phase="session_start")
            phases._phase_cleanup_stale_weights(repo_root)
            if selector is None:
                await phases._phase_init_ppo_selector(
                    orch=orch,
                    cfg=cfg,
                    executor=executor,
                    registry=registry,
                    policy_path=policy_path,
                    policy_mode=policy_mode,
                )

            await phases._phase_create_session_row(
                store=store, sid=sid, repo_root=repo_root, seed_path=effective_seed
            )
            # desktop-12g9: instantiate the worktree manager and reap any
            # leftovers from prior sessions before dispatch opens. The manager
            # must be in place before any FK-referencing worktree row inserts
            # (A2's dispatch wiring), and the sweep must happen after the
            # current session row exists so list_orphans correctly excludes it.
            await phases._phase_init_worktree_manager(
                orch=orch, cfg=cfg, store=store, sid=sid, repo_root=repo_root
            )
            await phases._phase_session_start_worktree_sweep(orch=orch, sid=sid)
            await phases._phase_clear_beads_in_progress(repo_root=repo_root, sid=sid)
            # Snapshot pre-session dirty trunk state before _phase_git_safety_sweep
            # restores any branch state — RECONCILE_STATE uses this sidecar to
            # attribute dirty paths to prior sessions even when the DB/log was
            # recovered or rotated.
            await phases._phase_session_start_dirty_baseline(repo_root=repo_root, sid=sid)
            # desktop-kqo5: cache default branch + sweep main-repo HEAD before
            # opening dispatch. Must run before _phase_install_skills so the
            # cached value is available to any phase that needs it.
            await phases._phase_git_safety_sweep(orch=orch, repo_root=repo_root, sid=sid)
            phases._phase_install_skills(repo_root)
            await phases._phase_fetch_github(
                gh=gh, store=store, sid=sid, cfg=cfg, repo_root=repo_root
            )
            # Stamp the refresh clock so the first _build_state tick doesn't
            # immediately re-run _refresh_issues (bootstrap already fetched).
            orch._last_refresh_time = time.monotonic()
            await phases._phase_ensure_labels(gh=gh, cfg=cfg)
            await phases._phase_load_learnings(cfg=cfg, repo_root=repo_root)
            if selector is None:
                open_issues_at_bootstrap = await store.get_open_issues(sid)
                phases._phase_queue_agent_instantiation(
                    orch=orch,
                    cfg=cfg,
                    seed_path=effective_seed,
                    open_issues_count=len(open_issues_at_bootstrap),
                )

            with suppress(Exception):
                await _publish_bootstrap_phase("ready", "completed", 0.0)
            return orch
        finally:
            _bootstrap_phase_publisher.reset(token)

    # -------------------------------------------------------------------------
    # Async context manager
    # -------------------------------------------------------------------------

    async def __aenter__(self) -> Orchestrator:
        # Session row is created during bootstrap (before FK-referencing inserts).
        # If bootstrap was skipped (e.g., tests using __new__), create it now.
        existing = await self._store.get_session(self._session_id)
        if existing is None:
            await self._store.create_session(
                SessionRecord(
                    session_id=self._session_id,
                    project_path=str(self._repo_root),
                    started_at=now_iso(),
                    status="running",
                    seed_path=str(getattr(self, "_seed_path", None) or ""),
                )
            )
        await self._store.abandon_unfinished_plays(
            self._session_id,
            reason="unfinished play abandoned during session startup recovery",
        )
        await self._store.abandon_active_work_claims(self._session_id)

        self._health = HealthMonitor(
            handles=self._manager.handles,
            circuit_breakers=self._manager.circuit_breakers,
            on_crash=self._completion.on_crash,
            on_context_pressure=self._completion.on_context_pressure,
        )
        self._health.start()

        # Loop-liveness watchdog (#9): an independent task that force-drains the
        # session if the core loop heartbeat goes stale (a hard freeze the
        # idle/unanswered-pause backstops cannot catch). No-op when disabled via
        # feedback.loop_liveness_timeout_seconds = null. Started here, before the
        # loop runs, and cancelled during _stop_inner.
        self.start_loop_liveness_watchdog()

        # desktop-gkku: keep the OS from idling our process while a session
        # is active. macOS holds an IOPMAssertion (PreventUserIdleSystemSleep,
        # which keeps I/O priority normal and prevents the screen-lock
        # corruption window from desktop-tvsb). Windows holds
        # SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED). Linux
        # and other platforms get a no-op.
        from agentshore.power import acquire as _acquire_power

        self._power_assertion = _acquire_power("AgentShore session active")

        # desktop-jc7p: defense-in-depth against silent SQLite corruption.
        # Canary runs PRAGMA quick_check on a schedule, snapshot ring keeps a
        # known-good image alongside the live DB, restore_from_snapshot_ring
        # auto-swaps at next startup if quick_check fails on the main file.
        integrity_cfg = self._cfg.data_integrity
        if integrity_cfg.enabled:
            from agentshore.data.integrity import IntegrityMonitor

            self._integrity = IntegrityMonitor(
                self._store,
                project_dir(self._repo_root),
                db_path=project_db_path(self._repo_root),
                canary_interval_seconds=float(integrity_cfg.canary_interval_seconds),
                snapshot_interval_seconds=float(integrity_cfg.snapshot_interval_seconds),
                snapshot_ring_size=integrity_cfg.snapshot_ring_size,
                wal_checkpoint_interval_seconds=float(
                    integrity_cfg.wal_checkpoint_interval_seconds
                ),
            )
            self._integrity.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def run_play(
        self,
        play_type: PlayType,
        params: PlayParams | None = None,
    ) -> PlayOutcome:
        """Execute a single play synchronously (for tests and direct invocation)."""
        state = await self._state_builder.build_state()
        return await self._executor.execute(
            play_type,
            state,
            override=params or PlayParams(),
        )

    async def pause(self, reason: str = "user_request") -> None:
        """Pause the orchestrator loop after the current play completes."""
        await self._lifecycle.pause(reason)

    async def resume(self, override_budget: bool = False) -> None:
        """Resume the orchestrator loop after a pause."""
        await self._lifecycle.resume(override_budget)

    async def reload_config(self) -> None:
        """Reload configuration from the configured path."""
        await self._lifecycle.reload_config()

    def request_stop(self, reason: str = "stop_requested") -> None:
        """Signal the orchestrator to stop at the next loop iteration."""
        self._drain.request_stop(reason)

    def request_drain(self, reason: str = "signal_sigterm") -> None:
        """Schedule a graceful drain from a sync context (e.g. signal handler)."""
        self._drain.request_drain(reason)

    def request_end_session_report(self, *, open_browser: bool = True) -> None:
        """Request a shutdown-time end-of-session report for this session."""
        self._drain.request_end_session_report(open_browser=open_browser)

    def register_esr_ready_callback(
        self, callback: Callable[[str, str, str | None], None] | None
    ) -> None:
        """Wire a callback fired when the in-shutdown ESR file becomes available."""
        self._drain.register_esr_ready_callback(callback)

    async def begin_drain(self, reason: str) -> None:
        """Start graceful drain: only end_agent is dispatched until agents stop."""
        await self._drain.begin_drain(reason)

    async def hard_stop(self) -> None:
        """Immediate forced shutdown — cancels in-flight plays and kills agents."""
        await self._drain.hard_stop()

    def adjust_budget(self, delta_usd: float) -> bool:
        """Increase session budget; return True when a budget pause should resume."""
        return self._drain.adjust_budget(delta_usd)

    async def stop(self, grace_period_s: float = SHUTDOWN_GRACE_PERIOD_SECONDS) -> None:
        """Gracefully shut down the orchestrator."""
        await self._drain.stop(grace_period_s)

    async def publish_initial_state(self) -> OrchestratorState:
        """Publish and return the current state snapshot."""
        state = await self._state_builder.build_state()
        await self._safe_call(
            self._state_provider.on_state_update(state),
            "on_state_update_initial",
        )
        return state

    def on_natural_exit(self, callback: NaturalExitCallback) -> None:
        """Register a callback fired when the loop exits without an explicit stop.

        Natural exit is a ``_should_terminate`` return of ``should_stop=True``
        with a reason other than ``"stop_requested"`` (drain_complete,
        max_plays, timeout, shutting_down). The callback is awaited from
        ``run_until_idle``'s exit branch and receives the termination reason
        as its only argument. The sidecar boot wrapper uses it to fire
        ``session.completed`` over the JSON-RPC stdio transport (DESIGN §5.2).
        """
        self._natural_exit_callback = callback

    async def run_until_idle(self) -> None:
        """Drive the RL loop until selector returns None or a stop is requested.

        The public entry point (``async with ... as orch: await
        orch.run_until_idle()``); forwards to the owned :class:`LoopRunner`.
        """
        await self._loop.run_until_idle()
