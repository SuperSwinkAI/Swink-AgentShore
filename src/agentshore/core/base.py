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

import asyncio
import collections
import time
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
from agentshore.core.velocity_tracker import VelocityTracker
from agentshore.paths import project_weights_dir
from agentshore.state import NullStateProvider

if TYPE_CHECKING:
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
    from agentshore.rl.action_space import ConfigKey
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

    # Class-level defaults so tests that bypass __init__ via Orchestrator.__new__
    # still get sensible values. Real instances overwrite these in __init__.
    _last_selection_digest: bytes | None = None
    _idle_streak: int = 0
    _draining: bool = False
    _drain_reason: str | None = None
    _drain_initialized: bool = False
    _end_session_dispatch_started: bool = False
    _natural_exit_reason: str | None = None
    _natural_exit_callback: NaturalExitCallback | None = None
    _extra_budget: float = 0.0
    # Monotonic timestamp of the most recent _refresh_issues call.  Class-level
    # default is float('inf') so tests that bypass __init__ via Orchestrator.__new__
    # never trigger a periodic refresh.  __init__ resets it to 0.0 and bootstrap
    # stamps it to time.monotonic() right after the session-start fetch, ensuring
    # the first loop tick doesn't re-fetch immediately in production.
    _last_refresh_time: float = float("inf")
    # desktop-kqo5: main-repo branch guard (default branch + pre-play handshake
    # + dispatch-pause latch). Constructed in __init__; __new__-bypass tests
    # construct their own.
    _main_repo: MainRepoGuard
    # desktop-12g9: AgentShore-managed worktree lifecycle owner. Assigned by
    # ``_phase_init_worktree_manager`` during bootstrap. Stays None for
    # tests that bypass bootstrap (Orchestrator.__new__); reaper hooks
    # short-circuit on None so those callers remain no-ops.
    _worktrees: WorktreeManager | None = None
    # Guarded RL experience-recording collaborator (crash hardening). Class-level
    # default None so tests that bypass __init__ (Orchestrator.__new__) and the
    # non-PPO/headless paths are safe; constructed in phases.py once the PPO
    # selector + metrics + policy/config versions are wired. The completion path
    # no-ops the RL tail when this is None.
    _experience_recorder: ExperienceRecorder | None = None
    # Pure progress assessor (no-op-spin detection + WS3 reprieve gating).
    # Class-level default None; constructed in phases.py. Callers guard on None.
    _progress_monitor: ForwardProgressMonitor | None = None

    # Type annotations for instance attributes set in __init__ — mixins access
    # these via self.* and rely on these annotations for mypy resolution.
    _cfg: RuntimeConfig
    _repo_root: Path
    _session_id: str
    _store: DataStore
    _manager: AgentManager
    _executor: PlayExecutor
    _selector: PlaySelector | None
    _state_provider: StateProvider
    _stop_requested: bool
    _stopped: bool
    _end_session_report_requested: bool
    _end_session_report_open_browser: bool
    # When True the orchestrator is hosted inside the desktop sidecar / an
    # embedded process where the shell renders the ESR in-app. drain.py skips
    # ``webbrowser.open`` in this mode and instead fires ``_esr_ready_callback``
    # so the desktop can navigate to ``/session/esr`` (issue #561).
    _embedded_mode: bool
    _esr_ready_callback: Callable[[str, str, str | None], None] | None
    _log_path: Path | None
    _stop_reason: str
    _health: HealthMonitor | None
    _in_flight: dict[str, asyncio.Task[PlayOutcome]]
    _dispatch_ctx: dict[str, _DispatchContext]
    _completion_processing_count: int
    _completion_processing_idle: asyncio.Event
    context_pressure_hints: dict[str, float]
    _seed_path: Path | None
    _step_index: int
    _policy_version: str
    _config_hash: str
    _metrics: MetricsEngine | None
    # Override FIFO + first-play / pending-kind / dispatched-id latches.
    _overrides: OverrideQueue
    _loop_started_at: float
    _registry: object | None
    _pause_event: asyncio.Event
    _pause_reason: str | None
    # Monotonic deadline after which an unanswered feedback pause auto-stops the
    # session (#9). Set in pause() for feedback-eligible reasons when
    # feedback.unanswered_timeout_seconds is configured; cleared on resume().
    _pause_deadline: float | None
    _last_play_id: int | None
    # Rolling-velocity / executor-skip-divergence / recent-agent-type windows.
    _velocity: VelocityTracker
    # All-category no-op window for spin detection: (was_skip, play_type_value)
    # per completed play. Unlike _executor_skip_window (masked-only), this counts
    # every skip category (masked + no_target + staffing) so the LoopProgressMonitor
    # can see an alternating no_target/masked spin the masked-only rate misses.
    _recent_play_outcomes: collections.deque[tuple[bool, str]]
    _budget_override: bool
    # Live budget-cap overrides applied mid-session (Feature B, #41/#42). Each is
    # ``None`` until a live control RPC/command sets it, in which case it shadows
    # the corresponding ``_cfg.budget`` field without mutating the frozen config.
    # ``effective_budget_caps`` resolves overrides → cfg as the single source of
    # truth read by the snapshot builder and the time hard-stop.
    # Class-level ``None`` defaults so __new__-bypass test stubs (which skip
    # __init__) still resolve overrides as "fall through to _cfg.budget".
    _budget_override_enabled: bool | None = None
    _budget_override_total: float | None = None
    _time_override_enabled: bool | None = None
    _time_override_minutes: int | None = None
    _stop_done: asyncio.Event
    _config_path: Path | None
    # In-memory snapshot of recently-completed plays, used to bridge the SQLite
    # WAL-flush lag window. After ``_process_completion`` records a play, the
    # row is in the DB but may not be visible to a subsequent ``get_play_history``
    # read for tens of milliseconds. That lag let same-tick instantiate_agent
    # pairs slip past the cooldown mask in session ba744eef (desktop-65bg).
    # ``_fetch_state_data`` merges this deque with the DB result so recency
    # math sees the freshest view. Capped to keep the merge cost bounded.
    _recent_play_completions: collections.deque[PlayRecord]
    # Sibling shadow for per-issue labels applied by a successful play whose
    # next-tick mask depends on that label being visible immediately. Same
    # WAL-flush lag class as ``_recent_play_completions`` — the gh CLI label
    # add + ``add_issue_labels`` write may not be visible to a fast follow-up
    # ``get_open_issues`` read, so ``_fetch_state_data`` overlays this deque
    # onto the cached issue records before snapshot projection. Scoped strictly
    # to ROOT_CAUSE_FOUND_LABEL on systematic_debugging success (desktop-quv9
    # — session 2b8729bf re-selected the same issue at the very next tick
    # before the label landed). Other label flows do not need this hop.
    _recent_applied_labels: collections.deque[tuple[int, str]]
    # take_break-failure + rate-limit-recovery latches.
    _recovery: RecoveryTracker
    # Record/history → snapshot projection + trajectory math (composed component).
    _snapshots: SnapshotProjector
    # DB reads + live handles → OrchestratorState (composed component). Reads
    # orchestrator runtime/control state live via the _StateBuilderHost Protocol.
    _state_builder: StateBuilder
    # Pause/resume, SIGHUP config reload, feedback cadence, budget-drain
    # initiation (composed component). Reads+writes orchestrator runtime/control
    # state (incl. the _cfg SIGHUP swap) live via the _LifecycleHost Protocol.
    _lifecycle: LifecycleController
    # Graceful drain, stop/hard_stop, budget adjust, end-session report (composed
    # component). Reads+writes orchestrator runtime/control state (stop/drain
    # latches, budget override, ESR request flags, in-flight maps) live via the
    # _DrainHost Protocol; teardown order in stop/stop_inner is preserved exactly.
    _drain: DrainController
    # Play-completion harvesting, RL experience persistence, learnings, GitHub
    # refresh, health callbacks (composed component). Reads+writes orchestrator
    # runtime/control state (in-flight maps, completion-processing latches,
    # recent-completion shadows, pause/stop latches) live via the _CompletionHost
    # Protocol; the _process_completion pipeline order is preserved exactly.
    _completion: CompletionProcessor
    # Override resolution, selector calls, dispatch, and mask handling (composed
    # component). Reads+writes orchestrator runtime/control state (in-flight maps,
    # dispatch-context map, selection digest, idle streak, end-session latch) live
    # via the _DispatcherHost Protocol; the _dispatch_play gate order and the
    # OverrideQueue single-consume protocol are preserved exactly (no gate-move —
    # that is Phase 2).
    _dispatcher: Dispatcher
    # The main orchestration loop, loop-detection ladder, stagnation escalation,
    # and idle backoff (composed component, the conductor). Holds references to
    # every sibling component + the 1a collaborators via its constructor;
    # orchestrator runtime/control state (read or written) flows through the
    # _LoopHost Protocol so SIGHUP/per-tick mutation never goes stale. The
    # _run_loop_body tick order and the loop-liveness heartbeat are preserved
    # exactly. Loop-only counters (tick-failure streak, wedge counter, watchdog
    # handle, heartbeat, fleet-idle latch, warning memos, stagnation stage) are
    # owned by the LoopRunner, not the host.
    _loop: LoopRunner
    _feedback_cadence_plays_since_ack: int
    _feedback_cadence_last_ack_monotonic: float

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
        self._cfg = cfg
        self._repo_root = repo_root
        self._session_id = session_id
        self._store = store
        self._manager = manager
        # desktop-12g9: assigned in ``_phase_init_worktree_manager`` during
        # bootstrap. Tests bypassing bootstrap leave this None — the reaper
        # hooks short-circuit on None to keep them no-ops.
        self._worktrees = None
        self._executor = executor
        self._selector = selector
        self._state_provider = state_provider or NullStateProvider()
        self._stop_requested = False
        self._stopped = False
        self._draining = False
        self._drain_reason = None
        self._drain_initialized = False
        self._end_session_dispatch_started = False
        # Set when run_until_idle exits because _should_terminate signalled
        # should_stop with a reason other than "stop_requested" (i.e. drain
        # complete, max_plays, timeout). The sidecar boot wrapper reads this
        # to decide whether to fire session.completed (DESIGN §5.2).
        self._natural_exit_reason = None
        self._natural_exit_callback = None
        self._end_session_report_requested = False
        self._end_session_report_open_browser = False
        self._embedded_mode = False
        self._esr_ready_callback = None
        self._log_path = None
        self._extra_budget = 0.0
        self._stop_reason = "unknown"
        self._health = None
        self._integrity: IntegrityMonitor | None = None
        self._power_assertion: PowerAssertion | None = None
        self._in_flight = {}
        self._dispatch_ctx = {}
        self._completion_processing_count = 0
        self._completion_processing_idle = asyncio.Event()
        self._completion_processing_idle.set()
        self.context_pressure_hints = {}
        self._seed_path = None
        self._step_index = 0
        self._policy_version = "ppo-v1"
        self._config_hash = ""
        self._metrics = None
        # Override FIFO + single-consume latches (first-play override, pending
        # override kind, dispatched play-ids). The completion/dispatch paths
        # write; loop/state read.
        self._overrides = OverrideQueue()
        # Loop-detection warning memo: highest streak value already logged for each
        # kind. Reset to None when the streak drops below the warn threshold so a
        # fresh streak gets a fresh warning. Prevents per-tick log storms while the
        # streak holds at the same value across orchestrator iterations.
        self._last_warned_failure_streak = None
        self._last_warned_any_streak = None
        self._loop_started_at = 0.0
        self._registry = None  # PlayRegistry, set in bootstrap
        # Pause/resume: cleared by pause(), set by resume(); loop awaits this each iteration
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # initially running
        self._pause_reason = None
        self._pause_deadline = None
        self._last_play_id = None
        # Rolling-velocity / executor-skip-divergence / recent-agent-type
        # collaborator. Owns the windows the completion path writes and the
        # observation/state path reads (slot 177 divergence rate,
        # ``state.recent_executor_skip``, reward velocity + type diversity).
        self._velocity = VelocityTracker(velocity_window_size=cfg.rl.velocity_window_size)
        # All-category no-op window for the LoopProgressMonitor (see annotation).
        self._recent_play_outcomes = collections.deque(maxlen=50)
        # Retained for IPC compatibility with older feedback responses. Budget
        # reserve drain itself is not bypassable once reached.
        self._budget_override = False
        # Live mid-session cap overrides (None ⇒ fall through to _cfg.budget).
        self._budget_override_enabled = None
        self._budget_override_total = None
        self._time_override_enabled = None
        self._time_override_minutes = None
        # Concurrent stop() callers wait on this event; first caller does the cleanup
        self._stop_done = asyncio.Event()
        self._config_path = None
        # Bounded recent-completions cache (see annotation above for rationale).
        # 64 is large enough to span a few seconds of WAL lag at peak dispatch
        # rate; older entries either appear in the DB read or are no longer
        # within any cooldown window we care about.
        self._recent_play_completions = collections.deque(maxlen=64)
        # Sibling label shadow (desktop-quv9). Same bound rationale as the
        # play-completion shadow: a few seconds of WAL lag at peak dispatch
        # rate is plenty for the next refresh_issues cycle to land. Entries
        # are ``(issue_number, label)`` tuples; the merge helper is keyed
        # on issue_number.
        self._recent_applied_labels = collections.deque(maxlen=64)
        # Per-resource worktree-allocation failure backstop (Piece A, issue #60).
        # ``_resource_failure_counts`` tallies consecutive allocation failures
        # keyed on resource key (``pr:<n>``); once a key crosses
        # ``WORKTREE_PARK_THRESHOLD`` it is added to ``_parked_resource_keys`` and
        # excluded from candidate selection for the rest of the session. This
        # bounds transient blips (a couple of retries) while stopping a
        # structurally-unallocatable PR from being re-selected every tick. Both
        # are session-scoped in-memory; snapshotted onto state each tick.
        self._resource_failure_counts: dict[str, int] = {}
        self._parked_resource_keys: set[str] = set()
        # take_break-failure + rate-limit-recovery latches (desktop-s1u7). The
        # completion path mutates them; state.py reads recovery_exhausted_agent_ids.
        self._recovery = RecoveryTracker()
        # Record/history → snapshot projection + trajectory math. Holds stable
        # refs (manager/store/session_id); reload-mutable cfg/extra_budget are
        # passed per-call to build_budget_snapshot and safe_call is passed
        # per-call to record_trajectory_snapshot (Lesson L2a).
        self._snapshots = SnapshotProjector(manager=manager, store=store, session_id=session_id)
        # DB reads + live handles → OrchestratorState. Stable services/
        # collaborators captured via the constructor; orchestrator runtime/
        # control state (cfg, in-flight maps, pause/drain latches, recent-
        # completion shadows) is read live via the _StateBuilderHost Protocol so
        # SIGHUP/per-tick mutation never goes stale. Owns its stale-idle counter.
        self._state_builder = StateBuilder(
            host=self,
            store=store,
            manager=manager,
            executor=executor,
            session_id=session_id,
            repo_root=repo_root,
            snapshots=self._snapshots,
            velocity=self._velocity,
            recovery=self._recovery,
            overrides=self._overrides,
        )
        # Selection-state digest gate: skip the selector + storm-prone log line
        # when nothing the selector cares about changed since the last attempt.
        # Pairs with ``_IDLE_BACKOFF_SECONDS`` to stretch the loop's idle wait
        # progressively the longer the digest stays put.
        self._last_selection_digest = None
        self._idle_streak = 0
        # desktop-85ex: track persistent-idle window transitions for the
        # ``fleet_idle_persistent`` event. Re-set on every Orchestrator
        # instantiation so a re-used object starts cleanly.
        self._fleet_idle_persistent_active = False
        # Monotonic wall-clock time of last _refresh_issues call; 0.0 means
        # "never refreshed" so the first eligible tick always fires.
        self._last_refresh_time = 0.0
        # Feedback cadence: plays completed since last user-acknowledged checkpoint
        # and monotonic time of that acknowledgement. Both reset in resume().
        self._feedback_cadence_plays_since_ack = 0
        self._feedback_cadence_last_ack_monotonic = time.monotonic()
        # desktop-kqo5: main-repo branch guard — default branch (resolved by the
        # session-start sweeper / SIGHUP), the per-dispatch pre-play ref
        # handshake, and the auto-restore-failed dispatch-pause latch.
        self._main_repo = MainRepoGuard()
        # Pause/resume, SIGHUP config reload, feedback cadence, budget-drain
        # initiation. Stable services/collaborators (store, session_id,
        # repo_root, main_repo) captured via the constructor; orchestrator
        # runtime/control state is read+written live via the _LifecycleHost
        # Protocol so SIGHUP config swaps and per-tick mutation never go stale.
        self._lifecycle = LifecycleController(
            host=self,
            store=store,
            session_id=session_id,
            repo_root=repo_root,
            main_repo=self._main_repo,
        )
        # Graceful drain, stop/hard_stop, budget adjust, end-session report
        # generation. Stable services/collaborators (store, manager, session_id,
        # repo_root, state_builder) captured via the constructor; orchestrator
        # runtime/control state (stop/drain latches, budget override, ESR request
        # flags, in-flight maps) is read+written live via the _DrainHost Protocol
        # so per-tick mutation never goes stale. Teardown order in stop/stop_inner
        # is preserved exactly.
        self._drain = DrainController(
            host=self,
            store=store,
            manager=manager,
            session_id=session_id,
            repo_root=repo_root,
            state_builder=self._state_builder,
        )
        # Play-completion harvesting, RL experience persistence, learnings update,
        # GitHub issue refresh, and the agent health callbacks. Stable services/
        # collaborators (store, manager, executor, session_id, repo_root, the 1a
        # collaborators, and the sibling components) captured via the constructor;
        # orchestrator runtime/control state (in-flight maps, completion-processing
        # latches, recent-completion shadows, pause/stop latches) is read+written
        # live via the _CompletionHost Protocol so per-tick mutation never goes
        # stale. The _process_completion pipeline order is preserved exactly.
        self._completion = CompletionProcessor(
            host=self,
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
        # Override resolution, selector calls, dispatch, and mask handling. Stable
        # services/collaborators (store, manager, executor, session_id, repo_root,
        # the main_repo + overrides collaborators, and the state_builder/completion
        # sibling components) captured via the constructor; orchestrator runtime/
        # control state (in-flight maps, dispatch-context map, selection digest,
        # idle streak, end-session latch) is read+written live via the
        # _DispatcherHost Protocol so per-tick mutation never goes stale. The
        # _dispatch_play gate order and the OverrideQueue single-consume protocol
        # are preserved exactly (no gate-move — that is Phase 2).
        self._dispatcher = Dispatcher(
            host=self,
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
        # The main orchestration loop, loop-detection ladder, stagnation
        # escalation, and idle backoff — the conductor. Constructed LAST because
        # it references every sibling component (state_builder, dispatcher,
        # completion, lifecycle, drain) plus the 1a collaborators (main_repo,
        # overrides, velocity). Orchestrator runtime/control state (in-flight
        # map, idle streak, selection digest, pause/drain latches, natural-exit
        # hooks) is read+written live via the _LoopHost Protocol so SIGHUP/
        # per-tick mutation never goes stale. The _run_loop_body tick order and
        # the loop-liveness heartbeat are preserved exactly. The LoopRunner owns
        # its own loop-only counters (tick-failure streak, wedge counter, watchdog
        # task, heartbeat, fleet-idle latch, warning memos, stagnation stage).
        self._loop = LoopRunner(
            host=self,
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
        # Crash-hardening collaborators (constructed in phases.py once the PPO
        # selector / metrics / versions are wired). None on the non-PPO path.
        self._experience_recorder = None
        self._progress_monitor = None

    # ------------------------------------------------------------------
    # Plain readonly accessors used by multiple mixins
    # ------------------------------------------------------------------

    def effective_budget_caps(self) -> BudgetConfig:
        """Resolve the live-effective budget caps (overrides shadowing ``_cfg``).

        Single source of truth for the *base* caps (before ``_extra_budget`` is
        added by the snapshot builder). A live ``set_budget``/``add_*`` call sets
        the override fields; until then each falls through to ``_cfg.budget``.
        Returns a fresh frozen ``BudgetConfig`` so the config-immutability
        invariant is preserved (no in-place mutation of ``_cfg``).
        """
        b = self._cfg.budget
        return BudgetConfig(
            enabled=(
                self._budget_override_enabled
                if self._budget_override_enabled is not None
                else b.enabled
            ),
            total=(
                self._budget_override_total
                if self._budget_override_total is not None
                else b.total
            ),
            warning_threshold=b.warning_threshold,
            time_enabled=(
                self._time_override_enabled
                if self._time_override_enabled is not None
                else b.time_enabled
            ),
            time_total_minutes=(
                self._time_override_minutes
                if self._time_override_minutes is not None
                else b.time_total_minutes
            ),
        )

    def _weights_dir(self) -> Path:
        """Canonical per-project PPO weights directory."""
        return project_weights_dir(self._repo_root)

    def _selector_config_index(self) -> tuple[ConfigKey, ...] | None:
        raw = getattr(self._selector, "_config_index", None)
        return raw if isinstance(raw, tuple) and raw else None

    # ------------------------------------------------------------------
    # Shared infrastructure + host-Protocol loop delegators on the composition
    # root.
    #
    # The 7-mixin teardown is complete: ``_OrchestratorBase`` no longer carries a
    # not-implemented stub wall. Every behaviour lives in an owned
    # component. ``_safe_call`` is stateless infra reached via
    # ``self._host._safe_call`` from every component. The four loop methods other
    # components reference through their host Protocols
    # (``_CompletionHost._initiate_autonomous_stop`` /
    # ``_check_stagnation_escalation`` and ``_DrainHost.stop_loop_liveness_watchdog``,
    # plus ``__aenter__``'s ``start_loop_liveness_watchdog``) resolve here as thin
    # delegators forwarding to ``self._loop`` — so ``self._host.<method>`` (and
    # the public ``orch.<method>``) keep resolving on the composition root now
    # that the loop is an owned component rather than an inherited mixin.
    # ------------------------------------------------------------------

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
