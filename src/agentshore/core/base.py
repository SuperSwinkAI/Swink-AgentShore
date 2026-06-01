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

from agentshore.paths import project_weights_dir
from agentshore.state import NullStateProvider

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from agentshore.agents.health import HealthMonitor
    from agentshore.agents.manager import AgentManager
    from agentshore.agents.worktree import WorktreeManager
    from agentshore.beads import ProjectGraph
    from agentshore.config import RuntimeConfig
    from agentshore.core.context import _DispatchContext, _StateData
    from agentshore.core.experience_recorder import ExperienceRecorder
    from agentshore.core.progress_monitor import ForwardProgressMonitor
    from agentshore.data.integrity import IntegrityMonitor
    from agentshore.data.store import (
        DataStore,
        GitHubIssueRecord,
        PlayRecord,
        PullRequestRecord,
        TrajectorySnapshotRecord,
    )
    from agentshore.plays.base import PlayParams
    from agentshore.plays.executor import PlayExecutor
    from agentshore.plays.override import OverrideEntry, OverrideKind
    from agentshore.plays.selector import PlaySelector
    from agentshore.power import PowerAssertion
    from agentshore.rl.action_space import ConfigKey
    from agentshore.rl.mask_reason import MaskReason
    from agentshore.rl.metrics import MetricsEngine
    from agentshore.state import (
        AgentSnapshot,
        BudgetSnapshot,
        IssueSnapshot,
        OrchestratorState,
        PlayOutcome,
        PlayType,
        PullRequestSnapshot,
        SessionStatsSnapshot,
        StateProvider,
        TrajectorySnapshot,
    )

    NaturalExitCallback = Callable[[str], Awaitable[None]]


class _OrchestratorBase:
    """Holds every ``self.*`` attribute set in ``Orchestrator.__init__``.

    Mixins reference these attributes through type annotations so mypy can
    resolve attribute access; runtime attribute lookup is unaffected.
    """

    # Class-level defaults so tests that bypass __init__ via Orchestrator.__new__
    # still get sensible values. Real instances overwrite these in __init__.
    _last_warned_failure_streak: int | None = None
    _last_warned_any_streak: int | None = None
    _last_selection_digest: bytes | None = None
    _idle_streak: int = 0
    # desktop-85ex: True iff the loop is currently inside a fleet-idle persistent
    # window (idle streak ≥ fleet_idle_threshold + no in-flight plays). Flipped
    # to True the first tick the threshold is crossed (emit one info event),
    # flipped to False the first tick anything starts dispatching again (emit
    # one info event to mark exit). No per-tick emission.
    _fleet_idle_persistent_active: bool = False
    _draining: bool = False
    _drain_reason: str | None = None
    _drain_initialized: bool = False
    _end_session_dispatch_started: bool = False
    _natural_exit_reason: str | None = None
    _natural_exit_callback: NaturalExitCallback | None = None
    _extra_budget: float = 0.0
    _last_stagnation_stage: int = 0
    _idle_agent_claim_ticks: dict[str, int] = {}
    # Monotonic timestamp of the most recent _refresh_issues call.  Class-level
    # default is float('inf') so tests that bypass __init__ via Orchestrator.__new__
    # never trigger a periodic refresh.  __init__ resets it to 0.0 and bootstrap
    # stamps it to time.monotonic() right after the session-start fetch, ensuring
    # the first loop tick doesn't re-fetch immediately in production.
    _last_refresh_time: float = float("inf")
    # desktop-kqo5: cached default branch resolved from origin/HEAD at session
    # start, refreshed on SIGHUP. ``_pre_play_branches`` maps dispatch_id ->
    # the symbolic ref (e.g. "refs/heads/main") captured before play_started,
    # consumed at play_completed for the branch-mutation guard.
    _default_branch: str = "main"
    # Class-level mutable default empty dict so tests bypassing __init__
    # (Orchestrator.__new__) still hit a working attribute. Real instances
    # bind a fresh dict in __init__.
    _pre_play_branches: dict[str, str | None] = {}
    _main_repo_dispatch_paused: bool = False
    # Loop-liveness heartbeat (#9): monotonic timestamp stamped at the top of
    # every run_until_idle iteration. An independent watchdog task reads it to
    # detect a hard-frozen loop and force-drain. Class-level default is
    # float('inf') so tests that bypass __init__ (Orchestrator.__new__) never
    # look stale to a watchdog they didn't arm; __init__ resets it to 0.0 and
    # the loop stamps it to time.monotonic() on entry.
    _last_loop_iteration_at: float = float("inf")
    # desktop-12g9: AgentShore-managed worktree lifecycle owner. Assigned by
    # ``_phase_init_worktree_manager`` during bootstrap. Stays None for
    # tests that bypass bootstrap (Orchestrator.__new__); reaper hooks
    # short-circuit on None so those callers remain no-ops.
    _worktrees: WorktreeManager | None = None
    # Loop-liveness watchdog task handle (#9). Class-level default so tests that
    # bypass __init__ (Orchestrator.__new__) can call stop_loop_liveness_watchdog
    # during a partial-stop without first wiring the attribute.
    _loop_liveness_task: asyncio.Task[None] | None = None
    # Guarded RL experience-recording collaborator (crash hardening). Class-level
    # default None so tests that bypass __init__ (Orchestrator.__new__) and the
    # non-PPO/headless paths are safe; constructed in phases.py once the PPO
    # selector + metrics + policy/config versions are wired. The completion path
    # no-ops the RL tail when this is None.
    _experience_recorder: ExperienceRecorder | None = None
    # Pure progress assessor (no-op-spin detection + WS3 reprieve gating).
    # Class-level default None; constructed in phases.py. Callers guard on None.
    _progress_monitor: ForwardProgressMonitor | None = None
    # Consecutive run_until_idle ticks whose body raised. The per-tick guard
    # increments this and resets it on any clean tick; at
    # _MAX_CONSECUTIVE_TICK_FAILURES the loop drains gracefully rather than
    # spinning on a permanently-throwing tick. Class-level default for __new__.
    _tick_failure_streak: int = 0

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
    _first_play_override: tuple[PlayType, PlayParams] | None
    _override_queue: asyncio.Queue[OverrideEntry]
    _pending_override_kind: OverrideKind | None
    _override_dispatched_play_ids: set[int]
    _forced_mask_play_types: tuple[PlayType, ...]
    _loop_started_at: float
    _registry: object | None
    _pause_event: asyncio.Event
    _pause_reason: str | None
    # Monotonic deadline after which an unanswered feedback pause auto-stops the
    # session (#9). Set in pause() for feedback-eligible reasons when
    # feedback.unanswered_timeout_seconds is configured; cleared on resume().
    _pause_deadline: float | None
    _last_play_id: int | None
    _recent_executor_skip: bool
    _executor_skip_window: collections.deque[bool]
    # All-category no-op window for spin detection: (was_skip, play_type_value)
    # per completed play. Unlike _executor_skip_window (masked-only), this counts
    # every skip category (masked + no_target + staffing) so the LoopProgressMonitor
    # can see an alternating no_target/masked spin the masked-only rate misses.
    _recent_play_outcomes: collections.deque[tuple[bool, str]]
    _budget_override: bool
    _stop_done: asyncio.Event
    _config_path: Path | None
    _velocity_window_start_play_id: int | None
    _velocity_events: collections.deque[tuple[int, str]]
    _recent_agent_types: collections.deque[str]
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
    _break_recovery_failures: dict[str, int]
    _rate_limit_recovery_enqueued: set[str]
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
        self._last_stagnation_stage = 0
        self._stop_reason = "unknown"
        self._health = None
        self._loop_liveness_task = None
        # Loop-liveness heartbeat (#9). 0.0 until run_until_idle stamps the
        # first iteration; the watchdog treats "loop never started" (0.0) as
        # not-yet-armed so it cannot fire before the loop has begun.
        self._last_loop_iteration_at = 0.0
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
        self._first_play_override = None
        self._override_queue = asyncio.Queue()
        # OverrideKind of the most-recently consumed override, set by
        # _consume_override and read once by _dispatch_play. None means the
        # next dispatch is PPO-selected (not an override).
        self._pending_override_kind = None
        # play_id values of plays that were dispatched from the override queue
        # (bootstrap recipe, user request, retry). Used by _compute_play_streaks
        # to skip them — they are not PPO-collapse, so they should not
        # contribute to same_type_streak / same_type_failure_streak.
        # Set is unbounded; in practice sessions have <10k plays.
        self._override_dispatched_play_ids = set()
        # Loop-detection warning memo: highest streak value already logged for each
        # kind. Reset to None when the streak drops below the warn threshold so a
        # fresh streak gets a fresh warning. Prevents per-tick log storms while the
        # streak holds at the same value across orchestrator iterations.
        self._last_warned_failure_streak = None
        self._last_warned_any_streak = None
        # Retained for state/IPC compatibility but always empty: loop detection
        # no longer force-masks the repeating play type (collapse is handled by
        # the stagnation entropy boost). Kept so _assemble_state and the IPC
        # serializer have a stable field to read.
        self._forced_mask_play_types = ()
        self._loop_started_at = 0.0
        self._registry = None  # PlayRegistry, set in bootstrap
        # Pause/resume: cleared by pause(), set by resume(); loop awaits this each iteration
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # initially running
        self._pause_reason = None
        self._pause_deadline = None
        self._last_play_id = None
        # True when the most recent play returned ``skipped_outcome("masked")``
        # from the executor's preconditions safety net — i.e. state shifted
        # between selection and execution. Surfaced via
        # ``state.recent_executor_skip`` as a diagnostic; cleared by the next
        # non-skipped play completion.
        self._recent_executor_skip = False
        # Rolling 50-outcome window of executor masked-skip occurrences. Each
        # entry is True iff that outcome was ``skipped_outcome("masked")``.
        # Exposed via ``MetricsEngine.executor_skip_rate_provider`` so PPO's
        # observation vector carries the divergence rate as a signal
        # (slot 177). Skipped plays are not persisted to DataStore, so this
        # state has to live on the orchestrator.
        self._executor_skip_window = collections.deque(maxlen=50)
        # All-category no-op window for the LoopProgressMonitor (see annotation).
        self._recent_play_outcomes = collections.deque(maxlen=50)
        # Retained for IPC compatibility with older feedback responses. Budget
        # reserve drain itself is not bypassable once reached.
        self._budget_override = False
        # Concurrent stop() callers wait on this event; first caller does the cleanup
        self._stop_done = asyncio.Event()
        self._config_path = None
        self._velocity_window_start_play_id = None
        self._velocity_events = collections.deque(maxlen=self._cfg.rl.velocity_window_size)
        self._recent_agent_types = collections.deque(maxlen=self._cfg.rl.velocity_window_size)
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
        # Consecutive take_break failures per agent. Resets to 0 on a successful
        # break or any other terminal transition. When the count crosses
        # ``BREAK_RECOVERY_FAILURE_LIMIT``, the loop enqueues an end_agent
        # override with bypass_preconditions=True (desktop-s1u7).
        self._break_recovery_failures: dict[str, int] = {}
        # Agents the loop has already enqueued a RATE_LIMIT_RECOVERY override
        # for. Cleared once that agent recovers (status != ERROR) so the next
        # rate_limit event re-arms the override.
        self._rate_limit_recovery_enqueued: set[str] = set()
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
        self._idle_agent_claim_ticks = {}
        # Monotonic wall-clock time of last _refresh_issues call; 0.0 means
        # "never refreshed" so the first eligible tick always fires.
        self._last_refresh_time = 0.0
        # Feedback cadence: plays completed since last user-acknowledged checkpoint
        # and monotonic time of that acknowledgement. Both reset in resume().
        self._feedback_cadence_plays_since_ack = 0
        self._feedback_cadence_last_ack_monotonic = time.monotonic()
        # desktop-kqo5: default branch defaults to "main" until the session-start
        # sweeper resolves it via git symbolic-ref refs/remotes/origin/HEAD.
        self._default_branch = "main"
        # desktop-kqo5: per-dispatch pre-play symbolic ref shadow. Keyed by
        # dispatch_id; populated in _dispatch_play, consumed in
        # _process_completion. Entries are popped on consumption so the dict
        # never grows past the in-flight set.
        self._pre_play_branches = {}
        # desktop-kqo5: latched on main_repo_auto_restore_failed. _dispatch_play
        # consults this before launching a task. Now cleared by a successful
        # RECONCILE_STATE (which is exempt from the pause so it can heal the
        # trunk); if it persists, the idle-with-work watchdog auto-stops.
        self._main_repo_dispatch_paused = False
        # Consecutive idle-with-work ticks spent under a latched trunk pause with
        # nothing in flight — the wedge signature. Drives the auto-stop in
        # _continue_if_selector_idle_work_remains; reset on any dispatch.
        self._wedged_idle_ticks = 0
        # Bounded reprieves granted to an unanswered loop-detection auto-stop
        # while actionable work (merge-ready PRs / workable issues) remains, so
        # the session is not torn down on top of finished work. Resets per loop.
        self._auto_stop_reprieves_used = 0
        # Crash-hardening collaborators (constructed in phases.py once the PPO
        # selector / metrics / versions are wired). None on the non-PPO path.
        self._experience_recorder = None
        self._progress_monitor = None
        # Per-tick guard circuit-breaker counter (see class annotation).
        self._tick_failure_streak = 0

    # ------------------------------------------------------------------
    # Plain readonly accessors used by multiple mixins
    # ------------------------------------------------------------------

    def _weights_dir(self) -> Path:
        """Canonical per-project PPO weights directory."""
        return project_weights_dir(self._repo_root)

    def _selector_config_index(self) -> tuple[ConfigKey, ...] | None:
        raw = getattr(self._selector, "_config_index", None)
        return raw if isinstance(raw, tuple) and raw else None

    def _compute_rolling_velocity(self, current_play_id: int) -> float:
        """Rolling velocity: (issues_closed + prs_merged) / plays_in_window."""
        if not self._velocity_events:
            return 0.0
        watermark = (
            self._velocity_window_start_play_id
            if self._velocity_window_start_play_id is not None
            else 1
        )
        denom = max(1, current_play_id - watermark + 1)
        return min(1.0, len(self._velocity_events) / denom)

    def _executor_skip_rate_recent_50(self) -> float:
        """Fraction of the last 50 play outcomes that hit the executor masked-skip path.

        Empty window returns 0.0 — a fresh session hasn't had any executor
        outcomes yet, which is the same observable signal as "no recent
        divergence." Feeds ``ObservationContext.executor_skip_rate_recent_50``
        and ultimately observation slot 177.
        """
        if not self._executor_skip_window:
            return 0.0
        return sum(self._executor_skip_window) / len(self._executor_skip_window)

    # ------------------------------------------------------------------
    # Abstract method declarations — supplied by mixins via MRO.
    #
    # These stubs exist so cross-mixin calls (``self._safe_call(...)``,
    # ``self._build_state()``) type-check.  Subclasses must override them;
    # a direct instantiation of ``_OrchestratorBase`` would raise at
    # runtime, but Orchestrator inherits the concrete implementations.
    # ------------------------------------------------------------------

    async def _safe_call(self, coro: Awaitable[object], label: str) -> None:
        raise NotImplementedError

    async def _build_state(self) -> OrchestratorState:
        raise NotImplementedError

    async def _refresh_issues(self) -> None:
        raise NotImplementedError

    async def begin_drain(self, reason: str) -> None:
        raise NotImplementedError

    def start_loop_liveness_watchdog(self) -> None:
        raise NotImplementedError

    def stop_loop_liveness_watchdog(self) -> None:
        raise NotImplementedError

    async def stop(self, grace_period_s: float = 5.0) -> None:
        raise NotImplementedError

    async def _harvest_completed(self) -> None:
        raise NotImplementedError

    async def _wait_for_in_flight(self, *, timeout: float) -> None:
        raise NotImplementedError

    async def _process_completion(self, dispatch_id: str, task: asyncio.Task[PlayOutcome]) -> None:
        raise NotImplementedError

    async def _pause_with_reason(self, reason: str) -> None:
        raise NotImplementedError

    async def _pause_for_feedback_cadence_if_due(self) -> bool:
        raise NotImplementedError

    def _feedback_enabled_for_reason(self, reason: str) -> bool:
        raise NotImplementedError

    async def _begin_budget_reserve_drain_if_needed(
        self, state: OrchestratorState
    ) -> OrchestratorState:
        raise NotImplementedError

    def _should_terminate(self, state: OrchestratorState) -> tuple[bool, str | None]:
        raise NotImplementedError

    async def _check_no_forward_progress(
        self, state: OrchestratorState, outcome: PlayOutcome
    ) -> None:
        raise NotImplementedError

    async def _check_stagnation_escalation(self, state: OrchestratorState) -> bool:
        raise NotImplementedError

    async def _consume_override(
        self, state: OrchestratorState
    ) -> tuple[PlayType, PlayParams] | None:
        raise NotImplementedError

    async def _select_play(
        self,
        state: OrchestratorState,
        *,
        override_play: tuple[PlayType, PlayParams] | None,
    ) -> tuple[PlayType, PlayParams] | None:
        raise NotImplementedError

    async def _dispatch_play(
        self,
        play_type: PlayType,
        params: PlayParams,
        state: OrchestratorState,
        *,
        revalidate: bool | None = None,
    ) -> bool:
        raise NotImplementedError

    def _shutdown_allows_only_end_agent(self, state: OrchestratorState) -> bool:
        raise NotImplementedError

    async def _revalidate_end_session_before_dispatch(self) -> bool:
        raise NotImplementedError

    @staticmethod
    def _params_have_dispatch_target(params: PlayParams) -> bool:
        raise NotImplementedError

    async def _handle_masked_override(self, entry: OverrideEntry, reason: MaskReason | str) -> None:
        raise NotImplementedError

    async def _release_masked_override(
        self, entry: OverrideEntry, *, reason: MaskReason | str
    ) -> None:
        raise NotImplementedError

    async def _record_control_rejection(
        self,
        *,
        kind: str,
        play_type: PlayType,
        params: PlayParams,
        reason: MaskReason | str,
    ) -> None:
        raise NotImplementedError

    async def _drop_selected_play_before_dispatch(
        self,
        play_type: PlayType,
        params: PlayParams,
        *,
        reason: MaskReason | str,
        event: str,
    ) -> None:
        raise NotImplementedError

    async def _dispatch_revalidation_reason(
        self,
        play_type: PlayType,
        params: PlayParams,
        state: OrchestratorState,
    ) -> MaskReason | None:
        raise NotImplementedError

    async def _mark_pr_manual_required(self, pr_number: int) -> None:
        raise NotImplementedError

    async def _record_trajectory_snapshot(
        self, outcome: PlayOutcome, next_state: OrchestratorState
    ) -> None:
        raise NotImplementedError

    async def _persist_alignment_scores(self, outcome: PlayOutcome) -> None:
        raise NotImplementedError

    async def _update_learnings(self, outcome: PlayOutcome, play_type: PlayType) -> None:
        raise NotImplementedError

    async def _promote_request_play_mutations(self) -> None:
        raise NotImplementedError

    async def _resource_keys_for_request_play(
        self, play_type: PlayType, params: PlayParams
    ) -> list[str]:
        raise NotImplementedError

    async def _on_crash(self, agent_id: str, return_code: int) -> None:
        raise NotImplementedError

    async def _on_context_pressure(self, agent_id: str, ratio: float) -> None:
        raise NotImplementedError

    async def _fetch_state_data(self) -> _StateData:
        raise NotImplementedError

    async def _safe_get_latest_trajectory(self) -> object:
        raise NotImplementedError

    async def _abandon_work_for_missing_agents(self) -> None:
        raise NotImplementedError

    async def _release_claims_for_prolonged_idle_agents(self, state: OrchestratorState) -> None:
        raise NotImplementedError

    def _annotate_action_mask(self, state: OrchestratorState) -> None:
        raise NotImplementedError

    def _assemble_state(self, data: _StateData) -> OrchestratorState:
        raise NotImplementedError

    # Snapshot projection helpers (supplied by _SnapshotsMixin)
    def _build_agent_snapshots(self, play_history: list[PlayRecord]) -> list[AgentSnapshot]:
        raise NotImplementedError

    @staticmethod
    def _project_open_issues(
        records: list[GitHubIssueRecord], graph: ProjectGraph | None
    ) -> list[IssueSnapshot]:
        raise NotImplementedError

    @staticmethod
    def _project_pull_requests(
        records: list[PullRequestRecord],
    ) -> list[PullRequestSnapshot]:
        raise NotImplementedError

    @staticmethod
    def _compute_play_streaks(
        play_history: list[PlayRecord],
        *,
        override_play_ids: set[int] | None = None,
    ) -> tuple[int, int]:
        raise NotImplementedError

    @staticmethod
    def _compute_play_recency(
        play_history: list[PlayRecord],
    ) -> tuple[
        PlayType | None,
        int | None,
        dict[PlayType, int],
        dict[PlayType, bool],
        dict[PlayType, bool],
        int | None,
        dict[PlayType, int],
    ]:
        raise NotImplementedError

    def _build_budget_snapshot(self, total_plays: int, total_cost: float) -> BudgetSnapshot:
        raise NotImplementedError

    @staticmethod
    def _extract_trajectory(
        record: TrajectorySnapshotRecord | None,
    ) -> TrajectorySnapshot | None:
        raise NotImplementedError

    def _compute_trajectory_record(
        self,
        outcome: PlayOutcome,
        next_state: OrchestratorState,
        history: list[PlayRecord],
    ) -> TrajectorySnapshotRecord | None:
        raise NotImplementedError

    @staticmethod
    def _compute_session_stats(
        play_history: list[PlayRecord],
    ) -> SessionStatsSnapshot:
        raise NotImplementedError
