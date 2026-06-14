"""EndSessionPlay — signal the Orchestrator to begin a graceful drain."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.plays.internal.base import InternalPlay
from agentshore.state import PlayOutcome, PlayType

if TYPE_CHECKING:
    from agentshore.plays.base import PlayExecutionContext, PlayParams
    from agentshore.rl.mask_reason import MaskReason
    from agentshore.state import OrchestratorState


class EndSessionPlay(InternalPlay):
    """Request graceful drain via _process_completion in the Orchestrator."""

    play_type = PlayType.END_SESSION

    def preconditions(self, state: OrchestratorState) -> list[MaskReason]:
        return []

    async def execute(
        self,
        state: OrchestratorState,
        params: PlayParams,
        *,
        ctx: PlayExecutionContext,
    ) -> PlayOutcome:
        reason = params.reason
        if reason is None:
            raw_reason = params.extras.get("drain_reason")
            reason = raw_reason if isinstance(raw_reason, str) and raw_reason else "ppo_selected"
        raw_source = params.extras.get("shutdown_source")
        source = raw_source if isinstance(raw_source, str) and raw_source else "end_session"
        return PlayOutcome(
            play_type=self.play_type,
            agent_id=None,
            success=True,
            partial=False,
            duration_seconds=0.0,
            token_cost=0,
            dollar_cost=0.0,
            artifacts=[
                {
                    "type": "session_event",
                    "event": "drain_requested",
                    "reason": reason,
                    "source": source,
                }
            ],
            alignment_delta=0.0,
        )
