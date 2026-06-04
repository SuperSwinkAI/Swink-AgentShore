"""Tests for AgentSnapshot.tasks_completed/tasks_failed wiring.

Repurposed from dead-code (formerly fed by AgentHandle.task_history which was
never written) to count plays-per-agent in the current session, derived from
play_history at state-build time.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from agentshore.agents.handle import AgentHandle
from agentshore.core import Orchestrator
from agentshore.core.mixins.snapshots import SnapshotProjector
from agentshore.data.models import PlayRecord
from agentshore.state import AgentStatus, AgentType


def _handle(agent_id: str) -> AgentHandle:
    return AgentHandle(
        agent_id=agent_id,
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.IDLE,
        working_dir=Path("/tmp"),
    )


def _play(agent_id: str | None, success: bool) -> PlayRecord:
    return PlayRecord(
        session_id="test",
        play_type="issue_pickup",
        started_at="2026-05-07T11:00:00Z",
        success=success,
        agent_id=agent_id,
    )


def _orch_with_handles(handles: list[AgentHandle]) -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    manager = MagicMock()
    manager.handles = {h.agent_id: h for h in handles}
    orch._manager = manager
    orch._snapshots = SnapshotProjector(manager=manager, store=MagicMock(), session_id="test")
    return orch


def test_tasks_completed_counts_successful_plays():
    orch = _orch_with_handles([_handle("a"), _handle("b")])
    history = [
        _play("a", True),
        _play("a", True),
        _play("a", True),
        _play("b", True),
    ]
    snaps = {s.agent_id: s for s in orch._snapshots.build_agent_snapshots(history)}
    assert snaps["a"].tasks_completed == 3
    assert snaps["b"].tasks_completed == 1


def test_tasks_failed_counts_failed_plays():
    orch = _orch_with_handles([_handle("a")])
    history = [
        _play("a", True),
        _play("a", False),
        _play("a", False),
    ]
    snaps = {s.agent_id: s for s in orch._snapshots.build_agent_snapshots(history)}
    assert snaps["a"].tasks_completed == 1
    assert snaps["a"].tasks_failed == 2


def test_unknown_agent_id_in_history_is_ignored():
    """play_history can reference agents already terminated. Don't crash."""
    orch = _orch_with_handles([_handle("a")])
    history = [
        _play("a", True),
        _play("ghost", True),  # not in handles
        _play(None, True),  # internal play (no agent)
    ]
    snaps = {s.agent_id: s for s in orch._snapshots.build_agent_snapshots(history)}
    assert snaps["a"].tasks_completed == 1
    assert "ghost" not in snaps


def test_handles_with_no_history_show_zero():
    orch = _orch_with_handles([_handle("a"), _handle("fresh")])
    history = [_play("a", True)]
    snaps = {s.agent_id: s for s in orch._snapshots.build_agent_snapshots(history)}
    assert snaps["fresh"].tasks_completed == 0
    assert snaps["fresh"].tasks_failed == 0


def test_empty_history_yields_zero_counts():
    orch = _orch_with_handles([_handle("a")])
    snaps = orch._snapshots.build_agent_snapshots([])
    assert snaps[0].tasks_completed == 0
    assert snaps[0].tasks_failed == 0
    assert snaps[0].timeout_count == 0


def test_timeout_count_plumbed_from_handle() -> None:
    handle = _handle("a")
    handle.timeout_count = 5
    orch = _orch_with_handles([handle])
    snaps = {s.agent_id: s for s in orch._snapshots.build_agent_snapshots([])}
    assert snaps["a"].timeout_count == 5
