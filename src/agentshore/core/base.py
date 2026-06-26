"""Base class for :class:`agentshore.core.Orchestrator` mixins.

``_OrchestratorBase`` is the rightmost entry in the ``Orchestrator`` MRO and
is the only base that defines ``__init__``. It declares the runtime
attributes that every mixin reads via ``self._store``/``self._cfg``/etc. as
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
    import asyncio
    import collections
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from agentshore.agents.health import HealthMonitor
    from agentshore.agents.manager import AgentManager
    from agentshore.agents.worktree import WorktreeManager
    from agentshore.config import RuntimeConfig
    from agentshore.core.context import _DispatchContext
    from agentshore.core.experience_recorder import ExperienceRecorder
    from agentshore.core.progress_monitor import ForwardProgressMonitor
    from agentshore.data.integrity import IntegrityMonitor
    from agentshore.data.store import (
        DataStore,
        PlayRecord,
    )
    from agentshore.plays.executor import PlayExecutor
    from agentshore.plays.selector import PlaySelector
    from agentshore.power import PowerAssertion
    from agentshore.rl.config_head import ConfigKey
    from agentshore.rl.metrics import MetricsEngine
    from agentshore.state import (
        OrchestratorState,
        PlayOutcome,
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

    # Backward-compatible ``orch._<latch>`` facade over the SessionRuntime.
    # External callers and the test suite read/write these directly; rather than
    # rewrite every caller, each latch is a pure pass-through property — no
    # parallel state, the single owner is still ``self._runtime``. New core code
    # reaches state via ``self._runtime``.

    @property
    def _cfg(self) -> RuntimeConfig:
        return self._runtime.cfg

    @_cfg.setter
    def _cfg(self, value: RuntimeConfig) -> None:
        self._runtime.cfg = value

    @property
    def _selector(self) -> PlaySelector | None:
        return self._runtime.selector

    @_selector.setter
    def _selector(self, value: PlaySelector | None) -> None:
        self._runtime.selector = value

    @property
    def _state_provider(self) -> StateProvider:
        return self._runtime.state_provider

    @_state_provider.setter
    def _state_provider(self, value: StateProvider) -> None:
        self._runtime.state_provider = value

    @property
    def _registry(self) -> object | None:
        return self._runtime.registry

    @_registry.setter
    def _registry(self, value: object | None) -> None:
        self._runtime.registry = value

    @property
    def _metrics(self) -> MetricsEngine | None:
        return self._runtime.metrics

    @_metrics.setter
    def _metrics(self, value: MetricsEngine | None) -> None:
        self._runtime.metrics = value

    @property
    def _worktrees(self) -> WorktreeManager | None:
        return self._runtime.worktrees

    @_worktrees.setter
    def _worktrees(self, value: WorktreeManager | None) -> None:
        self._runtime.worktrees = value

    @property
    def _experience_recorder(self) -> ExperienceRecorder | None:
        return self._runtime.experience_recorder

    @_experience_recorder.setter
    def _experience_recorder(self, value: ExperienceRecorder | None) -> None:
        self._runtime.experience_recorder = value

    @property
    def _progress_monitor(self) -> ForwardProgressMonitor | None:
        return self._runtime.progress_monitor

    @_progress_monitor.setter
    def _progress_monitor(self, value: ForwardProgressMonitor | None) -> None:
        self._runtime.progress_monitor = value

    @property
    def _health(self) -> HealthMonitor | None:
        return self._runtime.health

    @_health.setter
    def _health(self, value: HealthMonitor | None) -> None:
        self._runtime.health = value

    @property
    def _integrity(self) -> IntegrityMonitor | None:
        return self._runtime.integrity

    @_integrity.setter
    def _integrity(self, value: IntegrityMonitor | None) -> None:
        self._runtime.integrity = value

    @property
    def _power_assertion(self) -> PowerAssertion | None:
        return self._runtime.power_assertion

    @_power_assertion.setter
    def _power_assertion(self, value: PowerAssertion | None) -> None:
        self._runtime.power_assertion = value

    @property
    def _policy_version(self) -> str:
        return self._runtime.policy_version

    @_policy_version.setter
    def _policy_version(self, value: str) -> None:
        self._runtime.policy_version = value

    @property
    def _config_path(self) -> Path | None:
        return self._runtime.config_path

    @_config_path.setter
    def _config_path(self, value: Path | None) -> None:
        self._runtime.config_path = value

    @property
    def _log_path(self) -> Path | None:
        return self._runtime.log_path

    @_log_path.setter
    def _log_path(self, value: Path | None) -> None:
        self._runtime.log_path = value

    @property
    def _embedded_mode(self) -> bool:
        return self._runtime.embedded_mode

    @_embedded_mode.setter
    def _embedded_mode(self, value: bool) -> None:
        self._runtime.embedded_mode = value

    @property
    def _esr_ready_callback(self) -> Callable[[str, str, str | None], None] | None:
        return self._runtime.esr_ready_callback

    @_esr_ready_callback.setter
    def _esr_ready_callback(self, value: Callable[[str, str, str | None], None] | None) -> None:
        self._runtime.esr_ready_callback = value

    @property
    def _natural_exit_callback(self) -> NaturalExitCallback | None:
        return self._runtime.natural_exit_callback

    @_natural_exit_callback.setter
    def _natural_exit_callback(self, value: NaturalExitCallback | None) -> None:
        self._runtime.natural_exit_callback = value

    @property
    def _stop_requested(self) -> bool:
        return self._runtime.stop_requested

    @_stop_requested.setter
    def _stop_requested(self, value: bool) -> None:
        self._runtime.stop_requested = value

    @property
    def _stopped(self) -> bool:
        return self._runtime.stopped

    @_stopped.setter
    def _stopped(self, value: bool) -> None:
        self._runtime.stopped = value

    @property
    def _stop_reason(self) -> str:
        return self._runtime.stop_reason

    @_stop_reason.setter
    def _stop_reason(self, value: str) -> None:
        self._runtime.stop_reason = value

    @property
    def _stop_done(self) -> asyncio.Event:
        return self._runtime.stop_done

    @_stop_done.setter
    def _stop_done(self, value: asyncio.Event) -> None:
        self._runtime.stop_done = value

    @property
    def _draining(self) -> bool:
        return self._runtime.draining

    @_draining.setter
    def _draining(self, value: bool) -> None:
        self._runtime.draining = value

    @property
    def _drain_reason(self) -> str | None:
        return self._runtime.drain_reason

    @_drain_reason.setter
    def _drain_reason(self, value: str | None) -> None:
        self._runtime.drain_reason = value

    @property
    def _drain_initialized(self) -> bool:
        return self._runtime.drain_initialized

    @_drain_initialized.setter
    def _drain_initialized(self, value: bool) -> None:
        self._runtime.drain_initialized = value

    @property
    def _pause_event(self) -> asyncio.Event:
        return self._runtime.pause_event

    @_pause_event.setter
    def _pause_event(self, value: asyncio.Event) -> None:
        self._runtime.pause_event = value

    @property
    def _pause_reason(self) -> str | None:
        return self._runtime.pause_reason

    @_pause_reason.setter
    def _pause_reason(self, value: str | None) -> None:
        self._runtime.pause_reason = value

    @property
    def _pause_deadline(self) -> float | None:
        return self._runtime.pause_deadline

    @_pause_deadline.setter
    def _pause_deadline(self, value: float | None) -> None:
        self._runtime.pause_deadline = value

    @property
    def _natural_exit_reason(self) -> str | None:
        return self._runtime.natural_exit_reason

    @_natural_exit_reason.setter
    def _natural_exit_reason(self, value: str | None) -> None:
        self._runtime.natural_exit_reason = value

    @property
    def _end_session_dispatch_started(self) -> bool:
        return self._runtime.end_session_dispatch_started

    @_end_session_dispatch_started.setter
    def _end_session_dispatch_started(self, value: bool) -> None:
        self._runtime.end_session_dispatch_started = value

    @property
    def _end_session_report_requested(self) -> bool:
        return self._runtime.end_session_report_requested

    @_end_session_report_requested.setter
    def _end_session_report_requested(self, value: bool) -> None:
        self._runtime.end_session_report_requested = value

    @property
    def _end_session_report_open_browser(self) -> bool:
        return self._runtime.end_session_report_open_browser

    @_end_session_report_open_browser.setter
    def _end_session_report_open_browser(self, value: bool) -> None:
        self._runtime.end_session_report_open_browser = value

    @property
    def _budget_override_enabled(self) -> bool | None:
        return self._runtime.budget_override_enabled

    @_budget_override_enabled.setter
    def _budget_override_enabled(self, value: bool | None) -> None:
        self._runtime.budget_override_enabled = value

    @property
    def _budget_override_total(self) -> float | None:
        return self._runtime.budget_override_total

    @_budget_override_total.setter
    def _budget_override_total(self, value: float | None) -> None:
        self._runtime.budget_override_total = value

    @property
    def _time_override_enabled(self) -> bool | None:
        return self._runtime.time_override_enabled

    @_time_override_enabled.setter
    def _time_override_enabled(self, value: bool | None) -> None:
        self._runtime.time_override_enabled = value

    @property
    def _time_override_minutes(self) -> int | None:
        return self._runtime.time_override_minutes

    @_time_override_minutes.setter
    def _time_override_minutes(self, value: int | None) -> None:
        self._runtime.time_override_minutes = value

    @property
    def _idle_streak(self) -> int:
        return self._runtime.idle_streak

    @_idle_streak.setter
    def _idle_streak(self, value: int) -> None:
        self._runtime.idle_streak = value

    @property
    def _last_selection_digest(self) -> bytes | None:
        return self._runtime.last_selection_digest

    @_last_selection_digest.setter
    def _last_selection_digest(self, value: bytes | None) -> None:
        self._runtime.last_selection_digest = value

    @property
    def _last_refresh_time(self) -> float:
        return self._runtime.last_refresh_time

    @_last_refresh_time.setter
    def _last_refresh_time(self, value: float) -> None:
        self._runtime.last_refresh_time = value

    @property
    def _last_play_id(self) -> int | None:
        return self._runtime.last_play_id

    @_last_play_id.setter
    def _last_play_id(self, value: int | None) -> None:
        self._runtime.last_play_id = value

    @property
    def _loop_started_at(self) -> float:
        return self._runtime.loop_started_at

    @_loop_started_at.setter
    def _loop_started_at(self, value: float) -> None:
        self._runtime.loop_started_at = value

    @property
    def _in_flight(self) -> dict[str, asyncio.Task[PlayOutcome]]:
        return self._runtime.in_flight

    @_in_flight.setter
    def _in_flight(self, value: dict[str, asyncio.Task[PlayOutcome]]) -> None:
        self._runtime.in_flight = value

    @property
    def _dispatch_ctx(self) -> dict[str, _DispatchContext]:
        return self._runtime.dispatch_ctx

    @_dispatch_ctx.setter
    def _dispatch_ctx(self, value: dict[str, _DispatchContext]) -> None:
        self._runtime.dispatch_ctx = value

    @property
    def _completion_processing_count(self) -> int:
        return self._runtime.completion_processing_count

    @_completion_processing_count.setter
    def _completion_processing_count(self, value: int) -> None:
        self._runtime.completion_processing_count = value

    @property
    def _completion_processing_idle(self) -> asyncio.Event:
        return self._runtime.completion_processing_idle

    @_completion_processing_idle.setter
    def _completion_processing_idle(self, value: asyncio.Event) -> None:
        self._runtime.completion_processing_idle = value

    @property
    def _feedback_cadence_plays_since_ack(self) -> int:
        return self._runtime.feedback_cadence_plays_since_ack

    @_feedback_cadence_plays_since_ack.setter
    def _feedback_cadence_plays_since_ack(self, value: int) -> None:
        self._runtime.feedback_cadence_plays_since_ack = value

    @property
    def _feedback_cadence_last_ack_monotonic(self) -> float:
        return self._runtime.feedback_cadence_last_ack_monotonic

    @_feedback_cadence_last_ack_monotonic.setter
    def _feedback_cadence_last_ack_monotonic(self, value: float) -> None:
        self._runtime.feedback_cadence_last_ack_monotonic = value

    @property
    def context_pressure_hints(self) -> dict[str, float]:
        return self._runtime.context_pressure_hints

    @context_pressure_hints.setter
    def context_pressure_hints(self, value: dict[str, float]) -> None:
        self._runtime.context_pressure_hints = value

    @property
    def _recent_play_outcomes(self) -> collections.deque[tuple[bool, str]]:
        return self._runtime.recent_play_outcomes

    @_recent_play_outcomes.setter
    def _recent_play_outcomes(self, value: collections.deque[tuple[bool, str]]) -> None:
        self._runtime.recent_play_outcomes = value

    @property
    def _recent_play_completions(self) -> collections.deque[PlayRecord]:
        return self._runtime.recent_play_completions

    @_recent_play_completions.setter
    def _recent_play_completions(self, value: collections.deque[PlayRecord]) -> None:
        self._runtime.recent_play_completions = value

    @property
    def _recent_applied_labels(self) -> collections.deque[tuple[int, str]]:
        return self._runtime.recent_applied_labels

    @_recent_applied_labels.setter
    def _recent_applied_labels(self, value: collections.deque[tuple[int, str]]) -> None:
        self._runtime.recent_applied_labels = value

    @property
    def _resource_failure_counts(self) -> dict[str, int]:
        return self._runtime.resource_failure_counts

    @_resource_failure_counts.setter
    def _resource_failure_counts(self, value: dict[str, int]) -> None:
        self._runtime.resource_failure_counts = value

    @property
    def _parked_resource_keys(self) -> set[str]:
        return self._runtime.parked_resource_keys

    @_parked_resource_keys.setter
    def _parked_resource_keys(self, value: set[str]) -> None:
        self._runtime.parked_resource_keys = value

    @property
    def _auth_suppressed_agent_types(self) -> set[str]:
        return self._runtime.auth_suppressed_agent_types

    @_auth_suppressed_agent_types.setter
    def _auth_suppressed_agent_types(self, value: set[str]) -> None:
        self._runtime.auth_suppressed_agent_types = value

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
