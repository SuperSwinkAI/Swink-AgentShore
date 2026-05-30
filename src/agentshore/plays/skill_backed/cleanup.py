"""CleanupPlay — run agentshore-cleanup to sweep code quality across the project."""

from __future__ import annotations

from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.plays.skill_backed.gates import (
    CapabilityGate,
    CooldownGate,
    FirstRunWarmupGate,
    InFlightGate,
    OpenIssueCeilingGate,
)
from agentshore.state import PlayType


class CleanupPlay(SkillBackedPlay):
    """Run a language-agnostic code-quality sweep: lint, format, typecheck, test.

    Auto-fixes are pushed as a PR; unfixable failures are filed as issues.
    Cost/time penalties are not waived — the 50-play cooldown and open-issues
    ceiling prevent PPO from spamming this play for its small success bonus.
    """

    gates = (
        CapabilityGate("can_implement"),
        InFlightGate(PlayType.CLEANUP),
        FirstRunWarmupGate(PlayType.CLEANUP, threshold=20, prerequisite=PlayType.SEED_PROJECT),
        CooldownGate(PlayType.CLEANUP, plays=20),
        OpenIssueCeilingGate(ceiling=15),
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
