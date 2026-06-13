"""CalibrateAlignmentPlay — run agentshore-calibrate-alignment to sync beads task states."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.play_pacing import STANDARD_PLAY_COOLDOWN_PLAYS
from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.plays.skill_backed.gates import (
    BeadsInitializedGate,
    CooldownGate,
    FirstRunWarmupGate,
    InFlightGate,
)
from agentshore.state import PlayType

if TYPE_CHECKING:
    from agentshore.state import OrchestratorState


class CalibrateAlignmentPlay(SkillBackedPlay):
    """Cross-reference open PRs against beads tasks and update closure ratios."""

    def __init__(self, *, cooldown_plays: int = STANDARD_PLAY_COOLDOWN_PLAYS) -> None:
        self.gates = (
            BeadsInitializedGate(no_epics_hint="nothing to calibrate"),
            FirstRunWarmupGate(PlayType.CALIBRATE_ALIGNMENT, threshold=20),
            InFlightGate(PlayType.CALIBRATE_ALIGNMENT),
            CooldownGate(PlayType.CALIBRATE_ALIGNMENT, plays=cooldown_plays),
        )

    @property
    def play_type(self) -> PlayType:
        return PlayType.CALIBRATE_ALIGNMENT

    @property
    def skill_name(self) -> str:
        return "agentshore-calibrate-alignment"

    @property
    def capability(self) -> str | None:
        return "can_implement"

    def estimated_cost(self, state: OrchestratorState) -> float:
        return 0.04
