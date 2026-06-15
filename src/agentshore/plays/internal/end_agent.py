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

# Guards against micro-agent churn: tier-mismatched spawns terminated in <1s
# would waste instantiate + end_agent slot pairs and add RL noise.
#
# History:
#   - desktop-lyfb (2026-05-21) lowered 10 → 5 to give PPO faster termination
#     signal on convergence.
#   - 2026-05-22 raised back to 10 after observing premature Codex termination
#     agent did 5 task plays (1 merge_pr
#     fail on dirty trunk, 4 successes incl. 2 code_review + 2 PR ops) and was
#     end_agent'd immediately, before the bootstrap cleanup had even finished.
#     5 amortizes instantiate cost over too few plays once an agent is
#     productive; 10 keeps healthy agents working long enough to be worth their
#     spin-up tokens while still allowing PPO to retire clearly-weak agents.
_MIN_PLAYS_PER_AGENT = 10


class EndAgentPlay(InternalPlay):
    """Terminate an agent that has run enough plays to be worth evaluating."""

    play_type = PlayType.END_AGENT
    # Terminating handoff: the executor snapshots the agent's context size before
    # this play resets it, and records a handoff row on success.
    is_handoff = True

    def preconditions(self, state: OrchestratorState) -> list[MaskReason]:
        active = [a for a in state.agents if a.status in (AgentStatus.IDLE, AgentStatus.BUSY)]
        idle = [a for a in active if a.status == AgentStatus.IDLE]

        # During drain, bypass the minimum-plays gate so the
        # session can wind down regardless of how many plays each agent has run.
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
            # force=True: the executor marks the target agent in-flight with this
            # play's own END_AGENT marker before execute() runs, so the default
            # active-play guard in clear() would always reject the retirement.
            # Preconditions guarantee the agent is IDLE, so the only in-flight
            # play is the synthetic end_agent marker itself (#154).
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
