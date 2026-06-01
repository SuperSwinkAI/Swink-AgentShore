"""WriteImplementationPlanPlay -- prepare task-level implementation plans."""

from __future__ import annotations

from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.plays.skill_backed.gates import CapabilityGate
from agentshore.state import PlayType


class WriteImplementationPlanPlay(SkillBackedPlay):
    """Write a concrete implementation plan for an issue before coding.

    Candidate validity ("is there an open, uncovered issue to plan?") lives in
    ``EligibilityAuthority._VALIDITY_FNS`` for ``WRITE_IMPLEMENTATION_PLAN`` and
    is appended by the base ``preconditions`` adapter. This play only declares
    the capability gate.
    """

    gates = (CapabilityGate("can_implement"),)

    @property
    def play_type(self) -> PlayType:
        return PlayType.WRITE_IMPLEMENTATION_PLAN

    @property
    def skill_name(self) -> str:
        return "agentshore-write-plan"

    @property
    def capability(self) -> str | None:
        return "can_implement"
