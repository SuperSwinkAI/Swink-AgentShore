"""PrunePlay — run agentshore-prune to retire stale worktrees, branches, and beads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.beads import BeadStatus
from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.plays.skill_backed.gates import (
    CapabilityGate,
    CooldownGate,
    InFlightGate,
)
from agentshore.rl.mask_reason import MaskClassification, MaskReason, MaskSource
from agentshore.state import PlayType

if TYPE_CHECKING:
    from agentshore.state import OrchestratorState


_STALE_LINKED_BEAD_THRESHOLD = 10
_PRUNE_COOLDOWN_PLAYS = 20


def _stale_linked_bead_count(state: OrchestratorState) -> int:
    """Count open beads whose linked GH issue is not in the open issue set.

    Upper bound — the agent does the precise CLOSED-on-GitHub check at
    execute time. Untracked issues (not in the open set because they
    haven't been refreshed) get counted here; the agent filters them
    out before closing anything.
    """
    if state.graph is None:
        return 0
    open_issue_numbers = {
        iss.issue_number for iss in state.open_issues if iss.state.upper() == "OPEN"
    }
    return sum(
        1
        for task in state.graph.tasks
        if task.status == BeadStatus.OPEN
        and task.issue_number is not None
        and task.issue_number not in open_issue_numbers
    )


class PrunePlay(SkillBackedPlay):
    """Retire infrastructure debt the orchestrator can't clear mid-session.

    Three sweeps: orphan worktrees, dead local/remote branches, and beads
    whose linked GH issue is closed. Conservative on beads — unlinked
    decomposition residue is never touched.
    """

    gates = (
        CapabilityGate("can_implement"),
        InFlightGate(PlayType.PRUNE),
        CooldownGate(PlayType.PRUNE, plays=_PRUNE_COOLDOWN_PLAYS),
    )

    @property
    def play_type(self) -> PlayType:
        return PlayType.PRUNE

    @property
    def skill_name(self) -> str:
        return "agentshore-prune"

    @property
    def capability(self) -> str | None:
        return "can_implement"

    def preconditions(self, state: OrchestratorState) -> list[MaskReason]:
        reasons = super().preconditions(state)
        if reasons:
            return reasons
        # Threshold gate: only unmask Prune when there's measurable debt.
        # We have one cheap state-derivable signal — stale linked beads —
        # so the gate keys off that. Worktree/branch debt is discovered
        # by the agent at execute time and counts toward future prune
        # eligibility on subsequent ticks (via the graph diff after the
        # post-prune refresh).
        if _stale_linked_bead_count(state) < _STALE_LINKED_BEAD_THRESHOLD:
            return [
                MaskReason(
                    text=(
                        f"no prune-worthy debt (<{_STALE_LINKED_BEAD_THRESHOLD} stale linked beads)"
                    ),
                    classification=MaskClassification.HARD,
                    source=MaskSource.PRECONDITION,
                )
            ]
        return []

    def estimated_cost(self, state: OrchestratorState) -> float:
        return 0.10
