"""TakeBreakPlay — pause one agent for a configurable duration.

Used when the RL agent detects resource contention, rate limits, or quota
exhaustion. The play sleeps for the triggering agent only, then attempts to
recover that agent so the orchestrator can re-evaluate the situation with
fresh state without blocking unrelated agents.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from agentshore.rl.mask_reason import MaskClassification, MaskReason, MaskSource
from agentshore.state import AgentStatus, PlayOutcome, PlayType

if TYPE_CHECKING:
    from agentshore.plays.base import PlayExecutionContext, PlayParams
    from agentshore.state import OrchestratorState

_PLAY_COST = 0.005


class TakeBreakPlay:
    """Sleep for cfg.session.break_duration_minutes for one target agent."""

    play_type = PlayType.TAKE_BREAK
    skill_name = None
    capability = None

    def preconditions(self, state: OrchestratorState) -> list[MaskReason]:
        cooldown_targets = [
            a
            for a in state.agents
            if a.status == AgentStatus.ERROR
            and a.last_error_class in ("rate_limit", "unknown")
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
        await asyncio.sleep(duration_s)
        elapsed = time.monotonic() - t0

        trigger_agent_id = params.extras.get("trigger_agent_id")
        target_agent_id = params.agent_id or (
            trigger_agent_id if isinstance(trigger_agent_id, str) else None
        )
        if target_agent_id is None:
            target = next(
                (
                    a
                    for a in state.agents
                    if a.status == AgentStatus.ERROR
                    and a.last_error_class in ("rate_limit", "unknown")
                    and a.current_play_type != PlayType.TAKE_BREAK
                ),
                None,
            )
            target_agent_id = target.agent_id if target is not None else None

        recovered: list[str] = []
        # Recover only the agent that triggered this cooldown. Other agents may
        # continue working or be handled by their own TAKE_BREAK play.
        recovery_attempted = target_agent_id is not None
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
