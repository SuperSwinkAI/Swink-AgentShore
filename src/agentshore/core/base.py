"""Base class for :class:`agentshore.core.Orchestrator` mixins.

``_OrchestratorBase`` is the rightmost entry in the ``Orchestrator`` MRO and
is the only base that defines ``__init__``. It declares the runtime
attributes that every mixin reads via ``self._store``/``self._runtime``/etc. as
class-level annotations so mypy can resolve cross-mixin lookups without
circular type imports.

It also hosts a handful of plain readonly accessors (``_weights_dir``,
``_selector_config_index``, the rolling-velocity / executor-skip-rate
providers) that other mixins call but that have no behavioural state of
their own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.config.models import BudgetConfig
from agentshore.core.helpers import _logger
from agentshore.core.main_repo_guard import MainRepoGuard
from agentshore.core.mixins.completion import CompletionProcessor
from agentshore.core.mixins.dispatch import Dispatcher
from agentshore.core.mixins.drain import DrainController
from agentshore.core.mixins.lifecycle import LifecycleController
from agentshore.core.mixins.loop import LoopRunner
from agentshore.core.mixins.snapshots import SnapshotProjector
from agentshore.core.mixins.state import StateBuilder
from agentshore.core.override_queue import OverrideQueue
from agentshore.core.recovery_tracker import RecoveryTracker
from agentshore.core.session_runtime import SessionRuntime
from agentshore.core.velocity_tracker import VelocityTracker
from agentshore.paths import project_weights_dir
from agentshore.state import NullStateProvider

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from agentshore.agents.manager import AgentManager
    from agentshore.config import RuntimeConfig
    from agentshore.data.store import DataStore
    from agentshore.plays.executor import PlayExecutor
    from agentshore.plays.selector import PlaySelector
    from agentshore.rl.config_head import ConfigKey
    from agentshore.state import (
        OrchestratorState,
        StateProvider,
    )

    NaturalExitCallback = Callable[[str], Awaitable[None]]


class _OrchestratorBase:
    """Holds every ``self.*`` attribute set in ``Orchestrator.__init__``.

    Mixins reference these attributes through type annotations so mypy can
    resolve attribute access; runtime attribute lookup is unaffected.
    """

    # Owned collaborators / identity — stable for the orchestrator's life,
    # captured by each component's constructor. NOT shared mutable session
    # state; that lives on :class:`SessionRuntime` (``self._runtime``).
    #: Single owner of all shared mutable session state (TNQA P1); replaces the
    #: ~40 ``self._host.<latch>`` reads that threaded through six ``_*Host``
    #: Protocols. Components receive it as ``runtime=`` and read ``self._runtime``.
    _runtime: SessionRuntime
    _repo_root: Path
    _session_id: str
    _store: DataStore
    _manager: AgentManager
    _executor: PlayExecutor
    # desktop-kqo5: main-repo branch guard (default branch + pre-play handshake
    # + dispatch-pause latch). Constructed in __init__.
    _main_repo: MainRepoGuard
    # Override FIFO + first-play / pending-kind / dispatched-id latches.
    _overrides: OverrideQueue
    # Rolling-velocity / executor-skip-divergence / recent-agent-type windows.
    _velocity: VelocityTracker
    # take_break-failure + rate-limit-recovery latches.
    _recovery: RecoveryTracker
    # Orchestrator-local bookkeeping (never read by any component via the host).
    _seed_path: Path | None
    _step_index: int
    _config_hash: str
    _last_warned_failure_streak: int | None
    _last_warned_any_streak: int | None
    _fleet_idle_persistent_active: bool
    # Composed components (the conductor + its siblings).
    _snapshots: SnapshotProjector
    _state_builder: StateBuilder
    _lifecycle: LifecycleController
    _drain: DrainController
    _completion: CompletionProcessor
    _dispatcher: Dispatcher
    _loop: LoopRunner

    def __init__(
        self,
        *,
        cfg: RuntimeConfig,
        repo_root: Path,
        session_id: str,
        store: DataStore,
        manager: AgentManager,
        executor: PlayExecutor,
        selector: PlaySelector | None = None,
        state_provider: StateProvider | None = None,
    ) -> None:
        # Single owner of all shared mutable session state (TNQA P1).
        # ``SessionRuntime``'s field defaults reproduce the prior latch wall, so
        # a fresh runtime is fully initialised. Velocity-window size is the one
        # cfg-derived ctor arg; everything else defaults.
        self._runtime = SessionRuntime(
            cfg=cfg,
            selector=selector,
            state_provider=state_provider or NullStateProvider(),
        )
        self._repo_root = repo_root
        self._session_id = session_id
        self._store = store
        self._manager = manager
        self._executor = executor
        self._seed_path = None
        self._step_index = 0
        self._config_hash = ""
        # Loop-detection warning memo: highest streak already logged per kind.
        # Reset to None below the warn threshold so a fresh streak re-warns;
        # prevents per-tick log storms while the streak holds.
        self._last_warned_failure_streak = None
        self._last_warned_any_streak = None
        # desktop-85ex: track persistent-idle window transitions for the
        # ``fleet_idle_persistent`` event. Re-set on every Orchestrator
        # instantiation so a re-used object starts cleanly.
        self._fleet_idle_persistent_active = False
        # Override FIFO + single-consume latches (first-play override, pending
        # override kind, dispatched play-ids). The completion/dispatch paths
        # write; loop/state read.
        self._overrides = OverrideQueue()
        # Rolling-velocity / executor-skip-divergence / recent-agent-type
        # collaborator. Owns the windows the completion path writes and the
        # observation/state path reads (slot 177 divergence rate,
        # ``state.recent_executor_skip``, reward velocity + type diversity).
        self._velocity = VelocityTracker(velocity_window_size=cfg.rl.velocity_window_size)
        # take_break-failure + rate-limit-recovery latches (desktop-s1u7). The
        # completion path mutates them; state.py reads recovery_exhausted_agent_ids.
        self._recovery = RecoveryTracker()
        # desktop-kqo5: main-repo branch guard — default branch (resolved by the
        # session-start sweeper / SIGHUP), the per-dispatch pre-play ref
        # handshake, and the auto-restore-failed dispatch-pause latch.
        self._main_repo = MainRepoGuard()
        # Record/history → snapshot projection + trajectory math. Holds stable
        # refs (manager/store/session_id); reload-mutable cfg is passed per-call
        # to build_budget_snapshot and safe_call is passed per-call to
        # record_trajectory_snapshot (Lesson L2a).
        self._snapshots = SnapshotProjector(manager=manager, store=store, session_id=session_id)
        # Composed components. Each receives ``host=self`` for the narrow
        # *behaviour* seam (``_safe_call``, ``effective_budget_caps``, the loop's
        # autonomous-stop/stagnation forwards, sibling-component references) and
        # ``runtime=self._runtime`` for all shared *state*.
        self._state_builder = StateBuilder(
            host=self,
            runtime=self._runtime,
            store=store,
            manager=manager,
            executor=executor,
            session_id=session_id,
            repo_root=repo_root,
            main_repo=self._main_repo,
            snapshots=self._snapshots,
            velocity=self._velocity,
            recovery=self._recovery,
            overrides=self._overrides,
        )
        self._lifecycle = LifecycleController(
            host=self,
            runtime=self._runtime,
            store=store,
            session_id=session_id,
            repo_root=repo_root,
            main_repo=self._main_repo,
        )
        self._drain = DrainController(
            host=self,
            runtime=self._runtime,
            store=store,
            manager=manager,
            session_id=session_id,
            repo_root=repo_root,
            state_builder=self._state_builder,
        )
        self._completion = CompletionProcessor(
            host=self,
            runtime=self._runtime,
            store=store,
            manager=manager,
            executor=executor,
            session_id=session_id,
            repo_root=repo_root,
            main_repo=self._main_repo,
            velocity=self._velocity,
            recovery=self._recovery,
            overrides=self._overrides,
            snapshots=self._snapshots,
            state_builder=self._state_builder,
            lifecycle=self._lifecycle,
            drain=self._drain,
        )
        self._dispatcher = Dispatcher(
            host=self,
            runtime=self._runtime,
            store=store,
            manager=manager,
            executor=executor,
            session_id=session_id,
            repo_root=repo_root,
            main_repo=self._main_repo,
            overrides=self._overrides,
            state_builder=self._state_builder,
            completion=self._completion,
        )
        # The conductor — constructed LAST because it references every sibling
        # component. ``_run_loop_body`` tick order + the loop-liveness heartbeat
        # are preserved exactly. The LoopRunner owns its own loop-only counters
        # (tick-failure streak, wedge counter, watchdog task, heartbeat,
        # fleet-idle latch, warning memos, stagnation stage).
        self._loop = LoopRunner(
            host=self,
            runtime=self._runtime,
            session_id=session_id,
            main_repo=self._main_repo,
            overrides=self._overrides,
            velocity=self._velocity,
            state_builder=self._state_builder,
            dispatcher=self._dispatcher,
            completion=self._completion,
            lifecycle=self._lifecycle,
            drain=self._drain,
        )
        # Loop-liveness heartbeat (#9): 0.0 (not float('inf')) on a real
        # instance so the watchdog treats "loop never started" as not-yet-armed;
        # run_until_idle stamps the first iteration before the loop body runs.
        self._loop._last_loop_iteration_at = 0.0

    # TNQA wave-2 (Task 1): the ~52-property ``orch._<latch>`` backward-compat
    # facade over ``self._runtime`` that used to live here has been deleted.
    # All internal and external call sites were migrated to read/write
    # ``self._runtime.<field>`` (or ``orch._runtime.<field>``) directly —
    # ``self._runtime`` is a public, always-present attribute (set in
    # ``__init__`` above), so the indirection added no safety and only cost
    # ~420 lines of pure boilerplate. New code should always reach shared
    # session state via ``self._runtime``.

    # Plain readonly accessors used by multiple mixins

    def effective_budget_caps(self) -> BudgetConfig:
        """Resolve the live-effective budget caps (overrides shadowing ``_cfg``).

        Single source of truth for the live caps. A ``set_budget``/``add_budget``
        call sets the override fields; until then each falls through to
        ``_cfg.budget``. Returns a fresh frozen ``BudgetConfig`` so the
        config-immutability invariant is preserved (no in-place mutation of
        ``_cfg``).
        """
        rt = self._runtime
        b = rt.cfg.budget
        return BudgetConfig(
            enabled=(
                rt.budget_override_enabled if rt.budget_override_enabled is not None else b.enabled
            ),
            total=(rt.budget_override_total if rt.budget_override_total is not None else b.total),
            warning_threshold=b.warning_threshold,
            time_enabled=(
                rt.time_override_enabled if rt.time_override_enabled is not None else b.time_enabled
            ),
            time_total_minutes=(
                rt.time_override_minutes
                if rt.time_override_minutes is not None
                else b.time_total_minutes
            ),
        )

    def _weights_dir(self) -> Path:
        """Canonical per-project PPO weights directory."""
        return project_weights_dir(self._repo_root)

    def _selector_config_index(self) -> tuple[ConfigKey, ...] | None:
        raw = getattr(self._runtime.selector, "_config_index", None)
        return raw if isinstance(raw, tuple) and raw else None

    # Shared infrastructure + host-Protocol loop delegators on the composition
    # root. ``_safe_call`` is stateless infra every component reaches via
    # ``self._host._safe_call``. The loop methods other components reference
    # through their host Protocols (autonomous-stop, stagnation,
    # start/stop_loop_liveness_watchdog) are thin delegators to ``self._loop``,
    # so ``self._host.<method>`` resolves here on the composition root.

    async def _safe_call(self, coro: Awaitable[object], label: str) -> None:
        """Await *coro* and log ERROR if it raises; never propagates.

        Stateless shared infrastructure: every composed component reaches it via
        ``self._host._safe_call``, so it lives on the composition root rather
        than inside any one component.
        """
        try:
            await coro
        except Exception as exc:
            _logger.error("safe_call_failed", label=label, error=str(exc), exc_info=True)

    async def _initiate_autonomous_stop(
        self,
        reason: str,
        *,
        arm_gate_only: bool = False,
        fire_natural_exit: bool = False,
        clear_pause_deadline: bool = False,
    ) -> None:
        """Forward to :meth:`LoopRunner.initiate_autonomous_stop`.

        Referenced by ``CompletionProcessor`` (no-forward-progress monitor) and
        the loop's own autonomous-stop paths via the ``_CompletionHost`` Protocol.
        """
        await self._loop.initiate_autonomous_stop(
            reason,
            arm_gate_only=arm_gate_only,
            fire_natural_exit=fire_natural_exit,
            clear_pause_deadline=clear_pause_deadline,
        )

    async def _check_stagnation_escalation(self, state: OrchestratorState) -> bool:
        """Forward to :meth:`LoopRunner.check_stagnation_escalation`.

        Referenced by ``CompletionProcessor`` via the ``_CompletionHost`` Protocol.
        """
        return await self._loop.check_stagnation_escalation(state)

    def start_loop_liveness_watchdog(self) -> None:
        """Forward to :meth:`LoopRunner.start_loop_liveness_watchdog`.

        Called from ``Orchestrator.__aenter__`` before the loop runs.
        """
        self._loop.start_loop_liveness_watchdog()

    def stop_loop_liveness_watchdog(self) -> None:
        """Forward to :meth:`LoopRunner.stop_loop_liveness_watchdog`.

        Referenced by ``DrainController`` teardown via the ``_DrainHost`` Protocol.
        """
        self._loop.stop_loop_liveness_watchdog()

    async def resume(self) -> None:
        """Forward to :meth:`LifecycleController.resume`.

        Referenced by ``DrainController._rearm_after_budget_change`` via the
        ``_DrainHost`` Protocol so budget-pause and drain-reversal resume routes
        through the single canonical lifecycle path (DB update, cadence reset,
        ``session_resumed`` event).
        """
        await self._lifecycle.resume()
