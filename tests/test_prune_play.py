"""PrunePlay preconditions.

Prune is gated on three signals beyond the usual capability + in-flight
+ cooldown stack: a beads-initialized guard, and a debt threshold that
keeps the play masked until there's measurable stale work to justify
the dispatch cost.
"""

from __future__ import annotations

from agentshore.beads import BeadStatus, GraphTask, ProjectGraph
from agentshore.plays.skill_backed.prune import (
    _STALE_LINKED_BEAD_THRESHOLD,
    PrunePlay,
)
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    IssueSnapshot,
    OrchestratorState,
    PlayType,
    SessionState,
)


def _capable_agent() -> AgentSnapshot:
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


def _issue(number: int, *, state: str = "open") -> IssueSnapshot:
    return IssueSnapshot(
        issue_number=number,
        title=f"Issue #{number}",
        state=state,
        priority=None,
        labels=[],
        source=None,
    )


def _bead(bead_id: str, *, issue_number: int | None, status: BeadStatus) -> GraphTask:
    return GraphTask(
        bead_id=bead_id,
        title=f"Task {bead_id}",
        status=status,
        external_ref=f"gh-{issue_number}" if issue_number is not None else None,
        issue_number=issue_number,
    )


def _state(
    *,
    open_issues: list[IssueSnapshot],
    tasks: list[GraphTask],
    plays_since_last_play_type: dict[PlayType, int] | None = None,
    in_flight: list[PlayType] | None = None,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="test",
        session_state=SessionState.RUNNING,
        total_plays=50,
        total_cost=0.0,
        open_issues=open_issues,
        agents=[_capable_agent()],
        graph=ProjectGraph(tasks=tasks),
        plays_since_last_play_type=plays_since_last_play_type or {},
        in_flight_plays=in_flight or [],
    )


def test_prune_masked_when_no_stale_linked_beads() -> None:
    """Below the debt threshold, Prune stays masked with a clear reason."""
    play = PrunePlay()
    issues = [_issue(101)]
    # One linked bead whose GH issue is in the open set -> not stale.
    tasks = [_bead("bd-001", issue_number=101, status=BeadStatus.OPEN)]
    state = _state(open_issues=issues, tasks=tasks)
    reasons = play.preconditions(state)
    assert any("no prune-worthy debt" in r.text for r in reasons)


def test_prune_eligible_when_threshold_reached() -> None:
    """At or above the threshold of stale-linked beads, preconditions clear."""
    play = PrunePlay()
    # 10 open beads each linked to a closed (not in open_issues) GH issue.
    issues = [_issue(999)]  # one unrelated open issue
    stale_count = _STALE_LINKED_BEAD_THRESHOLD
    tasks = [
        _bead(f"bd-{i:03d}", issue_number=200 + i, status=BeadStatus.OPEN)
        for i in range(stale_count)
    ]
    state = _state(open_issues=issues, tasks=tasks)
    assert play.preconditions(state) == []


def test_prune_only_counts_open_beads() -> None:
    """Closed beads pointing at closed GH issues don't count toward the debt."""
    play = PrunePlay()
    issues: list[IssueSnapshot] = []
    # All beads point to (closed) issues but the beads themselves are CLOSED.
    tasks = [
        _bead(f"bd-{i:03d}", issue_number=200 + i, status=BeadStatus.CLOSED)
        for i in range(_STALE_LINKED_BEAD_THRESHOLD * 2)
    ]
    state = _state(open_issues=issues, tasks=tasks)
    reasons = play.preconditions(state)
    assert any("no prune-worthy debt" in r.text for r in reasons)


def test_prune_ignores_unlinked_beads() -> None:
    """Beads without external_ref never count — unlinked is out of scope."""
    play = PrunePlay()
    issues: list[IssueSnapshot] = []
    # Lots of OPEN unlinked beads — Prune should not consider these.
    tasks = [
        _bead(f"bd-{i:03d}", issue_number=None, status=BeadStatus.OPEN)
        for i in range(_STALE_LINKED_BEAD_THRESHOLD * 3)
    ]
    state = _state(open_issues=issues, tasks=tasks)
    reasons = play.preconditions(state)
    assert any("no prune-worthy debt" in r.text for r in reasons)


def test_prune_masked_when_in_flight() -> None:
    """In-flight Prune blocks a concurrent dispatch."""
    play = PrunePlay()
    issues = [_issue(999)]
    tasks = [
        _bead(f"bd-{i:03d}", issue_number=200 + i, status=BeadStatus.OPEN)
        for i in range(_STALE_LINKED_BEAD_THRESHOLD)
    ]
    state = _state(open_issues=issues, tasks=tasks, in_flight=[PlayType.PRUNE])
    reasons = play.preconditions(state)
    assert any("prune already in flight" in r.text for r in reasons)


def test_prune_masked_within_cooldown() -> None:
    """20-play cooldown after the last completion."""
    play = PrunePlay()
    issues = [_issue(999)]
    tasks = [
        _bead(f"bd-{i:03d}", issue_number=200 + i, status=BeadStatus.OPEN)
        for i in range(_STALE_LINKED_BEAD_THRESHOLD)
    ]
    state = _state(
        open_issues=issues,
        tasks=tasks,
        plays_since_last_play_type={PlayType.PRUNE: 5},
    )
    reasons = play.preconditions(state)
    assert any("prune cooldown (5/20" in r.text for r in reasons)


def test_prune_masked_without_beads_graph() -> None:
    """Without a beads graph there's no stale-linked count, so the threshold gate fires."""
    play = PrunePlay()
    state = OrchestratorState(
        session_id="test",
        session_state=SessionState.RUNNING,
        total_plays=50,
        total_cost=0.0,
        open_issues=[],
        agents=[_capable_agent()],
        graph=None,
    )
    reasons = play.preconditions(state)
    assert any("no prune-worthy debt" in r.text for r in reasons)


def test_prune_play_metadata() -> None:
    """Skill name and capability are the public contract — assert them."""
    play = PrunePlay()
    assert play.play_type == PlayType.PRUNE
    assert play.skill_name == "agentshore-prune"
    assert play.capability == "can_implement"
