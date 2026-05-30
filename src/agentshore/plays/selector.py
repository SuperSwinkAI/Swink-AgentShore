"""PlaySelector — protocol and concrete implementations for play selection.

``FixedPlanSelector`` yields a pre-recorded sequence of (play_type, params)
pairs; returns None after the sequence is exhausted. Used in tests where the
play sequence is fully prescribed.

Production use relies on ``PPOSelector`` from ``agentshore.rl.selector``, which
uses the trained PPO policy network to select plays from state observations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from agentshore.plays.base import PlayParams
    from agentshore.state import OrchestratorState, PlayType


@runtime_checkable
class PlaySelector(Protocol):
    """Select the next play to execute given the current session state.

    Returns ``None`` to signal that the RL loop should pause (no eligible
    plays remain). The orchestrator will pause and wait for user input
    rather than terminating.
    """

    async def select(self, state: OrchestratorState) -> tuple[PlayType, PlayParams] | None: ...


class FixedPlanSelector:
    """Yield plays from a fixed list; return None when exhausted.

    Useful for testing and the Phase 2Q end-to-end scenario where the play
    sequence is fully prescribed.
    """

    def __init__(
        self,
        plan: list[tuple[PlayType, PlayParams]],
    ) -> None:
        self._plan = list(plan)
        self._index = 0

    async def select(
        self,
        state: OrchestratorState,
    ) -> tuple[PlayType, PlayParams] | None:
        if self._index >= len(self._plan):
            return None
        result = self._plan[self._index]
        self._index += 1
        return result
