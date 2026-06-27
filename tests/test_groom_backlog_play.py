"""Tests for GroomBacklogPlay preconditions and Step 4a strict-link logic."""

from __future__ import annotations

from agentshore.beads import BeadStatus, EpicStatus, GraphTask, ProjectGraph
from agentshore.play_pacing import STANDARD_PLAY_COOLDOWN_PLAYS
from agentshore.plays.skill_backed.groom_backlog import GroomBacklogPlay
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    IssueSnapshot,
    OrchestratorState,
    PlayType,
    SessionState,
)


def _idle_agent(agent_type: AgentType = AgentType.CLAUDE_CODE) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id="agent-1",
        agent_type=agent_type,
        status=AgentStatus.IDLE,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
    )


def _busy_agent() -> AgentSnapshot:
    """An agent that is BUSY — fails the IDLE filter in _capability_check."""
    return AgentSnapshot(
        agent_id="agent-busy",
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.BUSY,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
    )


def _state(
    graph: ProjectGraph | None = None,
    agents: list[AgentSnapshot] | None = None,
    open_issues: list[IssueSnapshot] | None = None,
    total_plays: int = 30,
    plays_since_last_play_type: dict[PlayType, int] | None = None,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="sess",
        session_state=SessionState.RUNNING,
        total_plays=total_plays,
        total_cost=0.0,
        graph=graph,
        agents=agents if agents is not None else [_idle_agent()],
        plays_since_last_play_type=(
            {PlayType.GROOM_BACKLOG: STANDARD_PLAY_COOLDOWN_PLAYS}
            if plays_since_last_play_type is None
            else plays_since_last_play_type
        ),
        open_issues=open_issues if open_issues is not None else [],
    )


def _graph_with_epics(tasks: list[GraphTask] | None = None) -> ProjectGraph:
    return ProjectGraph(
        epics=[
            EpicStatus(
                bead_id="epic-1", title="Auth", total_tasks=3, closed_tasks=1, closure_ratio=0.33
            )
        ],
        tasks=tasks or [],
        tasks_ready=2,
        tasks_total=3,
        global_closure_ratio=0.33,
    )


def _empty_graph() -> ProjectGraph:
    return ProjectGraph()


def _unlinked_ready_task() -> GraphTask:
    """A ready task with no GH issue link — triggers bypass condition 1."""
    return GraphTask(
        bead_id="task-1",
        title="Implement auth",
        status=BeadStatus.OPEN,
        issue_number=None,
        ready=True,
    )


def _issue_snapshot(issue_number: int) -> IssueSnapshot:
    return IssueSnapshot(
        issue_number=issue_number,
        title="Some issue",
        state="open",
        priority=None,
        labels=[],
        source="github",
    )


def test_groom_backlog_blocks_when_graph_is_none() -> None:
    errors = GroomBacklogPlay().preconditions(_state(graph=None))
    assert errors != []
    assert any("beads not initialised" in e.text for e in errors)


def test_groom_backlog_blocks_when_graph_has_no_epics() -> None:
    errors = GroomBacklogPlay().preconditions(_state(graph=_empty_graph()))
    assert errors != []
    assert any("no epics" in e.text for e in errors)


def test_groom_backlog_preconditions_pass_with_epics() -> None:
    assert GroomBacklogPlay().preconditions(_state(graph=_graph_with_epics())) == []


def test_groom_backlog_play_type() -> None:
    assert GroomBacklogPlay().play_type == PlayType.GROOM_BACKLOG


def test_groom_backlog_skill_name() -> None:
    assert GroomBacklogPlay().skill_name == "agentshore-groom-backlog"


def test_groom_backlog_estimated_cost_is_light() -> None:
    cost = GroomBacklogPlay().estimated_cost(_state(graph=_graph_with_epics()))
    assert 0.03 <= cost <= 0.08


def _should_link(bead_title: str, search_results: list[dict[str, str]]) -> bool:
    """Replicate Step 4a linking decision: link only when exactly one result whose
    title matches the bead title exactly (case-insensitive, trimmed).

    Returns True if the link should be applied, False if it must be skipped.
    This mirrors the prose in groom_backlog SKILL.md Step 4a, items 3-4, so
    that the rule is machine-checkable without running an agent.
    """
    if len(search_results) != 1:
        return False
    return search_results[0]["title"].strip().lower() == bead_title.strip().lower()


def test_step4a_links_single_exact_match() -> None:
    """Exactly one result with a title that matches exactly → link."""
    results = [{"number": "42", "title": "Add login page", "state": "open"}]
    assert _should_link("Add login page", results) is True


def test_step4a_links_case_insensitive_match() -> None:
    """Title comparison is case-insensitive."""
    results = [{"number": "7", "title": "add Login Page", "state": "open"}]
    assert _should_link("Add login page", results) is True


def test_step4a_skips_multiple_results() -> None:
    """More than one result → ambiguous → do not link."""
    results = [
        {"number": "42", "title": "Add login page", "state": "open"},
        {"number": "43", "title": "Add login page (mobile)", "state": "open"},
    ]
    assert _should_link("Add login page", results) is False


def test_step4a_skips_zero_results() -> None:
    """No results → nothing to link."""
    assert _should_link("Add login page", []) is False


def test_step4a_skips_partial_title_match() -> None:
    """Single result but title is not an exact match → do not link."""
    results = [{"number": "55", "title": "Add login page (revised)", "state": "open"}]
    assert _should_link("Add login page", results) is False


def test_step4a_skips_keyword_overlap_only() -> None:
    """A result that merely overlaps on keywords (not exact title) must not link."""
    results = [
        {"number": "10", "title": "Add login functionality to the admin page", "state": "open"}
    ]
    assert _should_link("Add login page", results) is False


# H2 fix: bypass conditions are evaluated before the capability gate.
def test_bypass1_no_capable_agent_returns_descriptive_error() -> None:
    """Bypass 1 fires (unlinked ready tasks, no open issues) but no IDLE agent exists.

    Before the H2 fix, the capability gate ran first and returned a generic
    "no IDLE agent" message without the deadlock context.  After the fix,
    the bypass is evaluated first and returns a message that names the urgency
    *and* the capability gap, so the RL selector can see why the play is masked.
    Crucially the return is non-empty (play is masked — no capable executor).
    """
    graph = _graph_with_epics(tasks=[_unlinked_ready_task()])
    state = _state(graph=graph, agents=[_busy_agent()], open_issues=[])

    errors = GroomBacklogPlay().preconditions(state)

    # Play must be masked — a BUSY-only fleet cannot execute groom_backlog.
    assert errors != []
    assert any("urgent groom needed" in e.text for e in errors)
    assert any("unlinked ready tasks" in e.text for e in errors)
    assert any("no IDLE agent" in e.text for e in errors)


def test_bypass2_untracked_gh_issues_with_capable_agent_allows_play() -> None:
    """Bypass 2 fires (untracked GH issue) and an IDLE capable agent exists.

    The play must be allowed (empty preconditions list) when outside cooldown,
    because the graph is out of sync with GitHub.
    """
    # Issue #99 exists in GitHub but has no corresponding task in the beads graph.
    graph = _graph_with_epics(tasks=[])
    state = _state(
        graph=graph,
        agents=[_idle_agent()],
        open_issues=[_issue_snapshot(99)],
    )

    errors = GroomBacklogPlay().preconditions(state)

    assert errors == []


def test_bypass1_unlinked_ready_tasks_respects_recent_groom_cooldown() -> None:
    """Urgent deadlock recovery must not repeat groom_backlog immediately."""
    graph = _graph_with_epics(tasks=[_unlinked_ready_task()])
    state = _state(
        graph=graph,
        agents=[_idle_agent()],
        open_issues=[],
        plays_since_last_play_type={PlayType.GROOM_BACKLOG: 0},
    )

    errors = GroomBacklogPlay().preconditions(state)

    assert [e.text for e in errors] == [
        f"groom_backlog cooldown (0/{STANDARD_PLAY_COOLDOWN_PLAYS} plays since last)"
    ]


def test_bypass2_untracked_gh_issues_respects_recent_groom_cooldown() -> None:
    """A stale graph/cache mismatch must not bypass the post-groom cooldown."""
    graph = _graph_with_epics(tasks=[])
    state = _state(
        graph=graph,
        agents=[_idle_agent()],
        open_issues=[_issue_snapshot(99)],
        plays_since_last_play_type={PlayType.GROOM_BACKLOG: 3},
    )

    errors = GroomBacklogPlay().preconditions(state)

    assert [e.text for e in errors] == [
        f"groom_backlog cooldown (3/{STANDARD_PLAY_COOLDOWN_PLAYS} plays since last)"
    ]


def test_bypass2_untracked_gh_issues_bypasses_first_run_floor() -> None:
    """Urgent graph sync may run early when there is no previous groom."""
    graph = _graph_with_epics(tasks=[])
    state = _state(
        graph=graph,
        agents=[_idle_agent()],
        open_issues=[_issue_snapshot(99)],
        total_plays=2,
        plays_since_last_play_type={},
    )

    errors = GroomBacklogPlay().preconditions(state)

    assert errors == []


def test_normal_path_capability_gate_blocks_when_no_bypass_fires() -> None:
    """Normal path: neither bypass fires and no capable agent is available.

    The capability error from the normal path should be returned verbatim,
    without any urgency prefix.
    """
    # Linked ready task (issue_number set) — bypass 1 does not fire.
    linked_task = GraphTask(
        bead_id="task-2",
        title="Auth task",
        status=BeadStatus.OPEN,
        issue_number=42,
        ready=True,
    )
    # Issue 42 is tracked (matches task.issue_number) — bypass 2 does not fire.
    graph = _graph_with_epics(tasks=[linked_task])
    state = _state(
        graph=graph,
        agents=[_busy_agent()],
        open_issues=[_issue_snapshot(42)],
    )

    errors = GroomBacklogPlay().preconditions(state)

    assert errors != []
    # Normal capability error — no urgency prefix.
    assert not any("urgent groom needed" in e.text for e in errors)
    assert any("no IDLE agent" in e.text for e in errors)
