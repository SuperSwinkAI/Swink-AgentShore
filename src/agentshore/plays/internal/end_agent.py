"""EndAgentPlay — terminate an agent and free its slot."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import aiosqlite

from agentshore.errors import PreconditionFailed
from agentshore.plays.internal.base import InternalPlay
from agentshore.rl.mask_reason import MaskClassification, MaskReason, MaskSource
from agentshore.state import AgentStatus, PlayOutcome, PlayType, SessionState

if TYPE_CHECKING:
    from agentshore.plays.base import PlayExecutionContext, PlayParams
    from agentshore.state import OrchestratorState

_MIN_PLAYS_PER_AGENT = 10


class EndAgentPlay(InternalPlay):
    """Terminate an agent that has run enough plays to be worth evaluating."""

    play_type = PlayType.END_AGENT
    is_handoff = True

    def preconditions(self, state: OrchestratorState) -> list[MaskReason]:
        active = [a for a in state.agents if a.status in (AgentStatus.IDLE, AgentStatus.BUSY)]
        idle = [a for a in active if a.status == AgentStatus.IDLE]

        if state.session_state == SessionState.DRAINING:
            if not idle:
                return [
                    MaskReason(
                        text="no agents to end",
                        classification=MaskClassification.HARD,
                        source=MaskSource.PRECONDITION,
                    )
                ]
            return []
        if not active:
            return [
                MaskReason(
                    text="no agents to end",
                    classification=MaskClassification.HARD,
                    source=MaskSource.PRECONDITION,
                )
            ]
        if len(active) < 2:
            return [
                MaskReason(
                    text="at least 2 agents required before ending one",
                    classification=MaskClassification.INDEFINITE_WAIT,
                    source=MaskSource.PRECONDITION,
                )
            ]
        if not any((a.tasks_completed + a.tasks_failed) > _MIN_PLAYS_PER_AGENT for a in idle):
            return [
                MaskReason(
                    text=f"no agent has more than {_MIN_PLAYS_PER_AGENT} plays yet",
                    classification=MaskClassification.INDEFINITE_WAIT,
                    source=MaskSource.PRECONDITION,
                )
            ]
        return []

    async def execute(
        self,
        state: OrchestratorState,
        params: PlayParams,
        *,
        ctx: PlayExecutionContext,
    ) -> PlayOutcome:
        agent_id = params.agent_id
        if agent_id is None:
            return PlayOutcome.failed(self.play_type, "agent_id not resolved")

        try:
            await ctx.manager.clear(agent_id, force=True)
        except (PreconditionFailed, aiosqlite.Error, sqlite3.Error, RuntimeError, KeyError) as exc:
            return PlayOutcome.failed(self.play_type, str(exc), agent_id=agent_id)

        return PlayOutcome(
            play_type=self.play_type,
            agent_id=agent_id,
            success=True,
            partial=False,
            duration_seconds=0.0,
            token_cost=0,
            dollar_cost=0.0,
            artifacts=[{"type": "agent_ended", "agent_id": agent_id}],
            alignment_delta=0.0,
        )
