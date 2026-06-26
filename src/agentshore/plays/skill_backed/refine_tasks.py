"""RefineTaskBreakdownPlay — run agentshore-refine-tasks on an issue."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.rl.mask_reason import MaskClassification, MaskReason, MaskSource
from agentshore.state import PlayType

if TYPE_CHECKING:
    from agentshore.state import OrchestratorState


class RefineTaskBreakdownPlay(SkillBackedPlay):
    """Decompose an issue into more granular sub-tasks."""

    @property
    def play_type(self) -> PlayType:
        return PlayType.REFINE_TASK_BREAKDOWN

    @property
    def skill_name(self) -> str:
        return "agentshore-refine-tasks"

    @property
    def capability(self) -> str | None:
        return "can_run_skill"

    def preconditions(self, state: OrchestratorState) -> list[MaskReason]:
        if not state.open_issues:
            return [
                MaskReason(
                    text="no open issues to refine",
                    classification=MaskClassification.HARD,
                    source=MaskSource.CANDIDATE,
                )
            ]
        # Gate stops the play no-opping for dispatch reward when no issue carries
        # the label (was 25/138 plays, 18%). Mirror candidates.issue_available_
        # for_refine: eligible = needs-refinement AND not already refined.
        if not any(
            "agentshore/needs-refinement" in i.labels and "agentshore/refined" not in i.labels
            for i in state.open_issues
        ):
            return [
                MaskReason(
                    text="no issues carry agentshore/needs-refinement",
                    classification=MaskClassification.HARD,
                    source=MaskSource.CANDIDATE,
                )
            ]
        return []
