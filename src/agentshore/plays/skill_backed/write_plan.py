"""WriteImplementationPlanPlay -- prepare task-level implementation plans."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.plays.candidates import build_candidate_plan
from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.rl.mask_reason import MaskClassification, MaskReason, MaskSource
from agentshore.state import PlayType

if TYPE_CHECKING:
    from agentshore.state import OrchestratorState


class WriteImplementationPlanPlay(SkillBackedPlay):
    """Write a concrete implementation plan for an issue before coding."""

    @property
    def play_type(self) -> PlayType:
        return PlayType.WRITE_IMPLEMENTATION_PLAN

    @property
    def skill_name(self) -> str:
        return "agentshore-write-plan"

    @property
    def capability(self) -> str | None:
        return "can_implement"

    def preconditions(self, state: OrchestratorState) -> list[MaskReason]:
        issues: list[MaskReason] = []
        if not any(issue.state.upper() == "OPEN" for issue in state.open_issues):
            issues.append(
                MaskReason(
                    text="no open issues available to plan",
                    classification=MaskClassification.HARD,
                    source=MaskSource.PRECONDITION,
                )
            )
        issues += self._capability_check(state)

        if not build_candidate_plan(state).candidates_for(PlayType.WRITE_IMPLEMENTATION_PLAN):
            issues.append(
                MaskReason(
                    text=(
                        "no eligible issue for write_implementation_plan"
                        " (all covered by open PR, in-flight, or labeled out)"
                    ),
                    classification=MaskClassification.HARD,
                    source=MaskSource.CANDIDATE,
                )
            )

        return issues
