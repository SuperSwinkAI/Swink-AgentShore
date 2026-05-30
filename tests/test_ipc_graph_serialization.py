"""Tests for IPC serialization of OrchestratorState.graph (Track 8).

Covers:
- state.graph = None serializes to {"graph": null}
- state.graph with epics serializes the full nested structure
- Serialized shape matches the TypeScript ProjectGraph / EpicStatus interface
"""

from __future__ import annotations

import json

from agentshore.beads import BeadStatus, EpicStatus, GraphTask, ProjectGraph
from agentshore.ipc.serializer import make_message, serialize_state
from agentshore.state import IssueSnapshot, OrchestratorState, SessionState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_state(**overrides: object) -> OrchestratorState:
    """Build the smallest valid OrchestratorState for testing."""
    defaults: dict[str, object] = {
        "session_id": "s-graph-test",
        "session_state": SessionState.RUNNING,
        "total_plays": 0,
        "total_cost": 0.0,
        "agents": [],
        "open_issues": [],
        "budget": None,
        "trajectory": None,
        "active_play": None,
        "same_type_failure_streak": 0,
    }
    defaults.update(overrides)
    return OrchestratorState(**defaults)  # type: ignore[arg-type]


def _sample_graph() -> ProjectGraph:
    return ProjectGraph(
        epics=[
            EpicStatus(
                bead_id="bd-001",
                title="Auth & Login",
                total_tasks=8,
                closed_tasks=5,
                closure_ratio=0.625,
            ),
            EpicStatus(
                bead_id="bd-002",
                title="Dashboard",
                total_tasks=4,
                closed_tasks=0,
                closure_ratio=0.0,
            ),
        ],
        tasks=[
            GraphTask(
                bead_id="task-1",
                title="Wire login issue",
                status=BeadStatus.OPEN,
                parent_id="bd-001",
                epic_id="bd-001",
                epic_title="Auth & Login",
                external_ref="gh-42",
                issue_number=42,
                ready=True,
            )
        ],
        tasks_ready=4,
        tasks_total=12,
        global_closure_ratio=5 / 12,
    )


# ---------------------------------------------------------------------------
# graph = None
# ---------------------------------------------------------------------------


def test_serialize_state_graph_none_key_is_present() -> None:
    """When graph is None, the 'graph' key must be present and null."""
    state = _minimal_state(graph=None)
    result = serialize_state(state)
    assert "graph" in result
    assert result["graph"] is None


def test_serialize_state_graph_none_is_json_null() -> None:
    """graph=None round-trips through JSON as null."""
    state = _minimal_state(graph=None)
    msg = make_message("state_update", serialize_state(state))
    parsed = json.loads(msg)
    assert parsed["payload"]["graph"] is None


# ---------------------------------------------------------------------------
# graph with epics
# ---------------------------------------------------------------------------


def test_serialize_state_graph_top_level_keys() -> None:
    """Serialized graph has the four required top-level keys."""
    state = _minimal_state(graph=_sample_graph())
    result = serialize_state(state)
    graph = result["graph"]
    assert isinstance(graph, dict)
    for key in ("epics", "tasks", "tasks_ready", "tasks_total", "global_closure_ratio"):
        assert key in graph, f"Missing top-level graph key: {key}"


def test_serialize_state_graph_scalar_values() -> None:
    """tasks_ready, tasks_total, and global_closure_ratio have correct values."""
    state = _minimal_state(graph=_sample_graph())
    result = serialize_state(state)
    graph = result["graph"]
    assert graph["tasks_ready"] == 4  # type: ignore[index]
    assert graph["tasks_total"] == 12  # type: ignore[index]
    assert abs(graph["global_closure_ratio"] - 5 / 12) < 1e-9  # type: ignore[index,operator]


def test_serialize_state_graph_epics_is_list() -> None:
    """graph.epics is a list with one entry per epic."""
    state = _minimal_state(graph=_sample_graph())
    result = serialize_state(state)
    epics = result["graph"]["epics"]  # type: ignore[index]
    assert isinstance(epics, list)
    assert len(epics) == 2  # type: ignore[arg-type]


def test_serialize_state_graph_epic_keys() -> None:
    """Each serialized epic has the five required keys matching the TS interface."""
    state = _minimal_state(graph=_sample_graph())
    result = serialize_state(state)
    for epic in result["graph"]["epics"]:  # type: ignore[index]
        for key in ("bead_id", "title", "total_tasks", "closed_tasks", "closure_ratio"):
            assert key in epic, f"Epic missing key: {key}"


def test_serialize_state_graph_epic_values() -> None:
    """First epic values round-trip correctly."""
    state = _minimal_state(graph=_sample_graph())
    result = serialize_state(state)
    first = result["graph"]["epics"][0]  # type: ignore[index]
    assert first["bead_id"] == "bd-001"
    assert first["title"] == "Auth & Login"
    assert first["total_tasks"] == 8
    assert first["closed_tasks"] == 5
    assert abs(first["closure_ratio"] - 0.625) < 1e-9


def test_serialize_state_graph_epic_zero_closure_ratio() -> None:
    """An epic with no closed tasks has closure_ratio 0.0."""
    state = _minimal_state(graph=_sample_graph())
    result = serialize_state(state)
    second = result["graph"]["epics"][1]  # type: ignore[index]
    assert second["bead_id"] == "bd-002"
    assert second["closed_tasks"] == 0
    assert second["closure_ratio"] == 0.0


def test_serialize_state_graph_empty_epics_list() -> None:
    """A ProjectGraph with no epics serializes to an empty epics list."""
    graph = ProjectGraph(epics=[], tasks_ready=0, tasks_total=0, global_closure_ratio=0.0)
    state = _minimal_state(graph=graph)
    result = serialize_state(state)
    assert result["graph"]["epics"] == []  # type: ignore[index]


def test_serialize_state_graph_task_keys() -> None:
    """Each serialized graph task has the dashboard linkage keys."""
    state = _minimal_state(graph=_sample_graph())
    result = serialize_state(state)
    task = result["graph"]["tasks"][0]  # type: ignore[index]
    expected = {
        "bead_id",
        "title",
        "status",
        "parent_id",
        "epic_id",
        "epic_title",
        "external_ref",
        "issue_number",
        "ready",
        "closed_at",
        "updated_at",
    }
    assert expected <= set(task)
    assert task["issue_number"] == 42
    assert task["ready"] is True


def test_serialize_state_graph_task_timestamps() -> None:
    """Graph task timestamps are serialized for kanban recency logic."""
    graph = ProjectGraph(
        epics=[],
        tasks=[
            GraphTask(
                bead_id="task-2",
                title="Closed task",
                status=BeadStatus.CLOSED,
                closed_at="2026-05-15T10:00:00Z",
                updated_at="2026-05-15T10:05:00Z",
            )
        ],
        tasks_ready=0,
        tasks_total=1,
        global_closure_ratio=1.0,
    )
    state = _minimal_state(graph=graph)
    result = serialize_state(state)
    task = result["graph"]["tasks"][0]  # type: ignore[index]
    assert task["closed_at"] == "2026-05-15T10:00:00Z"
    assert task["updated_at"] == "2026-05-15T10:05:00Z"


# ---------------------------------------------------------------------------
# JSON round-trip and shape
# ---------------------------------------------------------------------------


def test_serialize_state_graph_json_round_trip() -> None:
    """Full state with graph serializes to valid JSON and back."""
    state = _minimal_state(graph=_sample_graph())
    msg = make_message("state_update", serialize_state(state))
    parsed = json.loads(msg)
    graph = parsed["payload"]["graph"]
    assert graph is not None
    assert len(graph["epics"]) == 2
    assert graph["tasks_ready"] == 4


def test_serialize_state_graph_types_match_ts_interface() -> None:
    """Keys have the types expected by the TypeScript ProjectGraph interface."""
    state = _minimal_state(graph=_sample_graph())
    result = serialize_state(state)
    graph = result["graph"]

    assert isinstance(graph["epics"], list)  # type: ignore[index]
    assert isinstance(graph["tasks"], list)  # type: ignore[index]
    assert isinstance(graph["tasks_ready"], int)  # type: ignore[index]
    assert isinstance(graph["tasks_total"], int)  # type: ignore[index]
    assert isinstance(graph["global_closure_ratio"], float)  # type: ignore[index]

    for epic in graph["epics"]:  # type: ignore[index]
        assert isinstance(epic["bead_id"], str)
        assert isinstance(epic["title"], str)
        assert isinstance(epic["total_tasks"], int)
        assert isinstance(epic["closed_tasks"], int)
        assert isinstance(epic["closure_ratio"], float)


def test_goal_clusters_key_removed() -> None:
    """v0.10.0: goal_clusters key is no longer emitted by the serializer."""
    state = _minimal_state(graph=_sample_graph())
    result = serialize_state(state)
    assert "goal_clusters" not in result


def test_serialize_state_issue_bead_linkage() -> None:
    """Issue snapshots expose their beads mirror linkage to dashboard clients."""
    issue = IssueSnapshot(
        issue_number=42,
        title="Wire login issue",
        state="open",
        priority=1,
        labels=["backend"],
        source="github",
        url="https://github.com/example/repo/issues/42",
        created_at="2026-01-01T00:00:00+00:00",
        closed_at=None,
        bead_id="task-1",
        bead_epic_id="bd-001",
        bead_epic_title="Auth & Login",
        bead_status="open",
        bead_ready=True,
        bead_mirror_status="mirrored",
    )
    state = _minimal_state(open_issues=[issue])
    result = serialize_state(state)
    serialized = result["open_issues"][0]  # type: ignore[index]
    assert serialized["bead_id"] == "task-1"
    assert serialized["url"] == "https://github.com/example/repo/issues/42"
    assert serialized["created_at"] == "2026-01-01T00:00:00+00:00"
    assert serialized["closed_at"] is None
    assert serialized["bead_epic_id"] == "bd-001"
    assert serialized["bead_ready"] is True
    assert serialized["bead_mirror_status"] == "mirrored"
