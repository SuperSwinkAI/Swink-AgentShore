"""GroomBacklogPlay — run agentshore-groom-backlog to reorganise the beads graph."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.plays.skill_backed.gates import BeadsInitializedGate, CooldownGate, InFlightGate
from agentshore.rl.mask_reason import MaskClassification, MaskReason, MaskSource
from agentshore.state import PlayType

if TYPE_CHECKING:
    from agentshore.state import OrchestratorState


_GROOM_BACKLOG_MIN_PLAYS = 20


def _has_unlinked_ready_tasks(state: OrchestratorState) -> bool:
    """Return True when the beads graph has ready tasks with no GH issue link.

    Used to detect the deadlock scenario where issue_pickup is blocked (no
    open GH issues) but beads tasks without external_refs are keeping the
    global_closure_ratio below the end_session threshold.
    """
    if state.graph is None:
        return False
    return any(t.issue_number is None and t.ready for t in state.graph.tasks)


def _has_untracked_gh_issues(state: OrchestratorState) -> bool:
    """Return True when open GH issues exist that have no corresponding beads task.

    Detects issues created outside AgentShore (by humans, QA skill, or other
    automation) that are not yet in the beads graph.
    """
    if state.graph is None:
        return False
    tracked = {t.issue_number for t in state.graph.tasks if t.issue_number is not None}
    return any(i.issue_number not in tracked for i in state.open_issues)


class GroomBacklogPlay(SkillBackedPlay):
    """Audit and reorganise the beads project graph — close stale beads, fix labels, relink."""

    gates = (
        BeadsInitializedGate(),
        InFlightGate(PlayType.GROOM_BACKLOG),
        CooldownGate(PlayType.GROOM_BACKLOG, plays=20),
    )

    @property
    def play_type(self) -> PlayType:
        return PlayType.GROOM_BACKLOG

    @property
    def skill_name(self) -> str:
        return "agentshore-groom-backlog"

    @property
    def capability(self) -> str | None:
        return "can_run_skill"

    def preconditions(self, state: OrchestratorState) -> list[MaskReason]:
        reasons = super().preconditions(state)
        if reasons:
            return reasons
        # Evaluate bypass conditions BEFORE the capability gate so that urgent
        # deadlock-recovery scenarios are not silently masked when the only idle
        # agent happens to lack can_run_skill.
        #
        # Bypass 1: No open GH issues but ready beads tasks have no issue links —
        #   without this bypass the session spins on selector_idle forever
        #   (issue_pickup blocked, end_session masked by low closure ratio).
        # Bypass 2: Open GH issues exist that have no corresponding beads task —
        #   issues created outside AgentShore (by humans or QA skill) need to
        #   be synced into the graph before issue_pickup can route them.
        #
        # If a bypass fires but no capable agent is available, we return a
        # descriptive error rather than [] so the RL selector sees a clear
        # reason in the log instead of silently blocking.
        if _has_unlinked_ready_tasks(state) and not state.open_issues:
            cap_issues = self._capability_check(state)
            if cap_issues:
                return [
                    MaskReason(
                        text=(
                            "urgent groom needed (unlinked ready tasks, no open issues)"
                            f" but {cap_issues[0].text}"
                        ),
                        classification=MaskClassification.TRANSIENT,
                        source=MaskSource.ELIGIBILITY,
                    )
                ]
            return []
        if _has_untracked_gh_issues(state):
            cap_issues = self._capability_check(state)
            if cap_issues:
                return [
                    MaskReason(
                        text=f"urgent groom needed (untracked GH issues) but {cap_issues[0].text}",
                        classification=MaskClassification.TRANSIENT,
                        source=MaskSource.ELIGIBILITY,
                    )
                ]
            return []
        # Normal path: capability check applies before the first-run floor.
        cap_issues = self._capability_check(state)
        if cap_issues:
            return cap_issues
        first_run = self.play_type not in state.plays_since_last_play_type
        if first_run and state.total_plays < _GROOM_BACKLOG_MIN_PLAYS:
            return [
                MaskReason(
                    text=(
                        f"too early for first groom "
                        f"({state.total_plays}/{_GROOM_BACKLOG_MIN_PLAYS} plays)"
                    ),
                    classification=MaskClassification.INDEFINITE_WAIT,
                    source=MaskSource.PRECONDITION,
                )
            ]
        return []

    def estimated_cost(self, state: OrchestratorState) -> float:
        return 0.05
