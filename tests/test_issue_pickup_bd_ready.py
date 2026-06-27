"""Tests for beads-ready integration in IssuePickupPlay preconditions (Track 4)."""

from __future__ import annotations

from agentshore.beads import EpicStatus, ProjectGraph
from agentshore.plays.skill_backed.issue_pickup import IssuePickupPlay
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    IssueSnapshot,
    OrchestratorState,
    SessionState,
)

# Helpers


def _make_agent() -> AgentSnapshot:
    return AgentSnapshot(
        agent_id="agent-1",
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.IDLE,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
    )


def _make_issue(number: int = 1) -> IssueSnapshot:
    return IssueSnapshot(
        issue_number=number,
        title=f"Issue #{number}",
        state="OPEN",
        priority=None,
        labels=[],
        source=None,
    )


def _make_state(*, graph: ProjectGraph | None = None) -> OrchestratorState:
    return OrchestratorState(
        session_id="test",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        open_issues=[_make_issue()],
        agents=[_make_agent()],
        graph=graph,
    )


# Precondition: graph is None — beads not set up, must not block


def test_preconditions_no_graph_does_not_block_on_beads() -> None:
    """When state.graph is None (beads not initialised), the beads gate is silent."""
    state = _make_state(graph=None)
    play = IssuePickupPlay()
    failures = play.preconditions(state)
    assert not any("groom_backlog" in f.text for f in failures), (
        f"Beads gate must not fire when graph is None; got: {failures}"
    )


# Precondition: has_epics but no ready tasks → block


def test_preconditions_epics_no_ready_tasks_blocks() -> None:
    """When the beads graph has epics but no ready tasks, preconditions block."""
    graph = ProjectGraph(
        epics=[
            EpicStatus(
                bead_id="e1", title="Epic 1", total_tasks=3, closed_tasks=0, closure_ratio=0.0
            )
        ],
        tasks_ready=0,
        tasks_total=3,
        global_closure_ratio=0.0,
    )
    state = _make_state(graph=graph)
    play = IssuePickupPlay()
    failures = play.preconditions(state)
    assert any("groom_backlog" in f.text for f in failures), (
        f"Expected groom_backlog message; got: {failures}"
    )


# Precondition: has_epics AND has_ready_tasks → no beads block


def test_preconditions_epics_with_ready_tasks_does_not_block() -> None:
    """When the beads graph has epics AND ready tasks, the beads gate does not fire."""
    graph = ProjectGraph(
        epics=[
            EpicStatus(
                bead_id="e1", title="Epic 1", total_tasks=3, closed_tasks=0, closure_ratio=0.0
            )
        ],
        tasks_ready=2,
        tasks_total=3,
        global_closure_ratio=0.0,
    )
    state = _make_state(graph=graph)
    play = IssuePickupPlay()
    failures = play.preconditions(state)
    assert not any("groom_backlog" in f.text for f in failures), (
        f"Beads gate must not fire when ready tasks exist; got: {failures}"
    )


# Precondition: graph present but no epics → no beads block


def test_preconditions_no_epics_does_not_block() -> None:
    """When the beads graph has no epics at all, the beads gate is silent."""
    graph = ProjectGraph(
        epics=[],
        tasks_ready=0,
        tasks_total=0,
        global_closure_ratio=0.0,
    )
    state = _make_state(graph=graph)
    play = IssuePickupPlay()
    failures = play.preconditions(state)
    assert not any("groom_backlog" in f.text for f in failures), (
        f"Beads gate must not fire when there are no epics; got: {failures}"
    )
