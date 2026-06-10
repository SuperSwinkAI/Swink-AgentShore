"""WriteImplementationPlanPlay -- prepare task-level implementation plans."""

from __future__ import annotations

from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.plays.skill_backed.gates import CapabilityGate, DependenciesResolvedGate
from agentshore.state import PlayType


class WriteImplementationPlanPlay(SkillBackedPlay):
    """Write a concrete implementation plan for an issue before coding.

    Candidate validity ("is there an open, uncovered issue to plan?") lives in
    ``EligibilityAuthority._VALIDITY_FNS`` for ``WRITE_IMPLEMENTATION_PLAN`` and
    is appended by the base ``preconditions`` adapter. This play declares the
    capability gate and the dependency-resolution gate (#96) so issues whose
    beads task still has unresolved ``blocked_by_ids`` are rejected before an
    agent is dispatched.
    """

    gates = (CapabilityGate("can_implement"), DependenciesResolvedGate())

    @property
    def play_type(self) -> PlayType:
        return PlayType.WRITE_IMPLEMENTATION_PLAN

    @property
    def skill_name(self) -> str:
        return "agentshore-write-plan"

    @property
    def capability(self) -> str | None:
        return "can_implement"
