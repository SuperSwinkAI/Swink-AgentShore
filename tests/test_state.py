"""Tests for agentshore.state — enums, frozen dataclasses, OrchestratorState, StateProvider."""

from __future__ import annotations

import dataclasses

import pytest

from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    IssueSnapshot,
    NullStateProvider,
    OrchestratorState,
    PlayOutcome,
    PlayType,
    PullRequestSnapshot,
    SessionState,
    SkillResult,
    StateProvider,
)

# ---------------------------------------------------------------------------
# Enum round-trips
# ---------------------------------------------------------------------------


def test_play_type_round_trip() -> None:
    for pt in PlayType:
        assert PlayType(pt.value) is pt


def test_agent_type_round_trip() -> None:
    for at in AgentType:
        assert AgentType(at.value) is at


def test_agent_status_round_trip() -> None:
    for s in AgentStatus:
        assert AgentStatus(s.value) is s


def test_session_state_round_trip() -> None:
    for s in SessionState:
        assert SessionState(s.value) is s


def test_play_type_invalid_raises() -> None:
    with pytest.raises(ValueError):
        PlayType("not_a_play")


# ---------------------------------------------------------------------------
# Frozen dataclass invariants
# ---------------------------------------------------------------------------


def test_play_outcome_is_frozen() -> None:
    outcome = PlayOutcome(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=100,
        dollar_cost=0.001,
        artifacts=[],
        alignment_delta=0.1,
    )
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        outcome.success = False  # type: ignore[misc]


def test_play_outcome_skipped_outcome() -> None:
    outcome = PlayOutcome.skipped_outcome(
        PlayType.MERGE_PR,
        "no_target",
        error="unresolved parameters",
    )

    assert outcome.success is True
    assert outcome.partial is True
    assert outcome.skipped is True
    assert outcome.skip_category == "no_target"
    assert outcome.error == "unresolved parameters"
    assert outcome.play_id is None


def test_agent_snapshot_is_frozen() -> None:
    snap = AgentSnapshot(
        agent_id="a",
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.IDLE,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
    )
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        snap.status = AgentStatus.BUSY  # type: ignore[misc]


def test_issue_snapshot_is_frozen() -> None:
    issue = IssueSnapshot(
        issue_number=1,
        title="fix it",
        state="open",
        priority=None,
        labels=[],
        source=None,
    )
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        issue.state = "closed"  # type: ignore[misc]


def test_pull_request_snapshot_is_frozen() -> None:
    pr = PullRequestSnapshot(
        pr_number=42,
        title="my pr",
        state="open",
        branch="feat/x",
        issue_number=None,
        labels=[],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
    )
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        pr.state = "merged"  # type: ignore[misc]


def test_skill_result_is_frozen() -> None:
    sr = SkillResult(success=True)
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        sr.success = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# OrchestratorState — mutable with default factories that don't share aliases
# ---------------------------------------------------------------------------


def _running_state(session_id: str) -> OrchestratorState:
    return OrchestratorState(
        session_id=session_id,
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
    )


def test_agentshore_state_agents_lists_are_distinct() -> None:
    s1 = _running_state("s1")
    s2 = _running_state("s2")
    s1.agents.append(
        AgentSnapshot(
            agent_id="x",
            agent_type=AgentType.CLAUDE_CODE,
            status=AgentStatus.IDLE,
            context_size=0,
            total_cost=0.0,
            total_tokens=0,
            tasks_completed=0,
            tasks_failed=0,
        )
    )
    assert len(s2.agents) == 0


def test_agentshore_state_is_mutable() -> None:
    state = OrchestratorState(
        session_id="s",
        session_state=SessionState.INITIALIZING,
        total_plays=0,
        total_cost=0.0,
    )
    state.total_plays = 5
    assert state.total_plays == 5


def test_agentshore_state_v1_contract_fields_default_values() -> None:
    """Per V1_CONTRACT.md §"AgentShore State Snapshot" — these fields must exist."""
    from agentshore.config.models import RunMode

    state = OrchestratorState(
        session_id="s",
        session_state=SessionState.INITIALIZING,
        total_plays=0,
        total_cost=0.0,
    )
    assert state.run_mode == RunMode.SOLO
    assert state.action_space_version == 0
    assert state.policy_version == ""
    assert state.policy_checkpoint_id is None
    assert state.seed_freshness is None
    assert state.learnings_count == 0
    assert state.human_feedback_count == 0


def test_agentshore_state_v1_contract_fields_round_trip_through_serializer() -> None:
    """serialize_state must emit every V1 contract field with the expected key."""
    from agentshore.config.models import RunMode
    from agentshore.ipc.serializer import serialize_state

    state = OrchestratorState(
        session_id="s",
        session_state=SessionState.RUNNING,
        total_plays=42,
        total_cost=1.23,
        run_mode=RunMode.AGENT,
        action_space_version=11,
        policy_version="ppo-v1-deadbeef",
        policy_checkpoint_id="17",
        seed_freshness=3,
        learnings_count=7,
        human_feedback_count=4,
    )
    payload = serialize_state(state)
    assert payload["run_mode"] == "agent"
    assert payload["action_space_version"] == 11
    assert payload["policy_version"] == "ppo-v1-deadbeef"
    assert payload["policy_checkpoint_id"] == "17"
    assert payload["seed_freshness"] == 3
    assert payload["learnings_count"] == 7
    assert payload["human_feedback_count"] == 4


def test_session_stats_aggregate_play_history() -> None:
    from pytest import approx

    from agentshore.core.mixins.snapshots import SnapshotProjector
    from agentshore.data.models import PlayRecord

    history = [
        PlayRecord(
            session_id="s",
            play_type=PlayType.ISSUE_PICKUP.value,
            started_at="2026-01-01T00:00:00Z",
            ended_at="2026-01-01T00:00:10Z",
            success=True,
            duration_ms=10_000,
            token_cost=100,
            dollar_cost=0.25,
            agent_id="agent-a",
        ),
        PlayRecord(
            session_id="s",
            play_type=PlayType.ISSUE_PICKUP.value,
            started_at="2026-01-01T00:01:00Z",
            ended_at="2026-01-01T00:01:20Z",
            success=False,
            duration_ms=20_000,
            token_cost=200,
            dollar_cost=0.75,
            agent_id="agent-a",
        ),
        PlayRecord(
            session_id="s",
            play_type=PlayType.RUN_QA.value,
            started_at="2026-01-01T00:02:00Z",
            ended_at="2026-01-01T00:02:30Z",
            success=True,
            duration_ms=30_000,
            token_cost=300,
            dollar_cost=0.50,
            agent_id="agent-a",
        ),
        # In-flight placeholder: dispatched but not yet finalized. The
        # success=False default must NOT be counted as a failure — the agent is
        # still doing the work. Mirrors the real placeholder row
        # (``ended_at``/``agent_id``/``duration_ms`` all unset).
        PlayRecord(
            session_id="s",
            play_type=PlayType.ISSUE_PICKUP.value,
            started_at="2026-01-01T00:03:00Z",
            success=False,
        ),
    ]

    stats = SnapshotProjector.compute_session_stats(history)

    # The 4th row is an in-flight placeholder (ended_at unset): it must be
    # excluded from every ok/fail/total counter so a running play is never
    # mislabelled a failure. If it leaked in, total_plays would be 4 and
    # failed_plays would be 2.
    assert stats.total_plays == 3
    assert stats.successful_plays == 2
    assert stats.failed_plays == 1
    assert stats.success_rate == approx(2 / 3)
    assert stats.total_cost == approx(1.5)
    assert stats.avg_cost_per_play == approx(0.5)
    assert stats.total_tokens == 600
    assert stats.avg_duration_seconds == approx(20.0)

    by_type = {row.play_type: row for row in stats.by_play_type}
    issue_pickup = by_type[PlayType.ISSUE_PICKUP]
    # 3 issue_pickup rows exist but one is in-flight, so the per-type table
    # counts only the 2 finalized ones (not 3 total / 2 failed).
    assert issue_pickup.total == 2
    assert issue_pickup.successful == 1
    assert issue_pickup.failed == 1
    assert issue_pickup.success_rate == approx(0.5)
    assert issue_pickup.total_cost == approx(1.0)
    assert issue_pickup.avg_duration_seconds == approx(15.0)

    run_qa = by_type[PlayType.RUN_QA]
    assert run_qa.total == 1
    assert run_qa.successful == 1
    assert run_qa.failed == 0

    # Agent specialization derived from the same history (Issue #333).
    cells = {(c.agent_id, c.play_type): c for c in stats.agent_specialization}
    pickup_cell = cells[("agent-a", PlayType.ISSUE_PICKUP)]
    assert pickup_cell.total == 2
    assert pickup_cell.successful == 1
    assert pickup_cell.failed == 1
    assert pickup_cell.success_rate == approx(0.5)
    qa_cell = cells[("agent-a", PlayType.RUN_QA)]
    assert qa_cell.total == 1
    assert qa_cell.successful == 1
    assert qa_cell.success_rate == approx(1.0)


def test_agentshore_state_open_issues_isolated() -> None:
    s1 = _running_state("a")
    s2 = _running_state("b")
    s1.open_issues.append(
        IssueSnapshot(
            issue_number=1,
            title="t",
            state="open",
            priority=None,
            labels=[],
            source=None,
        )
    )
    assert len(s2.open_issues) == 0


def test_project_open_issues_enriches_beads_linkage() -> None:
    from agentshore.beads import BeadStatus, GraphTask, ProjectGraph
    from agentshore.core.mixins.snapshots import SnapshotProjector
    from agentshore.data.models import GitHubIssueRecord

    graph = ProjectGraph(
        tasks=[
            GraphTask(
                bead_id="task-42",
                title="Issue task",
                status=BeadStatus.OPEN,
                parent_id="epic-1",
                epic_id="epic-1",
                epic_title="Auth",
                external_ref="gh-42",
                issue_number=42,
                ready=True,
            )
        ]
    )
    records = [
        GitHubIssueRecord(
            issue_number=42,
            session_id="s1",
            title="Mirrored issue",
            state="open",
            created_at="2026-01-01T00:00:00Z",
        ),
        GitHubIssueRecord(
            issue_number=99,
            session_id="s1",
            title="Missing mirror",
            state="open",
            created_at="2026-01-01T00:00:00Z",
        ),
    ]

    mirrored, missing = SnapshotProjector.project_open_issues(records, graph)

    assert mirrored.bead_id == "task-42"
    assert mirrored.bead_epic_id == "epic-1"
    assert mirrored.bead_ready is True
    assert mirrored.bead_mirror_status == "mirrored"
    assert missing.bead_id is None
    assert missing.bead_mirror_status == "missing"


def test_project_open_issues_mirrors_closed_graph_tasks() -> None:
    from agentshore.beads import BeadStatus, GraphTask, ProjectGraph
    from agentshore.core.mixins.snapshots import SnapshotProjector
    from agentshore.data.models import GitHubIssueRecord

    graph = ProjectGraph(
        tasks=[
            GraphTask(
                bead_id="task-17",
                title="Closed issue task",
                status=BeadStatus.CLOSED,
                parent_id="story-1",
                epic_id="epic-1",
                epic_title="Cleanup",
                external_ref="gh-17",
                issue_number=17,
                ready=False,
            )
        ]
    )
    records = [
        GitHubIssueRecord(
            issue_number=17,
            session_id="s1",
            title="Closed mirrored issue",
            state="closed",
            created_at="2026-01-01T00:00:00Z",
            closed_at="2026-01-02T00:00:00Z",
        )
    ]

    (snapshot,) = SnapshotProjector.project_open_issues(records, graph)

    assert snapshot.bead_id == "task-17"
    assert snapshot.bead_status == "closed"
    assert snapshot.bead_ready is False
    assert snapshot.bead_mirror_status == "mirrored"


# ---------------------------------------------------------------------------
# StateProvider protocol
# ---------------------------------------------------------------------------


def test_null_state_provider_is_state_provider() -> None:
    provider = NullStateProvider()
    assert isinstance(provider, StateProvider)


def test_custom_state_provider_passes_protocol() -> None:
    from agentshore.plays.base import PlayParams

    class _Fake:
        async def on_state_update(self, state: OrchestratorState) -> None:
            pass

        async def on_play_started(self, play_type: PlayType, params: PlayParams) -> None:
            pass

        async def on_play_completed(self, play: PlayOutcome) -> None:
            pass

        async def on_agent_changed(self, agent_id: str, status: AgentStatus) -> None:
            pass

        async def on_agent_subprocess_spawned(
            self, agent_id: str, agent_type: AgentType, pid: int
        ) -> None:
            pass

        async def on_agent_subprocess_exited(
            self, agent_id: str, agent_type: AgentType, pid: int, exit_code: int | None
        ) -> None:
            pass

        async def on_feedback_requested(self, reason: str) -> None:
            pass

        async def on_session_paused(self, reason: str) -> None:
            pass

        async def on_session_draining(self, reason: str) -> None:
            pass

        async def on_session_ended(self, reason: str) -> None:
            pass

        async def on_bootstrap_phase(self, phase: str, status: str, elapsed_ms: float) -> None:
            pass

    assert isinstance(_Fake(), StateProvider)
