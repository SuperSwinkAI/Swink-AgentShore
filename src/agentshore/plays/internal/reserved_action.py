"""Reserved action slots kept for policy shape compatibility."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.plays.internal.base import InternalPlay
from agentshore.rl.mask_reason import RESERVED_SLOT, MaskReason
from agentshore.state import PlayOutcome, PlayType

if TYPE_CHECKING:
    from agentshore.plays.base import PlayExecutionContext, PlayParams
    from agentshore.state import OrchestratorState

_RESERVED_ERROR = "reserved action slot"


class _ReservedActionPlay(InternalPlay):
    """No-op placeholder for action-space slots reserved for future plays."""

    def preconditions(self, state: OrchestratorState) -> list[MaskReason]:
        return [RESERVED_SLOT]

    async def execute(
        self,
        state: OrchestratorState,
        params: PlayParams,
        *,
        ctx: PlayExecutionContext,
    ) -> PlayOutcome:
        return PlayOutcome.failed(self.play_type, _RESERVED_ERROR)


class FutureFourPlay(_ReservedActionPlay):
    """Reserved replacement for the fourth future slot (idx 14)."""

    play_type = PlayType.FUTURE_4


class FutureSevenPlay(_ReservedActionPlay):
    """Reserved replacement for the seventh future slot."""

    play_type = PlayType.FUTURE_7


class FutureEightPlay(_ReservedActionPlay):
    """Reserved replacement for the eighth future slot (idx 21)."""

    play_type = PlayType.FUTURE_8
