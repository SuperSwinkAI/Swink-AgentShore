"""TakeBreakPlay — pause one agent for a configurable duration.

Used when the RL agent detects resource contention, rate limits, or quota
exhaustion. The play sleeps for the triggering agent only, then attempts to
recover that agent so the orchestrator can re-evaluate the situation with
fresh state without blocking unrelated agents.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

from agentshore.plays.internal.base import InternalPlay
from agentshore.rl.mask_reason import MaskClassification, MaskReason, MaskSource
from agentshore.state import RECOVERABLE_ERROR_CLASSES, AgentStatus, PlayOutcome, PlayType

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentshore.plays.base import PlayExecutionContext, PlayParams
    from agentshore.state import OrchestratorState

_PLAY_COST = 0.005

# When the session is draining, poll for the drain signal on this cadence so an
# in-flight break aborts within seconds instead of holding the agent for the
# full break_duration_minutes (default 30). See #30.
_DRAIN_POLL_SECONDS = 5.0


class TakeBreakPlay(InternalPlay):
    """Sleep for cfg.session.break_duration_minutes for one target agent."""

    play_type = PlayType.TAKE_BREAK

    def preconditions(self, state: OrchestratorState) -> list[MaskReason]:
        cooldown_targets = [
            a
            for a in state.agents
            if a.status == AgentStatus.ERROR
            and a.last_error_class in RECOVERABLE_ERROR_CLASSES
            and a.current_play_type != PlayType.TAKE_BREAK
        ]
        if cooldown_targets:
            return []

        active = [a for a in state.agents if a.status in (AgentStatus.IDLE, AgentStatus.BUSY)]
        if not active:
            return [
                MaskReason(
                    text="no active agents or cooldown targets — instantiate one first",
                    classification=MaskClassification.HARD,
                    source=MaskSource.PRECONDITION,
                )
            ]
        return []

    def estimated_cost(self, state: OrchestratorState) -> float:
        return _PLAY_COST

    async def execute(
        self,
        state: OrchestratorState,
        params: PlayParams,
        *,
        ctx: PlayExecutionContext,
    ) -> PlayOutcome:
        duration_s = ctx.cfg.session.break_duration_minutes * 60
        t0 = time.monotonic()
        is_draining = ctx.is_draining
        trigger_agent_id = params.extras.get("trigger_agent_id")
        target_agent_id = params.agent_id or (
            trigger_agent_id if isinstance(trigger_agent_id, str) else None
        )

        # Register a cancel signal for the pending recovery so an ``agent_cleared``
        # teardown can abandon this wait immediately (#367). Without it the break
        # ran to completion and logged ``break_recovery_failed`` ~31 min after the
        # target was gone, polluting recovery telemetry.
        cancel = (
            ctx.manager.register_break_recovery(target_agent_id)
            if target_agent_id is not None
            else None
        )
        try:
            cancelled = await self._sleep_break(duration_s, is_draining, cancel)
        finally:
            if target_agent_id is not None and cancel is not None:
                ctx.manager.unregister_break_recovery(target_agent_id, cancel)
        elapsed = time.monotonic() - t0
        if cancelled == "drain":
            return self._drain_abort_outcome(params, trigger_agent_id, elapsed)
        if cancelled == "cleared":
            return self._agent_cleared_outcome(target_agent_id, trigger_agent_id, elapsed)

        # Final guard: drain may have flipped during the last sleep chunk (or the
        # single unbroken sleep). Abort before attempting recovery so we never
        # record a spurious break-recovery failure on the wind-down path.
        if is_draining is not None and is_draining():
            return self._drain_abort_outcome(params, trigger_agent_id, elapsed)

        if target_agent_id is None:
            target = next(
                (
                    a
                    for a in state.agents
                    if a.status == AgentStatus.ERROR
                    and a.last_error_class in RECOVERABLE_ERROR_CLASSES
                    and a.current_play_type != PlayType.TAKE_BREAK
                ),
                None,
            )
            target_agent_id = target.agent_id if target is not None else None

        recovered: list[str] = []
        # Recover only the agent that triggered this cooldown. Other agents may
        # continue working or be handled by their own TAKE_BREAK play.
        #
        # The target may have been ``manager.clear()``-ed out of the registry —
        # e.g. an idle-reap or a concurrent end_agent play retired it — either
        # before this play was even dispatched (state.agents, captured at
        # dispatch time, already misses it) or during the break sleep itself
        # (up to 30 min by default; state.agents can't see that). Guard on
        # ``state.agents`` here to skip the common case cheaply; ``attempt_recovery``
        # itself no longer raises for an unknown id either (agents/manager.py),
        # covering the mid-sleep-clear case as defense-in-depth (#332). Without
        # either, ``attempt_recovery`` -> ``_get_handle`` raises
        # ``PreconditionFailed`` for an unknown id, crashing the play instead of
        # returning a clean outcome.
        target_still_present = target_agent_id is not None and any(
            a.agent_id == target_agent_id for a in state.agents
        )
        recovery_attempted = target_still_present
        recovery_succeeded = (
            recovery_attempted and await ctx.manager.attempt_recovery(target_agent_id)  # type: ignore[arg-type]
        )
        if recovery_succeeded:
            recovered.append(target_agent_id)  # type: ignore[arg-type]

        trigger_error_class = params.extras.get("trigger_error_class")

        # A break that ran but couldn't recover its target leaves the agent in
        # ERROR. Returning success=False lets the loop count consecutive
        # failures on this play and graduate to end_agent (desktop-s1u7).
        success = not recovery_attempted or recovery_succeeded

        return PlayOutcome(
            play_type=self.play_type,
            agent_id=target_agent_id,
            success=success,
            partial=False,
            duration_seconds=elapsed,
            token_cost=0,
            dollar_cost=_PLAY_COST,
            artifacts=[
                {
                    "type": "session_event",
                    "event": "break_completed" if success else "break_recovery_failed",
                    "duration_s": elapsed,
                    "recovered_agents": recovered,
                    "trigger_agent_id": trigger_agent_id,
                    "trigger_error_class": trigger_error_class,
                }
            ],
            alignment_delta=0.0,
            error=None if success else "attempt_recovery_failed",
        )

    async def _sleep_break(
        self,
        duration_s: float,
        is_draining: Callable[[], bool] | None,
        cancel: asyncio.Event | None,
    ) -> str:
        """Sleep out the break, returning why it ended.

        ``"drain"`` — the session began winding down mid-break (#30);
        ``"cleared"`` — the target agent was cleared, firing its break-recovery
        cancel signal (#367); ``""`` — the full duration elapsed.

        With no drain signal and no cancel signal this degenerates to a single
        ``asyncio.sleep(duration_s)``; otherwise it wakes on ``_DRAIN_POLL_SECONDS``
        boundaries (drain is a poll, not an event) and on the cancel event itself.
        """
        remaining = float(duration_s)
        poll = _DRAIN_POLL_SECONDS if is_draining is not None else remaining
        while remaining > 0:
            # Drain-aware break: poll the drain signal so a wind-down that begins
            # mid-break aborts within ``_DRAIN_POLL_SECONDS`` instead of holding
            # the agent for the full duration. On abort, skip recovery entirely —
            # the agent stays ERROR and end_agent retires it during drain (#30).
            if is_draining is not None and is_draining():
                return "drain"
            if cancel is not None and cancel.is_set():
                return "cleared"
            step = min(poll, remaining)
            if cancel is None:
                await asyncio.sleep(step)
            elif await self._sleep_until_cancelled(step, cancel):
                return "cleared"
            remaining -= step
        return "cleared" if cancel is not None and cancel.is_set() else ""

    @staticmethod
    async def _sleep_until_cancelled(step: float, cancel: asyncio.Event) -> bool:
        """Sleep *step* seconds; return True if *cancel* fired first."""
        sleeper = asyncio.ensure_future(asyncio.sleep(step))
        waiter = asyncio.ensure_future(cancel.wait())
        try:
            await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeper, waiter):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
        return cancel.is_set()

    def _agent_cleared_outcome(
        self,
        target_agent_id: str | None,
        trigger_agent_id: object,
        elapsed: float,
    ) -> PlayOutcome:
        """Outcome for a break abandoned because its target agent was cleared (#367).

        No recovery is attempted — the agent no longer exists. ``success=True``
        keeps this off the consecutive-break-failure path: the break did not fail,
        it became moot, and scoring it as a failure is what produced stale
        ``break_recovery_failed`` telemetry ~31 min after ``agent_cleared``.
        """
        return PlayOutcome(
            play_type=self.play_type,
            agent_id=target_agent_id,
            success=True,
            partial=True,
            duration_seconds=elapsed,
            token_cost=0,
            dollar_cost=_PLAY_COST,
            artifacts=[
                {
                    "type": "session_event",
                    "event": "break_skipped_agent_cleared",
                    "duration_s": elapsed,
                    "trigger_agent_id": trigger_agent_id,
                }
            ],
            alignment_delta=0.0,
            error=None,
        )

    def _drain_abort_outcome(
        self,
        params: PlayParams,
        trigger_agent_id: object,
        elapsed: float,
    ) -> PlayOutcome:
        """Outcome for a break aborted because the session began draining.

        No recovery is attempted — the agent is left in ERROR for end_agent to
        retire during drain (#30). ``success=True`` keeps this from counting as a
        break-recovery failure (it is an intentional skip, not a failed retry).
        """
        target_agent_id = params.agent_id or (
            trigger_agent_id if isinstance(trigger_agent_id, str) else None
        )
        return PlayOutcome(
            play_type=self.play_type,
            agent_id=target_agent_id,
            success=True,
            partial=True,
            duration_seconds=elapsed,
            token_cost=0,
            dollar_cost=_PLAY_COST,
            artifacts=[
                {
                    "type": "session_event",
                    "event": "break_skipped_draining",
                    "duration_s": elapsed,
                    "trigger_agent_id": trigger_agent_id,
                }
            ],
            alignment_delta=0.0,
            error=None,
        )
