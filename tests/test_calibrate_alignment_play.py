"""Tests for CalibrateAlignmentPlay preconditions."""

from __future__ import annotations

from agentshore.beads import EpicStatus, ProjectGraph
from agentshore.play_pacing import STANDARD_PLAY_COOLDOWN_PLAYS
from agentshore.plays.skill_backed.calibrate_alignment import CalibrateAlignmentPlay
from agentshore.state import OrchestratorState, PlayType, SessionState


def _state(
    graph: ProjectGraph | None = None,
    in_flight: list[PlayType] | None = None,
    total_plays: int = 30,
    plays_since_last_play_type: dict[PlayType, int] | None = None,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="sess",
        session_state=SessionState.RUNNING,
        total_plays=total_plays,
        total_cost=0.0,
        graph=graph,
        in_flight_plays=[] if in_flight is None else in_flight,
        plays_since_last_play_type=(
            {PlayType.CALIBRATE_ALIGNMENT: STANDARD_PLAY_COOLDOWN_PLAYS}
            if plays_since_last_play_type is None and total_plays >= 20
            else (plays_since_last_play_type or {})
        ),
    )


def _graph_with_epics() -> ProjectGraph:
    epic = EpicStatus(
        bead_id="bd-001", title="E1", total_tasks=4, closed_tasks=2, closure_ratio=0.5
    )
    return ProjectGraph(epics=[epic], tasks_ready=2, tasks_total=4, global_closure_ratio=0.5)


def _graph_no_epics() -> ProjectGraph:
    return ProjectGraph()


# ---------------------------------------------------------------------------
# preconditions: graph is None
# ---------------------------------------------------------------------------


def test_preconditions_blocks_when_graph_is_none() -> None:
    play = CalibrateAlignmentPlay()
    result = play.preconditions(_state(graph=None))
    assert result != []
    assert any("beads not initialised" in r.text for r in result)


# ---------------------------------------------------------------------------
# preconditions: graph exists but has no epics
# ---------------------------------------------------------------------------


def test_preconditions_blocks_when_graph_has_no_epics() -> None:
    play = CalibrateAlignmentPlay()
    result = play.preconditions(_state(graph=_graph_no_epics()))
    assert result != []
    assert any("no epics" in r.text for r in result)


# ---------------------------------------------------------------------------
# preconditions: graph has epics — should pass
# ---------------------------------------------------------------------------


def test_preconditions_passes_when_graph_has_epics() -> None:
    play = CalibrateAlignmentPlay()
    result = play.preconditions(_state(graph=_graph_with_epics()))
    assert result == []


# ---------------------------------------------------------------------------
# preconditions: blocked when already in flight
# ---------------------------------------------------------------------------


def test_preconditions_blocks_when_in_flight() -> None:
    play = CalibrateAlignmentPlay()
    result = play.preconditions(
        _state(graph=_graph_with_epics(), in_flight=[PlayType.CALIBRATE_ALIGNMENT])
    )
    assert result != []
    assert any("in flight" in r.text or "calibrate_alignment" in r.text for r in result)


# ---------------------------------------------------------------------------
# play_type and skill_name identity
# ---------------------------------------------------------------------------


def test_play_type_is_calibrate_alignment() -> None:
    assert CalibrateAlignmentPlay().play_type == PlayType.CALIBRATE_ALIGNMENT


def test_skill_name() -> None:
    assert CalibrateAlignmentPlay().skill_name == "agentshore-calibrate-alignment"


def test_capability_is_can_implement() -> None:
    assert CalibrateAlignmentPlay().capability == "can_implement"


# ---------------------------------------------------------------------------
# estimated_cost is in the expected range
# ---------------------------------------------------------------------------


def test_estimated_cost_in_range() -> None:
    play = CalibrateAlignmentPlay()
    state = _state(graph=_graph_with_epics())
    cost = play.estimated_cost(state)
    assert 0.03 <= cost <= 0.06
