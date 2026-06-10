"""RunQAPlay — run agentshore-run-qa to validate a branch."""

from __future__ import annotations

from agentshore.play_pacing import STANDARD_PLAY_COOLDOWN_PLAYS
from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.plays.skill_backed.gates import (
    CapabilityGate,
    CooldownGate,
    FirstRunWarmupGate,
    InFlightGate,
)
from agentshore.state import PlayType


class RunQAPlay(SkillBackedPlay):
    """Run QA / test suite on a branch.

    QA validates trunk/default-branch state and is not identity-blocked. Tier
    eligibility and ``can_test`` capability are enforced by agent selection.
    """

    def __init__(self, *, cooldown_plays: int = STANDARD_PLAY_COOLDOWN_PLAYS) -> None:
        self.gates = (
            CapabilityGate("can_test"),
            InFlightGate(PlayType.RUN_QA),
            FirstRunWarmupGate(PlayType.RUN_QA, threshold=20),
            CooldownGate(PlayType.RUN_QA, plays=cooldown_plays),
        )

    @property
    def play_type(self) -> PlayType:
        return PlayType.RUN_QA

    @property
    def skill_name(self) -> str:
        return "agentshore-run-qa"

    @property
    def capability(self) -> str | None:
        return "can_test"
