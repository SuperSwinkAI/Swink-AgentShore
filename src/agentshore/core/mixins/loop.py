"""Main loop, loop-detection ladder, stagnation escalation, and idle backoff."""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import hashlib
import time
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from agentshore.core.base import _OrchestratorBase
from agentshore.core.helpers import _logger, _ppo_selector_cls
from agentshore.plays.base import PlayParams
from agentshore.rl.constants import STAGNATION_ENTROPY_MULTIPLIER
from agentshore.state import AgentStatus, PlaySkipReason, PlayType, SessionState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agentshore.config import RuntimeConfig
    from agentshore.core.main_repo_guard import MainRepoGuard
    from agentshore.core.override_queue import OverrideQueue
    from agentshore.core.velocity_tracker import VelocityTracker
    from agentshore.data.store import DataStore
    from agentshore.plays.candidates import PlayCandidatePlan
    from agentshore.plays.registry import PlayRegistry
    from agentshore.plays.selector import PlaySelector
    from agentshore.rl.metrics import MetricsEngine
    from agentshore.state import (
        AgentSnapshot,
        OrchestratorState,
        PlayOutcome,
        StateProvider,
    )

    NaturalExitCallback = Callable[[str], Awaitable[None]]


@dataclasses.dataclass(frozen=True)
class SkipDiagnosis:
    """Why nothing was dispatched this tick — the shared skip-classification.

    Built once by ``_compute_skip_diagnosis`` and consumed by every site that
    needs to emit ``play_skipped`` / decide an idle wait: the in-flight
    selector-None path, the truly-idle ``_continue_if_selector_idle_work_remains``
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

# Fibonacci-style idle backoff. The main loop's wait timeout starts at 1s on
# any tick that produced new state, then stretches across consecutive ticks
# where the selection-relevant state digest stayed the same. Capped at the
# last value so override pushes / human pauses are still picked up within
# ~21s even if no in-flight play completes to wake the loop earlier.
_IDLE_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0)
_WAITING_BACKOFF_SECONDS: tuple[float, ...] = (5.0, 10.0, 20.0, 30.0, 60.0)

# desktop-kqo5: consecutive idle-with-work ticks spent under a latched trunk
# dispatch pause (nothing in flight) before the loop auto-stops via drain.
# RECONCILE_STATE is exempt from the pause and should clear it within a few
# ticks; this is the last-resort escape so the loop never idles indefinitely on
# a trunk it cannot heal. At the 21s backoff ceiling this is ~3-4 minutes grace.
_WEDGED_IDLE_STOP_TICKS = 12

# How many times an unanswered loop-detection auto-stop is deferred while
# actionable work (merge-ready PRs / workable issues) still remains. Each
# reprieve lifts the pause and resumes the loop (it never leaves the loop
# wedged); once exhausted, the auto-stop drains as normal so a genuinely stuck
# session still terminates rather than re-wedging (#9).
_AUTO_STOP_WORK_REPRIEVE_LIMIT = 2

# Loop-liveness watchdog (#9): how often the independent watchdog task wakes to
# compare the loop heartbeat against the configured timeout. Kept well below the
# default 600s timeout so the watchdog reacts promptly once the deadline passes
# without busy-polling. The watchdog never runs on the loop's critical path, so
# a blocked loop still gets reaped within ~one interval of the deadline.
_LOOP_LIVENESS_CHECK_INTERVAL_SECONDS = 15.0

# Per-tick guard circuit-breaker: consecutive run_until_idle ticks whose body
# raises before the loop stops spinning on the failure and drains gracefully.
# A single bad tick (recovered next iteration) is normal and never escalates —
# the streak resets on any clean tick. Set well above transient noise but low
# enough that a permanently-throwing tick drains in seconds, not silently hangs.
_MAX_CONSECUTIVE_TICK_FAILURES = 10


class _LoopMixin(_OrchestratorBase):
    """The main orchestration loop plus loop-detection and stagnation laddering."""

    _cfg: RuntimeConfig
    _session_id: str
    _store: DataStore
    _selector: PlaySelector | None
    _state_provider: StateProvider
    _stop_requested: bool
    _draining: bool
    _drain_reason: str | None
    _drain_initialized: bool
    _in_flight: dict[str, asyncio.Task[PlayOutcome]]
    _overrides: OverrideQueue
    _registry: object | None
    _metrics: MetricsEngine | None
    _pause_event: asyncio.Event
    _velocity: VelocityTracker
    _last_play_id: int | None
    _last_warned_failure_streak: int | None
    _last_warned_any_streak: int | None
    _last_stagnation_stage: int
    _last_selection_digest: bytes | None
    _idle_streak: int
    _main_repo: MainRepoGuard
    _wedged_idle_ticks: int
    _auto_stop_reprieves_used: int
    _last_refresh_time: float
    _last_loop_iteration_at: float
    _loop_liveness_task: asyncio.Task[None] | None
    _loop_started_at: float
    _natural_exit_reason: str | None
    _natural_exit_callback: NaturalExitCallback | None
    _forced_mask_play_types: tuple[PlayType, ...]
    _fleet_idle_persistent_active: bool

    # ------------------------------------------------------------------

    async def _check_stagnation_escalation(self, state: OrchestratorState) -> bool:
        """Stagnation ladder (warn+entropy at 5, surface at 10, pause at 15)."""
        if (
            getattr(self, "_draining", False)
            or getattr(self, "_stop_requested", False)
            or state.session_state in {SessionState.DRAINING, SessionState.SHUTTING_DOWN}
        ):
            return False

        warn_after = self._cfg.rl.stagnation.warn_after
        alert_after = self._cfg.rl.stagnation.alert_after
        pause_after = self._cfg.rl.stagnation.pause_after

        stagnation = 0
        if self._metrics is not None:
            ctx = await self._metrics.snapshot(state)
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
            if isinstance(self._selector, _ppo_selector_cls()):
                self._selector.set_entropy_coef(self._cfg.rl.entropy_coef)
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
                    if isinstance(self._selector, _ppo_selector_cls()):
                        boosted = self._cfg.rl.entropy_coef * STAGNATION_ENTROPY_MULTIPLIER
                        self._selector.set_entropy_coef(boosted)
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

    def _idle_backoff(self, wait_class: str = "default") -> float:
        """Current backoff seconds, indexed by ``_idle_streak``."""
        backoff = (
            _WAITING_BACKOFF_SECONDS
            if wait_class in {"waiting_for_capacity", "waiting_for_in_flight_resource"}
            else _IDLE_BACKOFF_SECONDS
        )
        idx = min(self._idle_streak, len(backoff) - 1)
        return backoff[idx]

    def _classify_selector_idle(
        self,
        state: OrchestratorState,
        reason_counts: list[dict[str, object]],
    ) -> str:
        """Classify selector-idle waits for logging severity and backoff."""

        from agentshore.rl.selector import _only_capacity_waiting

        if not self._in_flight and not state.in_flight_plays:
            if _only_capacity_waiting(reason_counts):
                return "waiting_for_capacity"
            return "resolver_exhausted"
        if _only_capacity_waiting(reason_counts):
            return "waiting_for_capacity"
        return "waiting_for_in_flight_resource"

    @staticmethod
    def _classify_play_skipped_reason(
        state: OrchestratorState,
        reason_counts: list[dict[str, object]],
        *,
        candidate_plan_has_work: bool,
    ) -> PlaySkipReason:
        """Pick a structured ``PlaySkipReason`` for the current tick.

        Called when ``_select_play`` returned ``None`` (post-rni0 the loop's
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

        # If the top mask reason looks like a cooldown / recency cap, that
        # diagnosis is more specific than the generic ``all_masked`` bucket.
        if reason_counts:
            top_reason = reason_counts[0].get("reason")
            if isinstance(top_reason, str):
                low = top_reason.lower()
                if "cooldown" in low or "recency" in low:
                    return "cooldown_active"

        if reason_counts:
            # PPO had something to look at but the mask blocked every
            # candidate.  Payload (caller-attached) carries the top
            # mask_reasons so operators can trace the root cause.
            return "all_masked"

        if candidate_plan_has_work:
            # Workable graph but nothing was pickable — typically resolver
            # couldn't find a concrete target (no idle reviewer, no
            # unblocked issue this tick, etc.).
            return "no_eligible_targets"

        return "selector_returned_none"

    async def _emit_play_skipped(
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
            "idle_streak": self._idle_streak,
            "has_remaining_work": candidate_plan_has_work,
        }
        if reason in {"all_masked", "cooldown_active"} and mask_reasons:
            payload["mask_reasons"] = mask_reasons
        _skip_log = _logger.debug if self._idle_streak > 1 else _logger.info
        _skip_log("play_skipped", **payload)

    def _compute_skip_diagnosis(self, state: OrchestratorState) -> SkipDiagnosis:
        """Build the candidate plan + top mask reasons + ``PlaySkipReason`` once.

        Single source of truth for the "why was nothing dispatched" computation
        that was previously inlined verbatim at every selector-idle site (the
        in-flight selector-None path and the truly-idle
        ``_continue_if_selector_idle_work_remains`` path). Pure: builds state-
        derived structures only, mutating nothing.
        """
        from agentshore.plays.candidates import build_candidate_plan
        from agentshore.rl.mask import compute_mask_reasons

        candidate_plan = build_candidate_plan(state)
        reason_counts: list[dict[str, object]] = []
        if self._registry is not None:
            counts = collections.Counter(
                compute_mask_reasons(
                    state,
                    cast("PlayRegistry", self._registry),
                    cfg=self._cfg,
                    config_index=self._selector_config_index(),
                    apply_reverse_failsafe=self._cfg.rl.reverse_failsafe_enabled,
                    candidate_plan=candidate_plan,
                ).values()
            )
            reason_counts = [
                {"reason": mask_reason, "count": count}
                for mask_reason, count in counts.most_common(5)
            ]
        skip_reason = self._classify_play_skipped_reason(
            state,
            reason_counts,
            candidate_plan_has_work=candidate_plan.has_remaining_work,
        )
        return SkipDiagnosis(
            candidate_plan=candidate_plan,
            reason_counts=reason_counts,
            skip_reason=skip_reason,
        )

    async def _emit_structured_play_skipped_for_current_tick(
        self,
        state: OrchestratorState,
    ) -> None:
        """Compute mask reasons + classify + emit ``play_skipped`` once.

        Convenience wrapper for the in-flight selector-None branch so it
        doesn't have to duplicate ``_continue_if_selector_idle_work_remains``
        plumbing. Skips the ``fleet_idle_persistent`` check because the loop
        is not actually idle — work is still in flight.
        """
        diagnosis = self._compute_skip_diagnosis(state)
        await self._emit_play_skipped(
            state,
            reason=diagnosis.skip_reason,
            mask_reasons=diagnosis.reason_counts,
            candidate_plan_has_work=diagnosis.candidate_plan.has_remaining_work,
        )

    async def _check_fleet_idle_persistent(
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
        threshold = self._cfg.rl.loop_detection.fleet_idle_threshold
        in_flight_empty = not self._in_flight and not state.in_flight_plays
        should_be_active = self._idle_streak >= threshold and in_flight_empty

        if should_be_active and not self._fleet_idle_persistent_active:
            payload: dict[str, object] = {
                "session_id": self._session_id,
                "idle_streak": self._idle_streak,
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
                idle_streak=self._idle_streak,
                threshold=threshold,
                transition="exited",
            )
            self._fleet_idle_persistent_active = False

    def _selection_state_digest(
        self,
        state: OrchestratorState,
        idle_agents: list[AgentSnapshot],
    ) -> bytes:
        """Compact hash of the inputs the selector / override resolver would see.

        Skipping ``_select_play`` when this is unchanged eliminates the
        ``selector_idle`` storm during long-running plays, where the loop
        would otherwise re-run the selector once per second against an
        identical state. Inputs:

        - Set of idle agent ids (selector only dispatches to idle agents).
        - ``len(self._in_flight)`` (a play completion changes this and is
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
        h.update(len(self._in_flight).to_bytes(4, "little"))
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

    async def _continue_if_selector_idle_work_remains(
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
        # plays except END_AGENT/RECONCILE_STATE. RECONCILE_STATE should heal the
        # trunk and clear the latch within a few ticks; if it cannot (nothing in
        # flight, pause persists across the grace window), escalate to a clean
        # drain-based stop rather than idling forever. Gated strictly on the
        # latched pause + no in-flight, so healthy capacity-idle never trips it.
        if self._main_repo.dispatch_paused and not self._in_flight:
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
                await self._initiate_autonomous_stop("main_repo_wedged", arm_gate_only=True)
                return False
        else:
            self._wedged_idle_ticks = 0

        diagnosis = self._compute_skip_diagnosis(state)
        candidate_plan = diagnosis.candidate_plan
        availability = candidate_plan.work_availability
        candidate_plan_has_work = candidate_plan.has_remaining_work
        reason_counts = diagnosis.reason_counts
        skip_reason = diagnosis.skip_reason
        await self._emit_play_skipped(
            state,
            reason=skip_reason,
            mask_reasons=reason_counts,
            candidate_plan_has_work=candidate_plan_has_work,
        )
        await self._check_fleet_idle_persistent(
            state,
            reason=skip_reason,
            mask_reasons=reason_counts,
        )
        if not candidate_plan_has_work:
            # Issue #562: ``has_remaining_work`` is a *graph* signal (workable
            # issues, ready tasks, actionable PRs, …) and is NOT correlated
            # with the action mask. Post idle_tick removal (PR #535) we hit
            # cases where the mask has eligible play slots but the graph
            # signal says "no work" — exiting the loop here strands the
            # session even though PPO has plays it can pick. If any mask
            # slot is True, treat this tick as a wait (sleep + keep going);
            # the next state transition (issue refresh, agent state change,
            # ceiling-tick re-eval) will re-run the selector.
            if any(state.action_mask):
                _logger.debug(
                    "selector_idle_mask_has_plays",
                    reason=reason,
                    session_id=self._session_id,
                    idle_streak=self._idle_streak,
                    mask_true_count=sum(1 for slot in state.action_mask if slot),
                    top_mask_reasons=reason_counts,
                )
                await asyncio.sleep(self._idle_backoff("waiting_for_capacity"))
                return True
            return False
        wait_class = self._classify_selector_idle(state, reason_counts)

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
            idle_streak=self._idle_streak,
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
        await asyncio.sleep(self._idle_backoff(wait_class))
        return True

    async def _wait_for_in_flight(self, *, timeout: float) -> None:
        """``asyncio.wait`` with first-completed semantics on the in-flight set."""
        await asyncio.wait(
            self._in_flight.values(),
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )

    async def _actionable_work_remains(self) -> tuple[bool, int, int]:
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
            state = await self._build_state()
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

    async def _initiate_autonomous_stop(
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
        self._drain_reason = reason
        if fire_natural_exit:
            self._natural_exit_reason = reason
        if clear_pause_deadline:
            self._pause_deadline = None
        if arm_gate_only:
            self._draining = True
            # Unblock the gate so the loop proceeds to begin_drain next iteration.
            self._pause_event.set()
            return
        await self.begin_drain(reason)

    async def _auto_stop_unanswered_pause(self) -> None:
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
            pause_reason=self._pause_reason,
            timeout_seconds=self._cfg.feedback.unanswered_timeout_seconds,
            note="no human response within feedback.unanswered_timeout_seconds; auto-stopping",
        )
        await self._initiate_autonomous_stop(
            "loop_detection_prompt_timeout",
            arm_gate_only=True,
            clear_pause_deadline=True,
        )

    def _loop_liveness_timeout_seconds(self) -> float | None:
        """Resolve the configured loop-liveness watchdog timeout (None disables)."""
        return self._cfg.feedback.loop_liveness_timeout_seconds

    def start_loop_liveness_watchdog(self) -> None:
        """Launch the independent loop-liveness watchdog task (#9).

        Idempotent. No-op when the timeout is unset (watchdog disabled) or a
        live task already exists. The task runs OFF the core loop so a
        hard-frozen loop — one that stopped iterating entirely, e.g. a deadlock
        in the play-mutation promotion path — still gets reaped. This is the
        backstop the idle/unanswered-pause auto-stops cannot provide: those
        require the loop to keep ticking, which a true freeze does not.
        """
        if self._loop_liveness_timeout_seconds() is None:
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
        while not self._stop_requested and not self._stopped:
            await asyncio.sleep(interval)
            timeout = self._loop_liveness_timeout_seconds()
            if timeout is None:
                continue
            if self._stop_requested or self._stopped:
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
                in_flight=len(self._in_flight),
                note=(
                    "core loop heartbeat did not advance within "
                    "feedback.loop_liveness_timeout_seconds; loop presumed "
                    "hard-frozen — force-draining and stopping the session"
                ),
            )
            # Drive teardown off the (dead) loop. begin_drain is idempotent and
            # records the drain reason / fires the ESR; stop() then performs the
            # full graceful shutdown (end agents, checkpoint, beads clear, store
            # close) and is re-entrancy safe. Guarded because the watchdog runs
            # off the (dead) loop — a begin_drain failure must not kill it.
            await self._safe_call(
                self._initiate_autonomous_stop("loop_liveness_timeout"),
                "loop_liveness_begin_drain",
            )
            await self.stop()
            return

    async def run_until_idle(self) -> None:
        """Drive the RL loop until selector returns None or a stop is requested.

        Each iteration runs ``_run_loop_body`` (one tick: pause-gate, harvest,
        build state, terminate-check, detectors, idle-gate, select, dispatch)
        behind a per-tick guard so a single throwing tick can never kill the
        loop (the ``sidecar_orchestrator_run_failed`` silent-hang class). A
        permanently-throwing tick trips the circuit breaker and drains cleanly.
        """
        self._loop_started_at = time.monotonic()
        # Arm the loop-liveness heartbeat before the first iteration so the
        # watchdog (#9) has a fresh baseline and never sees a stale 0.0.
        self._last_loop_iteration_at = time.monotonic()

        while not self._stop_requested:
            # Loop-liveness heartbeat (#9): stamp every iteration so the
            # independent watchdog can detect a hard-frozen loop. Stays OUTSIDE
            # the per-tick guard so it advances even on a throwing tick — a
            # fast-failing-but-looping tick must not look hard-frozen.
            self._last_loop_iteration_at = time.monotonic()
            tick_raised = False
            try:
                should_break = await self._run_loop_body()
            except Exception as exc:
                # Per-tick guard: contain the failure, never let one tick kill
                # the loop. The breaker drains gracefully once failures persist.
                tick_raised = True
                if await self._handle_tick_failure(exc):
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
        if self._natural_exit_reason is not None and self._natural_exit_callback is not None:
            await self._safe_call(
                self._natural_exit_callback(self._natural_exit_reason),
                "on_natural_exit_callback",
            )

    async def _handle_tick_failure(self, exc: Exception) -> bool:
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
            in_flight=len(self._in_flight),
            idle_streak=self._idle_streak,
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
            await self._safe_call(
                self._initiate_autonomous_stop(
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
        guard there. Control flow that previously ``break``-ed the while loop
        returns True; everything that previously ``continue``-d (or fell off the
        end) returns False so the loop re-iterates.
        """
        # Pause blocks new selection/dispatch, but completed in-flight plays
        # still need to be harvested so agent completions, costs, and rewards
        # do not get stranded behind the pause gate.
        if not self._pause_event.is_set():
            # Bound the wait by the unanswered-pause deadline (#9) so a
            # feedback pause nobody answers auto-stops instead of wedging the
            # loop indefinitely. ``remaining`` is None when no deadline is
            # armed (manual pause), making this a plain block-until-resume.
            remaining: float | None = None
            if self._pause_deadline is not None:
                remaining = max(0.0, self._pause_deadline - time.monotonic())
            pause_wait = asyncio.create_task(self._pause_event.wait())
            try:
                await asyncio.wait(
                    [pause_wait, *self._in_flight.values()],
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                if not pause_wait.done():
                    pause_wait.cancel()
                    with suppress(asyncio.CancelledError):
                        await pause_wait
            if self._stop_requested:
                return True
            if self._in_flight:
                # Harvest completions so they aren't stranded behind the gate.
                await self._harvest_completed()
            if (
                not self._pause_event.is_set()
                and self._pause_deadline is not None
                and time.monotonic() >= self._pause_deadline
            ):
                # Unanswered feedback pause past its deadline → clean drain.
                await self._auto_stop_unanswered_pause()
            elif not self._pause_event.is_set():
                return False

        if self._stop_requested:
            return True

        # Drain requested from sync context (e.g. signal handler) — initialize fully.
        if self._draining and not self._drain_initialized:
            await self.begin_drain(self._drain_reason or "signal_sigterm")

        await self._harvest_completed()

        # Periodic GitHub refresh fallback: fires when no refresh-triggering
        # play has completed recently, keeping cache fresh across long runs of
        # run_qa / systematic_debugging / unblock_pr. Invalidates the digest
        # so the next tick re-runs the selector; does NOT reset the idle
        # streak — a fleet sitting idle through a refresh has not become
        # less idle (desktop-mib1).
        if time.monotonic() - self._last_refresh_time > ISSUE_REFRESH_INTERVAL_SECONDS:
            await self._safe_call(self._refresh_issues(), "refresh_issues_periodic")
            self._last_selection_digest = None

        state = await self._build_state()

        state = await self._begin_budget_reserve_drain_if_needed(state)

        should_stop, reason = self._should_terminate(state)
        if should_stop:
            _logger.info(
                "loop_terminating",
                reason=reason,
                session_id=self._session_id,
            )
            if reason is not None and reason != "stop_requested":
                self._natural_exit_reason = reason
            return True
        if reason is not None:
            # reason set but should_stop False → pause; loop blocks at wait() next iteration
            await self._pause_with_reason(reason)
            return False

        # PPO sees the full mask every tick — the eligibility mask
        # (``compute_agent_eligibility_mask``) already zeros out worker
        # plays when no IDLE agent matches, and ``compute_config_mask``
        # still bounds INSTANTIATE_AGENT by per-(type, tier)
        # ``max_per_config`` / ``cooldown_plays``. Letting selection run
        # even when the fleet
        # is fully busy lets PPO grow the fleet under sustained pressure;
        # if nothing is pickable, the ``selection is None`` path below
        # falls through to ``_wait_for_in_flight`` as before.
        idle_agents = [a for a in state.agents if a.status == AgentStatus.IDLE]

        # Skip ``_select_play`` (and its log line) when nothing the selector
        # cares about has changed since the last attempt. The watchdog at
        # the ceiling tick still re-evaluates regardless, so a missed
        # signal recovers within ~21s. See ``_selection_state_digest``.
        digest = self._selection_state_digest(state, idle_agents)
        ceiling_tick = self._idle_streak >= len(_IDLE_BACKOFF_SECONDS) - 1
        if digest == self._last_selection_digest and not ceiling_tick:
            self._idle_streak += 1
            if self._in_flight:
                await self._wait_for_in_flight(
                    timeout=self._idle_backoff("waiting_for_in_flight_resource")
                )
                return False
            # truly idle, nothing changed → break unless idle-work remains
            return not await self._continue_if_selector_idle_work_remains(
                state, reason="unchanged_digest"
            )

        self._last_selection_digest = digest

        override_play = await self._consume_override(state)
        from_override = override_play is not None

        selection = await self._select_play(state, override_play=override_play)
        # Fold this cycle's EligibilityAuthority confirm-repicks into the rolling
        # divergence window (observation slot executor_skip_rate_recent_50). Drain
        # once per selection cycle whether or not a play was produced — an
        # all-repick cycle that yields None is exactly the divergence signal.
        self._velocity.record_selection_repicks(self._selector)
        if selection is None:
            # Only log once per distinct digest. With the digest gate
            # above, this fires at most once per state transition rather
            # than once per loop tick.
            self._idle_streak += 1
            _idle_log = _logger.debug if self._idle_streak > 1 else _logger.info
            _idle_log("selector_idle", session_id=self._session_id, idle_streak=self._idle_streak)
            if self._in_flight:
                await self._emit_structured_play_skipped_for_current_tick(state)
                await self._wait_for_in_flight(
                    timeout=self._idle_backoff("waiting_for_in_flight_resource")
                )
                return False
            # truly idle → break unless idle-work remains
            return not await self._continue_if_selector_idle_work_remains(
                state, reason="selector_none"
            )

        # Selector picked a play — reset the streak so the next idle window
        # starts at the 1s backoff floor. If we were inside a fleet-idle
        # persistent window (desktop-85ex), emit the exit transition once
        # before clearing the flag — this is the second of the two
        # bookend events the memory project_loop_detector_warning_storm
        # mandates we preserve, instead of re-emitting per tick.
        if self._fleet_idle_persistent_active:
            _logger.info(
                "fleet_idle_persistent",
                session_id=self._session_id,
                idle_streak=self._idle_streak,
                threshold=self._cfg.rl.loop_detection.fleet_idle_threshold,
                transition="exited",
            )
            self._fleet_idle_persistent_active = False
        self._idle_streak = 0
        self._wedged_idle_ticks = 0

        play_type, params = selection

        if (
            not from_override
            and play_type == PlayType.INSTANTIATE_AGENT
            and not idle_agents
            and self._in_flight
        ):
            _logger.info(
                "ppo_instantiate_under_pressure",
                session_id=self._session_id,
                in_flight=len(self._in_flight),
                live_agents=len(state.agents),
                open_issues=len(state.open_issues),
            )
        if self._shutdown_allows_only_end_agent(state) and play_type != PlayType.END_AGENT:
            _logger.warning(
                "selection_blocked_during_shutdown",
                play_type=play_type.value,
                session_id=self._session_id,
            )
            if idle_agents:
                play_type, params = PlayType.END_AGENT, PlayParams()
            else:
                if self._in_flight:
                    await self._wait_for_in_flight(
                        timeout=self._idle_backoff("waiting_for_in_flight_resource")
                    )
                    return False
                return True
        end_session_blocked = (
            play_type == PlayType.END_SESSION
            and not await self._revalidate_end_session_before_dispatch()
        )
        if end_session_blocked:
            if isinstance(self._selector, _ppo_selector_cls()):
                self._selector.consume_pending()
            return False
        dispatched = await self._dispatch_play(play_type, params, state)
        if not dispatched:
            return False
        # NO await — continue loop

        # Efficient wait if tasks in flight. Tests patch the module-local
        # ``agentshore.core.mixins.loop.AGENT_PING_TIMEOUT_SECONDS`` constant.
        if self._in_flight:
            await self._wait_for_in_flight(timeout=AGENT_PING_TIMEOUT_SECONDS)
        elif not self._in_flight:
            return True  # truly idle

        return False
