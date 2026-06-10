"""CleanupPlay — run agentshore-cleanup to sweep code quality across the project."""

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


class CleanupPlay(SkillBackedPlay):
    """Run a language-agnostic code-quality sweep: lint, format, typecheck, test.

    Auto-fixes are pushed as a PR; unfixable failures are filed as issues.
    Cost/time penalties are not waived — the configured standard cooldown
    prevents PPO from spamming this play for its small success bonus. There is
    intentionally no open-issue ceiling: a large backlog is exactly when trunk
    quality debt tends to accumulate, so cleanup must stay reachable on busy
    projects.
    """

    def __init__(self, *, cooldown_plays: int = STANDARD_PLAY_COOLDOWN_PLAYS) -> None:
        self.gates = (
            CapabilityGate("can_implement"),
            InFlightGate(PlayType.CLEANUP),
            FirstRunWarmupGate(PlayType.CLEANUP, threshold=20, prerequisite=PlayType.SEED_PROJECT),
            CooldownGate(PlayType.CLEANUP, plays=cooldown_plays),
        )

    @property
    def play_type(self) -> PlayType:
        return PlayType.CLEANUP

    @property
    def skill_name(self) -> str:
        return "agentshore-cleanup"

    @property
    def capability(self) -> str | None:
        return "can_implement"
