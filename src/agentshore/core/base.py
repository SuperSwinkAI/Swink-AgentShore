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

    _runtime: SessionRuntime
    _repo_root: Path
    _session_id: str
    _store: DataStore
    _manager: AgentManager
    _executor: PlayExecutor
    _main_repo: MainRepoGuard
    _overrides: OverrideQueue
    _velocity: VelocityTracker
    _recovery: RecoveryTracker
    _seed_path: Path | None
    _step_index: int
    _config_hash: str
    _last_warned_failure_streak: int | None
    _last_warned_any_streak: int | None
    _fleet_idle_persistent_active: bool
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
        self._last_warned_failure_streak = None
        self._last_warned_any_streak = None
        self._fleet_idle_persistent_active = False
        self._overrides = OverrideQueue()
        self._velocity = VelocityTracker(velocity_window_size=cfg.rl.velocity_window_size)
        self._recovery = RecoveryTracker()
        self._main_repo = MainRepoGuard()
        self._snapshots = SnapshotProjector(manager=manager, store=store, session_id=session_id)
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
        self._loop._last_loop_iteration_at = 0.0

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
