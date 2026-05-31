"""Tests for the refine_task_breakdown precondition tightening.

Regression for the 2026-05-07 run where this play returned the no-op
"all issues already refined" 25 times (18% of all plays). The fix adds a
agentshore/needs-refinement label check so PPO doesn't see the play as eligible
when there's nothing to refine.
"""

from __future__ import annotations

from agentshore.plays.skill_backed.refine_tasks import RefineTaskBreakdownPlay
from agentshore.state import IssueSnapshot, OrchestratorState, SessionState


def _issue(number: int, labels: list[str]) -> IssueSnapshot:
    return IssueSnapshot(
        issue_number=number,
        title=f"Issue {number}",
        state="open",
        priority=None,
        labels=labels,
        source=None,
    )


def _state(issues: list[IssueSnapshot]) -> OrchestratorState:
    return OrchestratorState(
        session_id="test",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        open_issues=issues,
    )


def test_masked_when_no_open_issues():
    play = RefineTaskBreakdownPlay()
    reasons = play.preconditions(_state([]))
    assert any("no open issues" in r for r in reasons)


def test_masked_when_no_issue_carries_needs_refinement():
    play = RefineTaskBreakdownPlay()
    reasons = play.preconditions(
        _state(
            [
                _issue(1, ["bug", "priority/medium", "size/s"]),
                _issue(2, ["enhancement", "size/s"]),
            ]
        )
    )
    assert any("agentshore/needs-refinement" in r for r in reasons)


def test_eligible_when_at_least_one_issue_needs_refinement():
    play = RefineTaskBreakdownPlay()
    reasons = play.preconditions(
        _state(
            [
                _issue(1, ["priority/medium"]),
                _issue(2, ["agentshore/needs-refinement"]),
                _issue(3, ["size/m"]),
            ]
        )
    )
    assert reasons == []


def test_eligible_when_label_appears_alone():
    play = RefineTaskBreakdownPlay()
    reasons = play.preconditions(_state([_issue(1, ["agentshore/needs-refinement"])]))
    assert reasons == []


def test_masked_when_only_issue_is_already_refined():
    # An issue carrying both needs-refinement and refined must not re-trigger
    # the play — refine already processed it (agentshore/refined).
    play = RefineTaskBreakdownPlay()
    reasons = play.preconditions(
        _state([_issue(1, ["agentshore/needs-refinement", "agentshore/refined"])])
    )
    assert any("agentshore/needs-refinement" in r for r in reasons)


def test_re_armed_when_refined_label_removed():
    # Removing agentshore/refined (e.g. by groom/design-audit) re-enables refine.
    play = RefineTaskBreakdownPlay()
    reasons = play.preconditions(_state([_issue(1, ["agentshore/needs-refinement"])]))
    assert reasons == []
