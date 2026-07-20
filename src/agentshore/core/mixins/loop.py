"""Main loop, loop-detection ladder, stagnation escalation, and idle backoff."""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import hashlib
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Protocol, assert_never, cast

from agentshore.core.helpers import _logger, _ppo_selector_cls
from agentshore.core.tick_action import (
    Break,
    Continue,
    Dispatch,
    Pause,
    TickAction,
    WaitIdle,
    WaitInFlight,
)
from agentshore.plays.base import PlayParams
from agentshore.rl.constants import STAGNATION_ENTROPY_MULTIPLIER
from agentshore.rl.mask_reason import MaskReason
from agentshore.state import AgentStatus, PlaySkipReason, PlayType, SessionState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agentshore.core.main_repo_guard import MainRepoGuard
    from agentshore.core.mixins.completion import CompletionProcessor
    from agentshore.core.mixins.dispatch import Dispatcher
    from agentshore.core.mixins.drain import DrainController
    from agentshore.core.mixins.lifecycle import LifecycleController
    from agentshore.core.mixins.state import StateBuilder
    from agentshore.core.override_queue import OverrideQueue
    from agentshore.core.session_runtime import SessionRuntime
    from agentshore.core.velocity_tracker import VelocityTracker
    from agentshore.plays.candidates import PlayCandidatePlan
    from agentshore.plays.registry import PlayRegistry
    from agentshore.rl.config_head import ConfigKey
    from agentshore.state import (
        AgentSnapshot,
        OrchestratorState,
    )

    NaturalExitCallback = Callable[[str], Awaitable[None]]


@dataclasses.dataclass(frozen=True)
class SkipDiagnosis:
    """Why nothing was dispatched this tick — the shared skip-classification.

    Built once by ``compute_skip_diagnosis`` and consumed by every site that
    needs to emit ``play_skipped`` / decide an idle wait: the in-flight
    selector-None path, the truly-idle ``continue_if_selector_idle_work_remains``
    path, and any future autonomous-stop classifier. Bundles the candidate plan
    (so callers can read ``work_availability`` / ``has_remaining_work`` without
    rebuilding it), the top mask reasons, and the resolved ``PlaySkipReason``.
    """

    candidate_plan: PlayCandidatePlan
    reason_counts: list[dict[str, object]]
    skip_reason: PlaySkipReason


ISSUE_REFRESH_INTERVAL_SECONDS = 120
AGENT_PING_TIMEOUT_SECONDS = 0.5
IPC_TIMEOUT_SECONDS = 1.0

# Fibonacci idle backoff indexed by idle_streak: 1s on a fresh-state tick,
# stretching while the selection digest is unchanged. Capped at 21s so override
# pushes / human pauses are picked up even if no play wakes the loop.
_IDLE_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0)
_WAITING_BACKOFF_SECONDS: tuple[float, ...] = (5.0, 10.0, 20.0, 30.0, 60.0)

# desktop-kqo5: idle-with-work ticks under a latched trunk-dispatch pause
# (nothing in flight) before auto-stopping via drain. RECONCILE_STATE should
# clear the pause within a few ticks; this is the last-resort escape so the loop
# never idles forever on a trunk it cannot heal. ~3-4 min at the 21s ceiling.
_WEDGED_IDLE_STOP_TICKS = 12

# Wall-clock seconds the *whole* fleet may sit idle (every agent idle, nothing
# in flight) before a clean autonomous drain — the end-session-wedge backstop so
# a fully-masked-into-a-corner fleet can't poll forever. Wall-clock not ticks
# because idle_backoff varies tick spacing. Measures *productive* idle: lifecycle
# churn (INSTANTIATE_AGENT <-> END_AGENT) doesn't reset it (#166).
_FLEET_IDLE_END_SESSION_SECONDS: float = 1200.0

# Consecutive failsafe END_SESSION attempts vetoed by the revalidation gate
# before the deterministic backstop forces a drain. PPO's reverse-failsafe gets
# first crack; if blocked this many times the backstop fires. ~12 min at the 90s
# failsafe cadence — inside the 20-min budget reserve so a bookkeeping-wedged
# session drains in minutes, not at the budget cutoff (#255). Honours the
# "PPO drives, deterministic code only backstops" invariant.
_END_SESSION_REVALIDATION_WEDGE_LIMIT = 8

# Pure fleet-management plays; in-flight/dispatch of these is NOT productive
# activity for the fleet-idle backstop (a session that only churns them is idle).
_LIFECYCLE_PLAY_TYPES: frozenset[PlayType] = frozenset(
    {PlayType.INSTANTIATE_AGENT, PlayType.END_AGENT}
)

# Budget-countdown heartbeat cadence: a budget-only frame so the dashboard's
# remaining-time keeps ticking during quiet stretches with no full state update.
# Budget-only so the office sprites never re-process and jitter. 30s keeps the
# displayed minute fresh to within ~30s + one idle-backoff (≤21s).
_BUDGET_HEARTBEAT_SECONDS: float = 30.0

# Times an unanswered loop-detection auto-stop is deferred while actionable work
# (merge-ready PRs / workable issues) remains. Each reprieve lifts the pause and
# resumes; once exhausted the auto-stop drains so a stuck session still ends (#9).
_AUTO_STOP_WORK_REPRIEVE_LIMIT = 2

# Loop-liveness watchdog (#9): how often the independent watchdog wakes to compare
# the loop heartbeat against the timeout. Well below the 600s default so a blocked
# loop is reaped within ~one interval of the deadline without busy-polling.
_LOOP_LIVENESS_CHECK_INTERVAL_SECONDS = 15.0

# Per-tick circuit-breaker: consecutive run_until_idle ticks that raise before the
# loop drains gracefully instead of spinning. Streak resets on any clean tick; set
# above transient noise but low enough a permanently-throwing tick drains fast.
_MAX_CONSECUTIVE_TICK_FAILURES = 10


class _LoopHost(Protocol):
    """Orchestrator *behaviour* the :class:`LoopRunner` invokes.

    All shared session *state* now lives on :class:`SessionRuntime` (reached via
    ``self._runtime``); this Protocol is the narrow behaviour seam that remains so
    the cross-component methods resolve on the composition root without a circular
    import. ``_OrchestratorBase`` structurally satisfies it.
    """

    async def _safe_call(self, coro: Awaitable[object], label: str) -> None: ...

    def _selector_config_index(self) -> tuple[ConfigKey, ...] | None: ...


class LoopRunner:
    """The main orchestration loop plus loop-detection and stagnation laddering.

    The loop is the conductor: ``run_until_idle`` / ``_run_loop_body`` drive
    every other component, so it holds references to all of them. Stable
    services / collaborators (the ``session_id``, the sibling components
    ``state_builder``/``dispatcher``/``completion``/``lifecycle``/``drain``, and
    the 1a collaborators ``main_repo``/``overrides``/``velocity``) are captured
    via the constructor; all orchestrator runtime/control state (read or
    written) flows through the :class:`_LoopHost` Protocol so SIGHUP and per-tick
    mutation never goes stale. Loop-only counters (tick-failure streak, wedge
    counter, watchdog handle, heartbeat, fleet-idle latch, warning memos,
    stagnation stage) are owned here.
    """

    def __init__(
        self,
        *,
        host: _LoopHost,
        runtime: SessionRuntime,
        session_id: str,
        main_repo: MainRepoGuard,
        overrides: OverrideQueue,
        velocity: VelocityTracker,
        state_builder: StateBuilder,
        dispatcher: Dispatcher,
        completion: CompletionProcessor,
        lifecycle: LifecycleController,
        drain: DrainController,
    ) -> None:
        self._host = host
        self._runtime = runtime
        self._session_id = session_id
        self._main_repo = main_repo
        self._overrides = overrides
        self._velocity = velocity
        self._state_builder = state_builder
        self._dispatcher = dispatcher
        self._completion = completion
        self._lifecycle = lifecycle
        self._drain = drain
        # Loop-only counters/latches (read only inside loop methods).
        self._last_warned_failure_streak: int | None = None
        self._last_warned_any_streak: int | None = None
        self._last_stagnation_stage: int = 0
        # desktop-85ex: True while inside a fleet-idle persistent window (idle
        # streak ≥ fleet_idle_threshold + nothing in flight). One info event on
        # each transition; flips False the first tick anything dispatches.
        self._fleet_idle_persistent_active: bool = False
        # Idle-with-work ticks under a latched trunk pause, nothing in flight —
        # the wedge signature. Drives the auto-stop; reset on any dispatch.
        self._wedged_idle_ticks: int = 0
        # Monotonic ts the fleet last became *fully* idle (every agent idle,
        # nothing in flight), else None. Drives the fleet-idle end-session
        # backstop; reset to None on any dispatch / busy agent.
        self._fleet_idle_since: float | None = None
        # Bounded reprieves for an unanswered loop-detection auto-stop while
        # actionable work remains, so finished work isn't torn down. Resets per loop.
        self._auto_stop_reprieves_used: int = 0
        # Loop-liveness watchdog task handle (#9).
        self._loop_liveness_task: asyncio.Task[None] | None = None
        # Loop-liveness heartbeat (#9): monotonic ts stamped atop every
        # run_until_idle iteration; the watchdog reads it to detect a frozen loop.
        # inf until the first stamp so a never-started loop never looks stale.
        self._last_loop_iteration_at: float = float("inf")
        # Per-tick guard circuit-breaker counter (see _MAX_CONSECUTIVE_TICK_FAILURES).
        self._tick_failure_streak: int = 0
        # Consecutive failsafe END_SESSION attempts vetoed by the revalidation gate
        # (see _END_SESSION_REVALIDATION_WEDGE_LIMIT). Drives the force-drain
        # backstop in _resolve_tick; reset on any dispatch (#255).
        self._end_session_revalidation_blocks: int = 0
        # Monotonic timestamp of the last budget-countdown heartbeat emit. 0.0
        # until the first one fires (so the first eligible tick emits promptly).
        self._last_budget_heartbeat_at: float = 0.0

    async def check_stagnation_escalation(self, state: OrchestratorState) -> bool:
        """Stagnation ladder (warn+entropy at 5, surface at 10, pause at 15)."""
        if (
            self._runtime.draining
            or self._runtime.stop_requested
            or state.session_state in {SessionState.DRAINING, SessionState.SHUTTING_DOWN}
        ):
            return False

        warn_after = self._runtime.cfg.rl.stagnation.warn_after
        alert_after = self._runtime.cfg.rl.stagnation.alert_after
        pause_after = self._runtime.cfg.rl.stagnation.pause_after

        stagnation = 0
        if self._runtime.metrics is not None:
            ctx = await self._runtime.metrics.snapshot(state)
            stagnation = int(ctx.stagnation_counter)

        if stagnation >= pause_after:
            stage = 3
        elif stagnation >= alert_after:
            stage = 2
        elif stagnation >= warn_after:
            stage = 1
        else:
            stage = 0

        if stage == 0:
            self._last_stagnation_stage = 0
            if isinstance(self._runtime.selector, _ppo_selector_cls()):
                self._runtime.selector.set_entropy_coef(self._runtime.cfg.rl.entropy_coef)
            return False

        if stage > self._last_stagnation_stage:
            for next_stage in range(self._last_stagnation_stage + 1, stage + 1):
                threshold = {1: warn_after, 2: alert_after, 3: pause_after}[next_stage]
                payload: dict[str, object] = {
                    "streak": stagnation,
                    "threshold": threshold,
                    "session_id": self._session_id,
                }
                if next_stage == 1:
                    if isinstance(self._runtime.selector, _ppo_selector_cls()):
                        boosted = self._runtime.cfg.rl.entropy_coef * STAGNATION_ENTROPY_MULTIPLIER
                        self._runtime.selector.set_entropy_coef(boosted)
                        payload["entropy_coef"] = boosted
                        payload["entropy_boost_multiplier"] = STAGNATION_ENTROPY_MULTIPLIER
                    payload["action"] = "boost_exploration"
                elif next_stage == 2:
                    payload["action"] = "surface_to_human"
                    payload["suggestion"] = (
                        "Review beads graph, switch agent mix, or end the session."
                    )
                else:
                    payload["action"] = "auto_pause_requires_resume"
                _logger.warning("stagnation_detected", **payload)

        self._last_stagnation_stage = stage
        return stage >= 2

    def idle_backoff(self, wait_class: str = "default") -> float:
        """Current backoff seconds, indexed by ``_idle_streak``."""
        backoff = (
            _WAITING_BACKOFF_SECONDS
            if wait_class in {"waiting_for_capacity", "waiting_for_in_flight_resource"}
            else _IDLE_BACKOFF_SECONDS
        )
        idx = min(self._runtime.idle_streak, len(backoff) - 1)
        return backoff[idx]

    def classify_selector_idle(
        self,
        state: OrchestratorState,
        reason_counts: list[dict[str, object]],
    ) -> str:
        """Classify selector-idle waits for logging severity and backoff."""

        from agentshore.rl.selector import _only_capacity_waiting

        if not self._runtime.in_flight and not state.in_flight_plays:
            if _only_capacity_waiting(reason_counts):
                return "waiting_for_capacity"
            return "resolver_exhausted"
        if _only_capacity_waiting(reason_counts):
            return "waiting_for_capacity"
        return "waiting_for_in_flight_resource"

    @staticmethod
    def classify_play_skipped_reason(
        state: OrchestratorState,
        reason_counts: list[dict[str, object]],
        *,
        candidate_plan_has_work: bool,
    ) -> PlaySkipReason:
        """Pick a structured ``PlaySkipReason`` for the current tick.

        Called when ``select_play`` returned ``None`` (post-rni0 the loop's
        only "wait" path). The ordering is deliberate:

        1. ``engine_paused``       — session is in a non-running state.
        2. ``cooldown_active``     — the dominant mask reason is a cooldown.
        3. ``all_masked``          — there is workable graph but every play
                                     type is masked. Payload includes top
                                     ``mask_reasons`` so log post-processing
                                     can resolve the root cause.
        4. ``no_eligible_targets`` — the mask permits play types, but no
                                     concrete candidate (workable issue,
                                     reviewable PR, …) resolves.
        5. ``selector_returned_none`` — fallback. Selector ran, produced no
                                     dispatch, and none of the above
                                     narrower diagnoses fit. This is the
                                     normal post-rni0 "fleet caught up,
                                     waiting" steady-state.

        ``value_dominated_by_idle`` is deprecated post-rni0 (idle was a play
        before; now a loop wait, never a selector pick) but kept in the
        ``PlaySkipReason`` enum so log consumers see a stable surface
        through the rollout window.
        """
        if state.session_state in {
            SessionState.PAUSED,
            SessionState.DRAINING,
            SessionState.SHUTTING_DOWN,
        }:
            return "engine_paused"

        # Cooldown / recency cap is a more specific diagnosis than ``all_masked``.
        if reason_counts:
            top_reason = reason_counts[0].get("reason")
            if isinstance(top_reason, MaskReason):
                low = top_reason.text.lower()
                if "cooldown" in low or "recency" in low:
                    return "cooldown_active"
            elif isinstance(top_reason, str):
                low = top_reason.lower()
                if "cooldown" in low or "recency" in low:
                    return "cooldown_active"

        if reason_counts:
            # Mask blocked every candidate. Caller attaches top mask_reasons.
            return "all_masked"

        if candidate_plan_has_work:
            # Workable graph but resolver found no concrete target this tick.
            return "no_eligible_targets"

        return "selector_returned_none"

    async def emit_play_skipped(
        self,
        state: OrchestratorState,
        *,
        reason: PlaySkipReason,
        mask_reasons: list[dict[str, object]],
        candidate_plan_has_work: bool,
    ) -> None:
        """Emit the structured ``play_skipped`` info event (desktop-85ex).

        The payload is intentionally compact:

        * ``reason``                       — one of ``PlaySkipReason``.
        * ``event_source``                 — ``"loop_idle"`` so log
                                             consumers can distinguish from
                                             the executor-time ``"executor"``
                                             variant emitted in
                                             ``completion.py``.
        * ``session_id``                   — for cross-session log joins.
        * ``idle_streak``                  — current consecutive idle count.
        * ``mask_reasons`` (``all_masked`` /
          ``cooldown_active`` only)        — top 5 (mask_reason, count) pairs.
        * ``has_remaining_work``           — candidate-plan boolean so log
                                             consumers can disambiguate
                                             ``no_eligible_targets`` from a
                                             genuinely empty graph.

        Replaces the prior free-text ``selector_idle`` line.  The structured
        shape lets ``agentshore.log → metrics`` post-processing diagnose fleet
        idle storms without grep-and-pray.
        """
        payload: dict[str, object] = {
            "reason": reason,
            "event_source": "loop_idle",
            "session_id": self._session_id,
            "idle_streak": self._runtime.idle_streak,
            "has_remaining_work": candidate_plan_has_work,
        }
        if reason in {"all_masked", "cooldown_active"} and mask_reasons:
            payload["mask_reasons"] = mask_reasons
        _skip_log = _logger.debug if self._runtime.idle_streak > 1 else _logger.info
        _skip_log("play_skipped", **payload)

    def compute_skip_diagnosis(self, state: OrchestratorState) -> SkipDiagnosis:
        """Build the candidate plan + top mask reasons + ``PlaySkipReason`` once.

        Single source of truth for the "why was nothing dispatched" computation
        that was previously inlined verbatim at every selector-idle site (the
        in-flight selector-None path and the truly-idle
        ``continue_if_selector_idle_work_remains`` path). Pure: builds state-
        derived structures only, mutating nothing.
        """
        from agentshore.plays.candidates import build_candidate_plan
        from agentshore.rl.mask import compute_mask_reasons

        candidate_plan = build_candidate_plan(state)
        reason_counts: list[dict[str, object]] = []
        if self._runtime.registry is not None:
            counts = collections.Counter(
                compute_mask_reasons(
                    state,
                    cast("PlayRegistry", self._runtime.registry),
                    cfg=self._runtime.cfg,
                    config_index=self._host._selector_config_index(),
                    apply_reverse_failsafe=self._runtime.cfg.rl.reverse_failsafe_enabled,
                    candidate_plan=candidate_plan,
                ).values()
            )
            reason_counts = [
                {"reason": mask_reason, "count": count}
                for mask_reason, count in counts.most_common(5)
            ]
        skip_reason = self.classify_play_skipped_reason(
            state,
            reason_counts,
            candidate_plan_has_work=candidate_plan.has_remaining_work,
        )
        return SkipDiagnosis(
            candidate_plan=candidate_plan,
            reason_counts=reason_counts,
            skip_reason=skip_reason,
        )

    async def emit_structured_play_skipped_for_current_tick(
        self,
        state: OrchestratorState,
    ) -> None:
        """Compute mask reasons + classify + emit ``play_skipped`` once.

        Convenience wrapper for the in-flight selector-None branch so it
        doesn't have to duplicate ``continue_if_selector_idle_work_remains``
        plumbing. Skips the ``fleet_idle_persistent`` check because the loop
        is not actually idle — work is still in flight.
        """
        diagnosis = self.compute_skip_diagnosis(state)
        await self.emit_play_skipped(
            state,
            reason=diagnosis.skip_reason,
            mask_reasons=diagnosis.reason_counts,
            candidate_plan_has_work=diagnosis.candidate_plan.has_remaining_work,
        )

    async def check_fleet_idle_persistent(
        self,
        state: OrchestratorState,
        *,
        reason: PlaySkipReason,
        mask_reasons: list[dict[str, object]],
    ) -> None:
        """Emit ``fleet_idle_persistent`` on transitions only (desktop-85ex).

        Memory ``project_loop_detector_warning_storm`` documents the bug
        pattern where ``loop_detected`` re-emitted per tick instead of per
        streak transition. This sibling event must NOT repeat the mistake.

        Enter condition  (active=False → True): idle streak crossed the
        configured threshold AND no in-flight work.
        Exit condition   (active=True  → False): something is dispatching
        again (``_in_flight`` non-empty) OR streak collapsed below
        threshold.

        Both transitions emit exactly one ``fleet_idle_persistent`` info
        event. Steady-state ticks inside the window emit nothing.
        """
        threshold = self._runtime.cfg.rl.loop_detection.fleet_idle_threshold
        in_flight_empty = not self._runtime.in_flight and not state.in_flight_plays
        should_be_active = self._runtime.idle_streak >= threshold and in_flight_empty

        if should_be_active and not self._fleet_idle_persistent_active:
            payload: dict[str, object] = {
                "session_id": self._session_id,
                "idle_streak": self._runtime.idle_streak,
                "threshold": threshold,
                "dominant_reason": reason,
                "transition": "entered",
            }
            if mask_reasons:
                payload["mask_reasons"] = mask_reasons
            _logger.info("fleet_idle_persistent", **payload)
            self._fleet_idle_persistent_active = True
        elif not should_be_active and self._fleet_idle_persistent_active:
            _logger.info(
                "fleet_idle_persistent",
                session_id=self._session_id,
                idle_streak=self._runtime.idle_streak,
                threshold=threshold,
                transition="exited",
            )
            self._fleet_idle_persistent_active = False

    def selection_state_digest(
        self,
        state: OrchestratorState,
        idle_agents: list[AgentSnapshot],
    ) -> bytes:
        """Compact hash of the inputs the selector / override resolver would see.

        Skipping ``select_play`` when this is unchanged eliminates the
        ``selector_idle`` storm during long-running plays, where the loop
        would otherwise re-run the selector once per second against an
        identical state. Inputs:

        - Set of idle agent ids (selector only dispatches to idle agents).
        - ``len(self._runtime.in_flight)`` (a play completion changes this and is
          worth re-selecting on).
        - ``state.total_plays`` ensures every completed play bumps the digest,
          even when in_flight cycles back to the same count between iterations.
        - ``state.action_mask`` (mask transitions ⇒ new plays may be eligible).
        - Whether an override or seed override is queued.
        - Session state (running / paused / stopped).
        - Open-issue + open-PR counts as a coarse GitHub-delta proxy.
        """
        h = hashlib.blake2b(digest_size=16)
        idle_ids = sorted(a.agent_id for a in idle_agents)
        for agent_id in idle_ids:
            h.update(agent_id.encode())
            h.update(b"|")
        h.update(b";")
        h.update(len(self._runtime.in_flight).to_bytes(4, "little"))
        h.update(b";")
        h.update(state.total_plays.to_bytes(4, "little"))
        h.update(b";")
        if state.action_mask:
            h.update(bytes(state.action_mask))
        h.update(b";")
        override_pending = (
            self._overrides.first_play_override is not None or not self._overrides.empty()
        )
        h.update(b"o" if override_pending else b".")
        h.update(b";")
        h.update(state.session_state.value.encode())
        h.update(b";")
        h.update(len(state.open_issues).to_bytes(4, "little"))
        h.update(b";")
        h.update(len(state.pull_requests).to_bytes(4, "little"))
        return h.digest()

    async def continue_if_selector_idle_work_remains(
        self,
        state: OrchestratorState,
        *,
        reason: str,
    ) -> bool:
        """Keep the loop alive when selection idles while visible work remains.

        Short-circuits to False when draining and no live agents remain —
        the drain is complete and the loop should exit rather than sleeping
        on the candidate-plan work signal (which sees open issues forever).

        Also emits the structured ``play_skipped`` event (desktop-85ex)
        carrying a ``PlaySkipReason`` enum value plus, when relevant, the
        top mask reasons. The legacy ``selector_idle_with_work`` line is
        retained as a debug/warning depending on wait_class so existing
        dashboards keep working — operators upgrade to the new event at
        their own pace.
        """
        # desktop-kqo5 wedge auto-stop: a latched trunk-dispatch pause blocks all
        # plays but END_AGENT/RECONCILE_STATE. If RECONCILE_STATE can't heal the
        # trunk within the grace window (nothing in flight), escalate to a clean
        # drain. Gated on pause + no in-flight so capacity-idle never trips it.
        if self._main_repo.dispatch_paused and not self._runtime.in_flight:
            self._wedged_idle_ticks += 1
            if self._wedged_idle_ticks >= _WEDGED_IDLE_STOP_TICKS:
                _logger.error(
                    "main_repo_wedged_auto_stop",
                    session_id=self._session_id,
                    wedged_idle_ticks=self._wedged_idle_ticks,
                    note=(
                        "trunk-dispatch pause did not clear (reconcile could not "
                        "heal the main checkout); auto-stopping via drain"
                    ),
                )
                await self.initiate_autonomous_stop("main_repo_wedged", arm_gate_only=True)
                return False
        else:
            self._wedged_idle_ticks = 0

        # Fleet-idle end-session backstop: if every agent is idle with nothing in
        # flight past _FLEET_IDLE_END_SESSION_SECONDS, end via clean drain rather
        # than poll forever. fire_natural_exit=True stamps _natural_exit_reason so
        # run_until_idle fires session.completed + normal teardown (a bare break
        # would strand the process at 0 CPU). "Productively idle" = no work play in
        # flight; lifecycle-only churn (and its transient BUSY) counts as idle, so
        # the deadline accrues through an INSTANTIATE_AGENT <-> END_AGENT cycle (#166).
        productive_in_flight = any(pt not in _LIFECYCLE_PLAY_TYPES for pt in state.in_flight_plays)
        fleet_fully_idle = not productive_in_flight
        if fleet_fully_idle:
            if self._fleet_idle_since is None:
                self._fleet_idle_since = time.monotonic()
            idle_seconds = time.monotonic() - self._fleet_idle_since
            if idle_seconds >= _FLEET_IDLE_END_SESSION_SECONDS:
                # Check whether the graph still reports workable work at the
                # moment the backstop fires. A non-empty count here means the
                # session didn't run out of real work — something upstream
                # (mask, resolver, or a mis-cleared gate label) kept excluding
                # it from candidacy for the whole idle window, which is a
                # distinct anomaly from a genuinely empty backlog and worth a
                # different alert so it doesn't have to be reverse-engineered
                # from raw NDJSON after the fact.
                availability = self.compute_skip_diagnosis(state).candidate_plan.work_availability
                backlog_remaining = (
                    availability.workable_issue_count > 0 or availability.ready_task_count > 0
                )
                event = (
                    "fleet_idle_end_session_with_backlog_remaining"
                    if backlog_remaining
                    else "fleet_idle_end_session"
                )
                _logger.warning(
                    event,
                    session_id=self._session_id,
                    idle_seconds=round(idle_seconds),
                    limit_seconds=_FLEET_IDLE_END_SESSION_SECONDS,
                    workable_issues=availability.workable_issue_count,
                    ready_tasks=availability.ready_task_count,
                    note=(
                        "whole fleet idle past limit with nothing in flight — "
                        "ending the session cleanly via drain"
                        if not backlog_remaining
                        else "whole fleet idle past limit despite workable backlog remaining — "
                        "likely a stuck candidate (e.g. a mask/label exclusion that never "
                        "cleared) rather than a genuine work shortage; ending the session "
                        "cleanly via drain"
                    ),
                )
                await self.initiate_autonomous_stop("fleet_idle_timeout", fire_natural_exit=True)
                return False
        else:
            self._fleet_idle_since = None

        diagnosis = self.compute_skip_diagnosis(state)
        candidate_plan = diagnosis.candidate_plan
        availability = candidate_plan.work_availability
        candidate_plan_has_work = candidate_plan.has_remaining_work
        reason_counts = diagnosis.reason_counts
        skip_reason = diagnosis.skip_reason
        await self.emit_play_skipped(
            state,
            reason=skip_reason,
            mask_reasons=reason_counts,
            candidate_plan_has_work=candidate_plan_has_work,
        )
        await self.check_fleet_idle_persistent(
            state,
            reason=skip_reason,
            mask_reasons=reason_counts,
        )
        if not candidate_plan_has_work:
            # Issue #562: ``has_remaining_work`` is a *graph* signal, NOT
            # correlated with the action mask. Post idle_tick removal (PR #535)
            # the mask can have eligible slots while the graph says "no work" —
            # exiting here would strand a session PPO can still work. If any mask
            # slot is True, wait (sleep + keep going); the next state transition
            # re-runs the selector.
            if any(state.action_mask):
                _logger.debug(
                    "selector_idle_mask_has_plays",
                    reason=reason,
                    session_id=self._session_id,
                    idle_streak=self._runtime.idle_streak,
                    mask_true_count=sum(1 for slot in state.action_mask if slot),
                    top_mask_reasons=reason_counts,
                )
                await asyncio.sleep(self.idle_backoff("waiting_for_capacity"))
                return True
            # Every slot masked AND graph says no work. Meaning depends on selector:
            # * Live PPO — common, transient (agents mid-issue, work reconciling);
            #   NOT terminal, must NOT end the session or we tear down live work.
            #   Keep polling; a live session only ends via the fleet-idle backstop
            #   above or once PPO selects END_SESSION. (Bare return False would park:
            #   breaks run_until_idle without _natural_exit_reason, so the sidecar
            #   supervisor returns without stop().)
            # * Scripted/replay selector — exhausted plan is terminal; harness owns
            #   teardown. Break the loop.
            if isinstance(self._runtime.selector, _ppo_selector_cls()):
                _logger.debug(
                    "selector_idle_all_masked_keep_polling",
                    reason=reason,
                    session_id=self._session_id,
                    idle_streak=self._runtime.idle_streak,
                    top_mask_reasons=reason_counts,
                )
                await asyncio.sleep(self.idle_backoff("waiting_for_capacity"))
                return True
            return False
        wait_class = self.classify_selector_idle(state, reason_counts)

        log = (
            _logger.debug
            if wait_class in {"waiting_for_capacity", "waiting_for_in_flight_resource"}
            else _logger.warning
        )
        log(
            "selector_idle_with_work",
            reason=reason,
            wait_class=wait_class,
            session_id=self._session_id,
            idle_streak=self._runtime.idle_streak,
            tracked_issues=availability.tracked_issue_count,
            github_open_issues=availability.github_open_issue_count,
            workable_issues=availability.workable_issue_count,
            implementation_eligible=availability.implementation_eligible_count,
            bead_in_progress_issues=availability.bead_in_progress_issue_count,
            ready_tasks=availability.ready_task_count,
            backlog_sync_work=availability.backlog_sync_work_count,
            actionable_pr_work=availability.actionable_pr_work_count,
            beads_blocks_issue_pickup=availability.beads_blocks_issue_pickup,
            top_mask_reasons=reason_counts,
        )
        await asyncio.sleep(self.idle_backoff(wait_class))
        return True

    async def actionable_work_remains(self) -> tuple[bool, int, int]:
        """Return (has_actionable_work, mergeable_pr_count, workable_issue_count).

        ``actionable work`` = a merge-ready PR (finished, approved, reviewed
        work waiting only to land) or a workable issue. Used by the auto-stop
        guard so the session is not torn down on top of it.
        """
        from agentshore.plays.candidates import build_candidate_plan

        # Guarded (WS3 item B): a state-build / candidate-plan failure here must
        # not crash the loop, and must not wrongly grant a reprieve. Fail closed —
        # report no work so a genuinely stuck session still drains.
        try:
            state = await self._state_builder.build_state()
            wa = build_candidate_plan(state).work_availability
        except Exception as exc:
            _logger.error(
                "actionable_work_check_failed",
                session_id=self._session_id,
                error=str(exc),
                exc_info=True,
            )
            return False, 0, 0
        mergeable = wa.mergeable_pr_count
        workable = wa.workable_issue_count
        has_work = mergeable > 0 or wa.actionable_pr_work_count > 0 or workable > 0
        return has_work, mergeable, workable

    async def initiate_autonomous_stop(
        self,
        reason: str,
        *,
        arm_gate_only: bool = False,
        fire_natural_exit: bool = False,
        clear_pause_deadline: bool = False,
    ) -> None:
        """Single entry point for every autonomous (non-operator) session stop.

        The five autonomous-stop paths (forward-progress monitor, wedged-trunk
        pause, unanswered-feedback pause, loop-liveness watchdog, tick-failure
        breaker) share the same drain-flag set + ``begin_drain`` shape; only the
        trigger condition and a couple of side-flags differ. They route through
        here so the stop taxonomy lives in one place.

        Modes (behaviour-preserving — each prior call site maps onto exactly
        one combination):

        * ``arm_gate_only=True`` — set the drain flags and wake the pause gate so
          the loop reaches ``begin_drain`` on its next iteration. Used by the
          in-loop wedged-trunk and unanswered-pause paths, which run inside the
          pause gate where the loop has not yet hit drain-init.
        * default — set the drain reason and call ``begin_drain`` immediately.
          Used by the forward-progress monitor, the tick-failure breaker, and the
          watchdog (which then also calls ``stop`` directly).

        ``fire_natural_exit`` stamps ``_natural_exit_reason`` so the natural-exit
        callback fires on loop exit. ``clear_pause_deadline`` resets the
        unanswered-pause deadline.
        """
        self._runtime.drain_reason = reason
        if fire_natural_exit:
            self._runtime.natural_exit_reason = reason
        if clear_pause_deadline:
            self._runtime.pause_deadline = None
        if arm_gate_only:
            self._runtime.draining = True
            # Unblock the gate so the loop proceeds to begin_drain next iteration.
            self._runtime.pause_event.set()
            return
        await self._drain.begin_drain(reason)

    async def auto_stop_unanswered_pause(self) -> None:
        """Auto-stop a feedback pause that went unanswered past its deadline (#9).

        Lifts the pause so the loop can reach the drain path, and requests a
        clean drain-based shutdown — the same teardown ``agentshore stop``
        performs (end_agent per agent, checkpoint, beads clear). Without this an
        unanswered loop-detection popup wedged the loop for hours, and the drain
        RPC could not be serviced while wedged. Emits ``loop_detection_prompt_timeout``
        so the auto-stop is visible in the NDJSON log (the wedge was silent).

        This covers genuine operator/feedback pauses that nobody answered.
        Autonomous no-progress stops are handled separately and directly by the
        forward-progress monitor (``_check_no_forward_progress`` → ``begin_drain``),
        so this path no longer needs the work/progress reprieve it once used to
        defer a blunt loop-detection pause.
        """
        _logger.warning(
            "loop_detection_prompt_timeout",
            session_id=self._session_id,
            pause_reason=self._runtime.pause_reason,
            timeout_seconds=self._runtime.cfg.feedback.unanswered_timeout_seconds,
            note="no human response within feedback.unanswered_timeout_seconds; auto-stopping",
        )
        await self.initiate_autonomous_stop(
            "loop_detection_prompt_timeout",
            arm_gate_only=True,
            clear_pause_deadline=True,
        )

    def loop_liveness_timeout_seconds(self) -> float | None:
        """Resolve the configured loop-liveness watchdog timeout (None disables)."""
        return self._runtime.cfg.feedback.loop_liveness_timeout_seconds

    def start_loop_liveness_watchdog(self) -> None:
        """Launch the independent loop-liveness watchdog task (#9).

        Idempotent. No-op when the timeout is unset (watchdog disabled) or a
        live task already exists. The task runs OFF the core loop so a
        hard-frozen loop — one that stopped iterating entirely, e.g. a deadlock
        in the play-mutation promotion path — still gets reaped. This is the
        backstop the idle/unanswered-pause auto-stops cannot provide: those
        require the loop to keep ticking, which a true freeze does not.
        """
        if self.loop_liveness_timeout_seconds() is None:
            return
        existing = self._loop_liveness_task
        if existing is not None and not existing.done():
            return
        self._loop_liveness_task = asyncio.get_event_loop().create_task(
            self._loop_liveness_watchdog(),
            name="agentshore.loop_liveness_watchdog",
        )

    def stop_loop_liveness_watchdog(self) -> None:
        """Cancel the loop-liveness watchdog task if running."""
        task = self._loop_liveness_task
        if task is not None and not task.done():
            task.cancel()

    async def _loop_liveness_watchdog(self) -> None:
        """Force-drain the session if the loop heartbeat goes stale (#9).

        Sleeps on a fixed check interval and compares ``now`` against
        ``_last_loop_iteration_at`` (stamped at the top of every
        ``run_until_idle`` iteration). When the gap exceeds the configured
        timeout the loop is presumed hard-frozen, so this task drives the
        teardown itself rather than only setting flags the dead loop would
        never service: it emits ``loop_liveness_timeout`` then drains and
        stops directly. Exits once a stop is in progress so it never double-runs
        the shutdown body (``stop`` is re-entrancy safe regardless).
        """
        interval = _LOOP_LIVENESS_CHECK_INTERVAL_SECONDS
        while not self._runtime.stop_requested and not self._runtime.stopped:
            await asyncio.sleep(interval)
            timeout = self.loop_liveness_timeout_seconds()
            if timeout is None:
                continue
            if self._runtime.stop_requested or self._runtime.stopped:
                return
            last = self._last_loop_iteration_at
            # 0.0 = loop has not begun iterating yet (not armed); inf = a
            # __new__-constructed instance with no real loop. Neither is stale.
            if last <= 0.0 or last == float("inf"):
                continue
            stalled_for = time.monotonic() - last
            if stalled_for < timeout:
                continue
            _logger.error(
                "loop_liveness_timeout",
                session_id=self._session_id,
                stalled_for_seconds=round(stalled_for, 1),
                timeout_seconds=timeout,
                in_flight=len(self._runtime.in_flight),
                note=(
                    "core loop heartbeat did not advance within "
                    "feedback.loop_liveness_timeout_seconds; loop presumed "
                    "hard-frozen — force-draining and stopping the session"
                ),
            )
            # Drive teardown off the (dead) loop: begin_drain (idempotent, records
            # reason + fires ESR) then stop() (full graceful shutdown, re-entrancy
            # safe). Guarded so a begin_drain failure can't kill the watchdog.
            await self._host._safe_call(
                self.initiate_autonomous_stop("loop_liveness_timeout"),
                "loop_liveness_begin_drain",
            )
            await self._drain.stop()
            return

    async def _maybe_emit_budget_heartbeat(self) -> None:
        """Emit a budget-only frame if the heartbeat cadence has elapsed.

        Throttled to ``_BUDGET_HEARTBEAT_SECONDS``. No-op when no time cap is set
        (nothing to count down) or before the first full state assembly has
        cached the dollar inputs. Budget-only so the dashboard refreshes just the
        remaining-time figure and never re-processes agents (no sprite jitter).
        A full state update that fires on the same tick is harmless — the
        dashboard applies whichever budget arrives last.
        """
        now = time.monotonic()
        if now - self._last_budget_heartbeat_at < _BUDGET_HEARTBEAT_SECONDS:
            return
        budget = self._state_builder.current_budget_snapshot()
        if budget is None:
            return
        self._last_budget_heartbeat_at = now
        await self._host._safe_call(
            self._runtime.state_provider.on_budget_update(budget),
            "on_budget_update_heartbeat",
        )

    async def run_until_idle(self) -> None:
        """Drive the RL loop until selector returns None or a stop is requested.

        Each iteration runs ``_run_loop_body`` (one tick: pause-gate, harvest,
        build state, terminate-check, detectors, idle-gate, select, dispatch)
        behind a per-tick guard so a single throwing tick can never kill the
        loop (the ``sidecar_orchestrator_run_failed`` silent-hang class). A
        permanently-throwing tick trips the circuit breaker and drains cleanly.
        """
        self._runtime.loop_started_at = time.monotonic()
        # Arm the loop-liveness heartbeat before the first iteration so the
        # watchdog (#9) has a fresh baseline and never sees a stale 0.0.
        self._last_loop_iteration_at = time.monotonic()

        while not self._runtime.stop_requested:
            # Loop-liveness heartbeat (#9): stamp every iteration so the watchdog
            # can detect a frozen loop. OUTSIDE the per-tick guard so it advances
            # even on a throwing tick (a fast-failing loop must not look frozen).
            self._last_loop_iteration_at = time.monotonic()
            # Budget-countdown heartbeat: emit a budget-only frame on a fixed
            # cadence so the dashboard's remaining-time keeps ticking during idle/
            # long-play ticks. Outside the per-tick guard so a heartbeat failure
            # never trips the loop breaker.
            await self._maybe_emit_budget_heartbeat()
            tick_raised = False
            try:
                should_break = await self._run_loop_body()
            except Exception as exc:
                # Per-tick guard: contain the failure, never let one tick kill
                # the loop. The breaker drains gracefully once failures persist.
                tick_raised = True
                if await self.handle_tick_failure(exc):
                    break
                continue
            finally:
                # A clean tick (no exception, including break/continue exits)
                # resets the breaker; an exception leaves the streak to escalate.
                if not tick_raised:
                    self._tick_failure_streak = 0
            if should_break:
                break

        # Natural-exit hook fires only when termination came from
        # _should_terminate (drain_complete, max_plays, timeout, shutting_down),
        # not from an external request_stop()/stop() call. The sidecar boot
        # wrapper uses this to fire session.completed (DESIGN §5.2).
        if (
            self._runtime.natural_exit_reason is not None
            and self._runtime.natural_exit_callback is not None
        ):
            await self._host._safe_call(
                self._runtime.natural_exit_callback(self._runtime.natural_exit_reason),
                "on_natural_exit_callback",
            )

    async def handle_tick_failure(self, exc: Exception) -> bool:
        """Handle an exception raised by one ``_run_loop_body`` tick.

        Returns True when the loop should stop (circuit breaker tripped → a
        graceful drain was initiated), False to back off briefly and retry. The
        backoff is bounded so a fast-throwing tick logs ~N times over a few
        seconds and then drains, rather than busy-logging thousands of times.
        """
        self._tick_failure_streak += 1
        _logger.error(
            "loop_tick_failed",
            session_id=self._session_id,
            error=str(exc),
            consecutive_failures=self._tick_failure_streak,
            in_flight=len(self._runtime.in_flight),
            idle_streak=self._runtime.idle_streak,
            exc_info=True,
        )
        if self._tick_failure_streak >= _MAX_CONSECUTIVE_TICK_FAILURES:
            _logger.error(
                "loop_circuit_breaker_tripped",
                session_id=self._session_id,
                consecutive_failures=self._tick_failure_streak,
                note=(
                    "run_until_idle tick raised on every recent iteration — "
                    "draining the session gracefully instead of spinning on the "
                    "failure or hanging silently"
                ),
            )
            await self._host._safe_call(
                self.initiate_autonomous_stop(
                    "tick_failure_circuit_breaker", fire_natural_exit=True
                ),
                "circuit_breaker_begin_drain",
            )
            return True
        await asyncio.sleep(min(self._tick_failure_streak * 0.5, 5.0))
        return False

    async def _run_loop_body(self) -> bool:
        """Run one RL-loop iteration. Returns True if the loop should break.

        Extracted from ``run_until_idle`` so each tick runs behind the per-tick
        guard there. The tick is split in two (03 H2): :meth:`_resolve_tick`
        performs the reads/harvests it needs and decides *what* the tick should
        do, returning a :class:`TickAction`; :meth:`_apply_tick_action` performs
        the single terminal effect and collapses it back to the break/continue
        ``bool`` (True breaks the loop, False re-iterates). Behavior is
        identical to the old monolithic body.
        """
        action = await self._resolve_tick()
        return await self._apply_tick_action(action)

    async def _resolve_tick(self) -> TickAction:
        """Decide what one RL-loop iteration should do.

        Performs the harvest/refresh/state-build/selection reads the decision
        needs and returns the terminal :class:`TickAction`. Every old
        ``return True`` becomes :class:`Break`, every ``return False`` becomes
        :class:`Continue`/:class:`WaitInFlight`/:class:`WaitIdle`/:class:`Pause`,
        and the dispatch tail becomes :class:`Dispatch`.
        """
        # Pause blocks new selection/dispatch, but in-flight completions must
        # still be harvested so completions/costs/rewards aren't stranded.
        if not self._runtime.pause_event.is_set():
            # Bound the wait by the unanswered-pause deadline (#9) so an
            # unanswered feedback pause auto-stops instead of wedging. remaining
            # is None when no deadline is armed (manual pause) → block-until-resume.
            remaining: float | None = None
            if self._runtime.pause_deadline is not None:
                remaining = max(0.0, self._runtime.pause_deadline - time.monotonic())
            pause_wait = asyncio.create_task(self._runtime.pause_event.wait())
            try:
                await asyncio.wait(
                    [pause_wait, *self._runtime.in_flight.values()],
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                if not pause_wait.done():
                    pause_wait.cancel()
                    with suppress(asyncio.CancelledError):
                        await pause_wait
            if self._runtime.stop_requested:
                return Break()
            if self._runtime.in_flight:
                # Harvest completions so they aren't stranded behind the gate.
                await self._completion.harvest_completed()
            if (
                not self._runtime.pause_event.is_set()
                and self._runtime.pause_deadline is not None
                and time.monotonic() >= self._runtime.pause_deadline
            ):
                # Unanswered feedback pause past its deadline → clean drain.
                await self.auto_stop_unanswered_pause()
            elif not self._runtime.pause_event.is_set():
                return Continue()

        if self._runtime.stop_requested:
            return Break()

        # Drain requested from sync context (e.g. signal handler) — initialize fully.
        if self._runtime.draining and not self._runtime.drain_initialized:
            await self._drain.begin_drain(self._runtime.drain_reason or "signal_sigterm")

        await self._completion.harvest_completed()

        # Periodic GitHub refresh fallback when no refresh-triggering play ran
        # recently (long run_qa / systematic_debugging / unblock_pr stretches).
        # Invalidates the digest to force a re-select; does NOT reset idle_streak —
        # idling through a refresh isn't less idle (desktop-mib1).
        if time.monotonic() - self._runtime.last_refresh_time > ISSUE_REFRESH_INTERVAL_SECONDS:
            await self._host._safe_call(
                self._completion.refresh_issues(), "refresh_issues_periodic"
            )
            self._runtime.last_selection_digest = None

        state = await self._state_builder.build_state()

        state = await self._lifecycle.begin_budget_reserve_drain_if_needed(state)

        should_stop, reason = self._lifecycle.should_terminate(state)
        if should_stop:
            if reason == "drain_complete":
                # Fire the one-shot drain-complete hook only on the genuine
                # drain-completion path with this tick's state — not from
                # stop_inner, which also runs for hard_stop.
                self._drain._on_drain_complete(state)
            _logger.info(
                "loop_terminating",
                reason=reason,
                session_id=self._session_id,
            )
            if reason is not None and reason != "stop_requested":
                self._runtime.natural_exit_reason = reason
            return Break()
        if reason is not None:
            # reason set but should_stop False → pause; loop blocks at wait() next iteration
            return Pause(reason)

        # PPO sees the full mask every tick: the eligibility mask zeros worker
        # plays when no IDLE agent matches, and compute_config_mask bounds
        # INSTANTIATE_AGENT per (type, tier) max. Nothing pickable → the
        # selection-is-None path below waits on in-flight.
        idle_agents = [a for a in state.agents if a.status == AgentStatus.IDLE]

        # Skip select_play (and its log line) when nothing the selector cares
        # about changed since last attempt. The ceiling tick re-evaluates
        # regardless, so a missed signal recovers within ~21s.
        digest = self.selection_state_digest(state, idle_agents)
        ceiling_tick = self._runtime.idle_streak >= len(_IDLE_BACKOFF_SECONDS) - 1
        if digest == self._runtime.last_selection_digest and not ceiling_tick:
            # Unchanged — idle without re-running the selector or its log line.
            return self._resolve_idle_tick(
                state,
                reason="unchanged_digest",
                log_selector_idle=False,
                emit_skipped=False,
            )

        self._runtime.last_selection_digest = digest

        override_play = await self._dispatcher.consume_override(state)

        selection = await self._dispatcher.select_play(state, override_play=override_play)
        # Fold this cycle's EligibilityAuthority confirm-repicks into the rolling
        # divergence window (obs slot executor_skip_rate_recent_50). Drain once per
        # cycle even on a None result — an all-repick None IS the divergence signal.
        self._velocity.record_selection_repicks(self._runtime.selector)
        if selection is None:
            # Selector idled on a fresh digest: log selector_idle and, when work
            # is in flight, emit a structured play_skipped before backing off.
            return self._resolve_idle_tick(
                state,
                reason="selector_none",
                log_selector_idle=True,
                emit_skipped=True,
            )

        # Selector picked a play. Defer idle-streak / fleet-idle resets until we
        # know it will *actually* dispatch — a revalidation-vetoed reverse-failsafe
        # END_SESSION must NOT reset the streak, else every blocked attempt re-pins
        # idle_streak at ~1 and only the time budget ends a wedged session (#255).
        play_type, params = selection

        if (
            self._dispatcher.shutdown_allows_only_end_agent(state)
            and play_type != PlayType.END_AGENT
        ):
            _logger.warning(
                "selection_blocked_during_shutdown",
                play_type=play_type.value,
                session_id=self._session_id,
            )
            if idle_agents:
                play_type, params = PlayType.END_AGENT, PlayParams()
            else:
                if self._runtime.in_flight:
                    return WaitInFlight("waiting_for_in_flight_resource")
                return Break()

        if play_type == PlayType.END_SESSION:
            failsafe = params.extras.get("reverse_failsafe") is True
            if not await self._dispatcher.revalidate_end_session_before_dispatch(failsafe=failsafe):
                if isinstance(self._runtime.selector, _ppo_selector_cls()):
                    self._runtime.selector.consume_pending()
                # A failsafe END_SESSION is the PPO trying to break a wedge. If the
                # gate vetoes it this many times in a row the session is wedged on
                # un-dispatchable bookkeeping; force a clean drain rather than poll
                # until the time budget (#255).
                if failsafe:
                    self._end_session_revalidation_blocks += 1
                    if (
                        self._end_session_revalidation_blocks
                        >= _END_SESSION_REVALIDATION_WEDGE_LIMIT
                    ):
                        _logger.warning(
                            "end_session_revalidation_wedged",
                            session_id=self._session_id,
                            blocks=self._end_session_revalidation_blocks,
                            limit=_END_SESSION_REVALIDATION_WEDGE_LIMIT,
                        )
                        await self.initiate_autonomous_stop(
                            "end_session_revalidation_wedged", fire_natural_exit=True
                        )
                return Continue()

        # Dispatching for real — reset idle bookkeeping. If inside a fleet-idle
        # persistent window (desktop-85ex), emit the exit transition once before
        # clearing the flag (the second bookend event, per
        # project_loop_detector_warning_storm).
        if self._fleet_idle_persistent_active:
            _logger.info(
                "fleet_idle_persistent",
                session_id=self._session_id,
                idle_streak=self._runtime.idle_streak,
                threshold=self._runtime.cfg.rl.loop_detection.fleet_idle_threshold,
                transition="exited",
            )
            self._fleet_idle_persistent_active = False
        self._runtime.idle_streak = 0
        self._wedged_idle_ticks = 0
        self._end_session_revalidation_blocks = 0
        # Only a *productive* pick resets the fleet-idle deadline; a lifecycle-only
        # pick must not, or an instantiate<->end cycle pins the clock at zero and
        # the backstop never fires (#166).
        if play_type not in _LIFECYCLE_PLAY_TYPES:
            self._fleet_idle_since = None
        return Dispatch(play_type, params, state)

    def _resolve_idle_tick(
        self,
        state: OrchestratorState,
        *,
        reason: str,
        log_selector_idle: bool,
        emit_skipped: bool,
    ) -> TickAction:
        """Collapse the two near-identical idle paths into one decision.

        Both the unchanged-digest and selector-returned-None paths increment the
        idle streak and then either back off on in-flight work or resolve the
        truly-idle case. They differ only in their telemetry: selector-None logs
        ``selector_idle`` and emits a structured play_skipped before waiting;
        unchanged-digest does neither. Those distinctions ride the
        ``log_selector_idle`` / ``emit_skipped`` flags and the ``reason`` string
        so each path keeps its exact log line and idle reason.
        """
        self._runtime.idle_streak += 1
        if log_selector_idle:
            _idle_log = _logger.debug if self._runtime.idle_streak > 1 else _logger.info
            _idle_log(
                "selector_idle",
                session_id=self._session_id,
                idle_streak=self._runtime.idle_streak,
            )
        if self._runtime.in_flight:
            return WaitInFlight(
                "waiting_for_in_flight_resource",
                emit_skipped_state=state if emit_skipped else None,
            )
        # truly idle → break unless idle-work remains
        return WaitIdle(state, reason)

    async def _apply_tick_action(self, action: TickAction) -> bool:
        """Perform a resolved tick's single terminal effect.

        Returns True when the loop should break, False to re-iterate — the same
        contract the old monolithic ``_run_loop_body`` returned inline.
        """
        match action:
            case Break():
                return True
            case Continue():
                return False
            case Pause(reason=reason):
                await self._lifecycle.pause_with_reason(reason)
                return False
            case WaitInFlight(wait_class=wait_class, emit_skipped_state=emit_state):
                if emit_state is not None:
                    await self.emit_structured_play_skipped_for_current_tick(emit_state)
                await self._completion.wait_for_in_flight(timeout=self.idle_backoff(wait_class))
                return False
            case WaitIdle(state=state, reason=reason):
                return not await self.continue_if_selector_idle_work_remains(state, reason=reason)
            case Dispatch(play_type=play_type, params=params, state=state):
                dispatched = await self._dispatcher.dispatch_play(play_type, params, state)
                if not dispatched:
                    return False
                # NO await on dispatch itself — wait only on resulting in-flight
                # work. Tests patch module-local AGENT_PING_TIMEOUT_SECONDS.
                if self._runtime.in_flight:
                    await self._completion.wait_for_in_flight(timeout=AGENT_PING_TIMEOUT_SECONDS)
                    return False
                return True  # truly idle
            case _:
                assert_never(action)
