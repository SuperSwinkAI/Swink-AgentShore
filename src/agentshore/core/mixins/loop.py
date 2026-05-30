"""Main loop, loop-detection ladder, stagnation escalation, and idle backoff."""

from __future__ import annotations

import asyncio
import collections
import hashlib
import time
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from agentshore.core.base import _OrchestratorBase
from agentshore.core.helpers import _is_loop_bucket, _logger, _ppo_selector_cls
from agentshore.plays.base import PlayParams
from agentshore.rl.constants import STAGNATION_ENTROPY_MULTIPLIER
from agentshore.state import AgentStatus, PlaySkipReason, PlayType, SessionState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agentshore.config import RuntimeConfig
    from agentshore.data.store import DataStore
    from agentshore.plays.override import OverrideEntry
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
    _override_queue: asyncio.Queue[OverrideEntry]
    _first_play_override: tuple[PlayType, PlayParams] | None
    _registry: object | None
    _metrics: MetricsEngine | None
    _pause_event: asyncio.Event
    _last_play_id: int | None
    _last_warned_failure_streak: int | None
    _last_warned_any_streak: int | None
    _last_stagnation_stage: int
    _last_selection_digest: bytes | None
    _idle_streak: int
    _main_repo_dispatch_paused: bool
    _wedged_idle_ticks: int
    _last_refresh_time: float
    _loop_started_at: float
    _natural_exit_reason: str | None
    _natural_exit_callback: NaturalExitCallback | None
    _forced_mask_play_types: tuple[PlayType, ...]
    _fleet_idle_persistent_active: bool

    # ------------------------------------------------------------------

    def _check_loop_detection(self, state: OrchestratorState) -> bool:
        """Loop-detection ladder (warn/pause). Returns True if pausing.

        Failure streaks: same play type failing back-to-back, signal is
        "this play can't make progress" → tight thresholds.

        Any-outcome streaks: same play type firing repeatedly, even when it
        succeeds. Signal is "policy collapsed onto a play it likes," often a
        cheap repeated action. Looser thresholds (2x) since some legitimate
        work is bursty (e.g., reviewing several PRs).
        """
        if (
            getattr(self, "_draining", False)
            or getattr(self, "_stop_requested", False)
            or state.session_state in {SessionState.DRAINING, SessionState.SHUTTING_DOWN}
        ):
            self._forced_mask_play_types = ()
            return False

        fail_streak = state.same_type_failure_streak
        any_streak = state.same_type_streak
        warn_streak = self._cfg.rl.loop_detection.warn_after
        pause_streak = self._cfg.rl.loop_detection.escalate_after

        if fail_streak >= warn_streak:
            if (
                self._last_warned_failure_streak is None
                or fail_streak > self._last_warned_failure_streak
            ) and _is_loop_bucket(fail_streak, warn_streak):
                _logger.warning(
                    "loop_detected",
                    streak=fail_streak,
                    kind="failure",
                    session_id=self._session_id,
                )
                self._last_warned_failure_streak = fail_streak
        elif fail_streak == 0:
            # Reset only on a genuine streak clearance (back to zero). Dips that
            # stay above zero are part of the same streak run and must not
            # re-trigger the same bucket warning on the next crossing.
            self._last_warned_failure_streak = None

        any_warn_threshold = 2 * warn_streak
        if any_streak >= any_warn_threshold:
            if (
                self._last_warned_any_streak is None or any_streak > self._last_warned_any_streak
            ) and _is_loop_bucket(any_streak, any_warn_threshold):
                _logger.warning(
                    "loop_detected",
                    streak=any_streak,
                    kind="any_outcome",
                    session_id=self._session_id,
                )
                self._last_warned_any_streak = any_streak
        elif any_streak == 0:
            self._last_warned_any_streak = None

        # Loop-detection no longer force-masks the repeated play type — that
        # overrode the policy's revealed preference. Collapse is handled from
        # within the policy via the stagnation entropy boost
        # (_check_stagnation_escalation), keeping the PPO in the driver's seat.
        return fail_streak >= pause_streak or any_streak >= 2 * pause_streak

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
        await self._emit_play_skipped(
            state,
            reason=skip_reason,
            mask_reasons=reason_counts,
            candidate_plan_has_work=candidate_plan.has_remaining_work,
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
        override_pending = self._first_play_override is not None or not self._override_queue.empty()
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
        from agentshore.plays.candidates import build_candidate_plan
        from agentshore.rl.mask import compute_mask_reasons

        # desktop-kqo5 wedge auto-stop: a latched trunk-dispatch pause blocks all
        # plays except END_AGENT/RECONCILE_STATE. RECONCILE_STATE should heal the
        # trunk and clear the latch within a few ticks; if it cannot (nothing in
        # flight, pause persists across the grace window), escalate to a clean
        # drain-based stop rather than idling forever. Gated strictly on the
        # latched pause + no in-flight, so healthy capacity-idle never trips it.
        if self._main_repo_dispatch_paused and not self._in_flight:
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
                self._draining = True
                self._drain_reason = "main_repo_wedged"
                self._pause_event.set()
                return False
        else:
            self._wedged_idle_ticks = 0

        candidate_plan = build_candidate_plan(state)
        availability = candidate_plan.work_availability
        candidate_plan_has_work = candidate_plan.has_remaining_work
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
            candidate_plan_has_work=candidate_plan_has_work,
        )
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

    async def _auto_stop_unanswered_pause(self) -> None:
        """Auto-stop a feedback pause that went unanswered past its deadline (#9).

        Lifts the pause so the loop can reach the drain path, and requests a
        clean drain-based shutdown — the same teardown ``agentshore stop``
        performs (end_agent per agent, checkpoint, beads clear). Without this an
        unanswered loop-detection popup wedged the loop for hours, and the drain
        RPC could not be serviced while wedged. Emits ``loop_detection_prompt_timeout``
        so the auto-stop is visible in the NDJSON log (the wedge was silent).
        """
        _logger.warning(
            "loop_detection_prompt_timeout",
            session_id=self._session_id,
            pause_reason=self._pause_reason,
            timeout_seconds=self._cfg.feedback.unanswered_timeout_seconds,
            note="no human response within feedback.unanswered_timeout_seconds; auto-stopping",
        )
        self._pause_deadline = None
        self._draining = True
        self._drain_reason = "loop_detection_prompt_timeout"
        # Unblock the gate so the loop proceeds to begin_drain on the next steps.
        self._pause_event.set()

    async def run_until_idle(self) -> None:
        """Drive the RL loop until selector returns None or a stop is requested.

        Each iteration: pause-gate, harvest completions, build state, check
        termination, run loop-detection ladder, gate on idle agents, resolve
        an override or selector pick, dispatch, and wait for any in-flight
        task to make progress.
        """
        self._loop_started_at = time.monotonic()

        while not self._stop_requested:
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
                    break
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
                    continue

            if self._stop_requested:
                break

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
                break
            if reason is not None:
                # reason set but should_stop False → pause; loop blocks at wait() next iteration
                await self._pause_with_reason(reason)
                continue

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
                    continue
                if await self._continue_if_selector_idle_work_remains(
                    state, reason="unchanged_digest"
                ):
                    continue
                break  # truly idle, nothing changed

            self._last_selection_digest = digest

            override_play = await self._consume_override(state)
            from_override = override_play is not None

            selection = await self._select_play(state, override_play=override_play)
            if selection is None:
                # Only log once per distinct digest. With the digest gate
                # above, this fires at most once per state transition rather
                # than once per loop tick.
                self._idle_streak += 1
                _idle_log = _logger.debug if self._idle_streak > 1 else _logger.info
                _idle_log(
                    "selector_idle", session_id=self._session_id, idle_streak=self._idle_streak
                )
                if self._in_flight:
                    await self._emit_structured_play_skipped_for_current_tick(state)
                    await self._wait_for_in_flight(
                        timeout=self._idle_backoff("waiting_for_in_flight_resource")
                    )
                    continue
                if await self._continue_if_selector_idle_work_remains(
                    state, reason="selector_none"
                ):
                    continue
                break  # truly idle

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
                        continue
                    break
            end_session_blocked = (
                play_type == PlayType.END_SESSION
                and not await self._revalidate_end_session_before_dispatch()
            )
            if end_session_blocked:
                if isinstance(self._selector, _ppo_selector_cls()):
                    self._selector.consume_pending()
                continue
            should_revalidate = isinstance(self._selector, _ppo_selector_cls()) or (
                override_play is not None and self._params_have_dispatch_target(params)
            )
            dispatched = await self._dispatch_play(
                play_type,
                params,
                state,
                revalidate=should_revalidate,
            )
            if not dispatched:
                continue
            # NO await — continue loop

            # Efficient wait if tasks in flight. Look up the timeout via
            # agentshore.core so tests that patch the constant take effect.
            if self._in_flight:
                from agentshore import core as _core_pkg

                await self._wait_for_in_flight(timeout=_core_pkg.AGENT_PING_TIMEOUT_SECONDS)
            elif not self._in_flight:
                break  # truly idle

        # Natural-exit hook fires only when termination came from
        # _should_terminate (drain_complete, max_plays, timeout, shutting_down),
        # not from an external request_stop()/stop() call. The sidecar boot
        # wrapper uses this to fire session.completed (DESIGN §5.2).
        if self._natural_exit_reason is not None and self._natural_exit_callback is not None:
            await self._safe_call(
                self._natural_exit_callback(self._natural_exit_reason),
                "on_natural_exit_callback",
            )
