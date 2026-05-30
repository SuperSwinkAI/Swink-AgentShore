"""Tests for DesignAuditPlay preconditions and result validation."""

from __future__ import annotations

from agentshore.beads import EpicStatus, ProjectGraph
from agentshore.plays.skill_backed.design_audit import (
    DesignAuditPlay,
    _validate_design_audit_artifact,
)
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    JsonArtifact,
    OrchestratorState,
    PlayType,
    SessionState,
)


def _idle_agent(
    agent_type: AgentType = AgentType.CLAUDE_CODE,
    status: AgentStatus = AgentStatus.IDLE,
) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id="agent-1",
        agent_type=agent_type,
        status=status,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
    )


def _graph_with_epics() -> ProjectGraph:
    return ProjectGraph(
        epics=[
            EpicStatus(
                bead_id="epic-1",
                title="Core workflow",
                total_tasks=3,
                closed_tasks=1,
                closure_ratio=0.33,
            )
        ],
        tasks_ready=1,
        tasks_total=3,
        global_closure_ratio=0.33,
    )


def _state(
    *,
    graph: ProjectGraph | None = None,
    agents: list[AgentSnapshot] | None = None,
    in_flight: list[PlayType] | None = None,
    plays_since_last_play_type: dict[PlayType, int] | None = None,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="sess",
        session_state=SessionState.RUNNING,
        total_plays=30,
        total_cost=0.0,
        graph=graph,
        agents=agents if agents is not None else [_idle_agent()],
        in_flight_plays=[] if in_flight is None else in_flight,
        plays_since_last_play_type=(
            {PlayType.DESIGN_AUDIT: 20}
            if plays_since_last_play_type is None
            else plays_since_last_play_type
        ),
    )


def _audit_artifact(**overrides: object) -> list[JsonArtifact]:
    artifact = {
        "type": "design_audit",
        "requirements_scanned": 4,
        "gaps_found": 2,
        "issues_created": 1,
        "issues_linked": 1,
        "unresolved_gaps": 0,
        "unknown_requirements": 0,
        "gap_issue_numbers": [101, 102],
    }
    artifact.update(overrides)
    return [artifact]


def test_play_identity() -> None:
    play = DesignAuditPlay()
    assert play.play_type == PlayType.DESIGN_AUDIT
    assert play.skill_name == "agentshore-design-audit"
    assert play.capability == "can_create_issues"


def test_preconditions_block_when_graph_missing() -> None:
    errors = DesignAuditPlay().preconditions(_state(graph=None))
    assert errors
    assert any("beads not initialised" in error for error in errors)


def test_preconditions_block_when_graph_has_no_epics() -> None:
    errors = DesignAuditPlay().preconditions(_state(graph=ProjectGraph()))
    assert errors
    assert any("no epics" in error for error in errors)


def test_preconditions_block_without_idle_issue_creator() -> None:
    errors = DesignAuditPlay().preconditions(
        _state(graph=_graph_with_epics(), agents=[_idle_agent(status=AgentStatus.BUSY)])
    )
    assert errors
    assert any("can_create_issues" in error for error in errors)


def test_preconditions_block_when_in_flight() -> None:
    errors = DesignAuditPlay().preconditions(
        _state(graph=_graph_with_epics(), in_flight=[PlayType.DESIGN_AUDIT])
    )
    assert errors
    assert any("already in flight" in error for error in errors)


def test_preconditions_block_during_cooldown() -> None:
    errors = DesignAuditPlay().preconditions(
        _state(
            graph=_graph_with_epics(),
            plays_since_last_play_type={PlayType.DESIGN_AUDIT: 19},
        )
    )
    assert errors
    assert any("cooldown" in error for error in errors)


def test_preconditions_pass_after_cooldown() -> None:
    assert DesignAuditPlay().preconditions(_state(graph=_graph_with_epics())) == []


def test_validate_design_audit_artifact_accepts_gap_coverage() -> None:
    assert _validate_design_audit_artifact(_audit_artifact()) is None


def test_validate_design_audit_artifact_rejects_missing_audit() -> None:
    assert "missing required design_audit" in _validate_design_audit_artifact([])


def test_validate_design_audit_artifact_rejects_unresolved_gaps() -> None:
    error = _validate_design_audit_artifact(_audit_artifact(unresolved_gaps=1))
    assert error is not None
    assert "unresolved gaps" in error


def test_validate_design_audit_artifact_rejects_more_gaps_than_requirements() -> None:
    error = _validate_design_audit_artifact(_audit_artifact(requirements_scanned=1, gaps_found=2))
    assert error is not None
    assert "more gaps than scanned requirements" in error


def test_validate_design_audit_artifact_rejects_gap_without_issue() -> None:
    error = _validate_design_audit_artifact(_audit_artifact(gap_issue_numbers=[101]))
    assert error is not None
    assert "did not create/link issues for all gaps" in error
