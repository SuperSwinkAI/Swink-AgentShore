"""SystematicDebuggingPlay -- investigate failures before fixes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.github.labels import (
    DEBUG_TRIGGER_LABELS,
    ISSUE_PICKUP_SKIP_LABELS,
    ROOT_CAUSE_FOUND_LABEL,
)
from agentshore.github.pr_links import issue_numbers_for_pr
from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.rl.mask_reason import MaskClassification, MaskReason, MaskSource
from agentshore.state import PlayType

if TYPE_CHECKING:
    from agentshore.state import OrchestratorState


class SystematicDebuggingPlay(SkillBackedPlay):
    """Find root cause for explicit QA/debug failures before a fix attempt."""

    @property
    def play_type(self) -> PlayType:
        return PlayType.SYSTEMATIC_DEBUGGING

    @property
    def skill_name(self) -> str:
        return "agentshore-systematic-debugging"

    @property
    def capability(self) -> str | None:
        return "can_implement"

    def preconditions(self, state: OrchestratorState) -> list[MaskReason]:
        issues = self._capability_check(state)
        if issues:
            return issues
        in_flight = set(state.in_flight_issues)
        open_pr_issue_numbers = {
            issue_number
            for pr in state.pull_requests
            if pr.state.upper() == "OPEN"
            for issue_number in issue_numbers_for_pr(pr)
        }
        has_eligible_failure_issue = any(
            iss.issue_number not in in_flight
            and iss.issue_number not in open_pr_issue_numbers
            and not (ISSUE_PICKUP_SKIP_LABELS & set(iss.labels))
            and ROOT_CAUSE_FOUND_LABEL not in iss.labels
            and bool(DEBUG_TRIGGER_LABELS & set(iss.labels))
            for iss in state.open_issues
        )
        if not has_eligible_failure_issue:
            return [
                MaskReason(
                    text=(
                        "no explicit QA/debug issue available "
                        "(all in-flight, PR-linked, or none exist)"
                    ),
                    classification=MaskClassification.HARD,
                    source=MaskSource.CANDIDATE,
                )
            ]
        return []
