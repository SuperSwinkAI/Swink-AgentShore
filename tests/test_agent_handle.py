"""Tests for AgentHandle, AgentInvocationResult, TaskRecord, and error aliases."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agentshore.agents.handle import (
    AgentHandle,
    AgentInvocationResult,
    TaskRecord,
    is_noop_invocation,
)
from agentshore.errors import AgentProcessCrashed, AgentProcessError, AgentTimeout, PlayTimeoutError
from agentshore.state import AgentStatus, AgentType, PlayType


def _noop_probe(raw_output: str, exit_code: int) -> AgentInvocationResult:
    return AgentInvocationResult(
        raw_output=raw_output,
        tokens_in=0,
        tokens_out=0,
        dollar_cost=0.0,
        duration_ms=10,
        exit_code=exit_code,
    )


def test_is_noop_invocation_true_for_clean_exit_empty_output() -> None:
    assert is_noop_invocation(_noop_probe("", 0)) is True
    assert is_noop_invocation(_noop_probe("   \n\t ", 0)) is True


def test_is_noop_invocation_false_when_output_present_or_nonzero_exit() -> None:
    # Has output → not a no-op (even if it lacks a JSON block).
    assert is_noop_invocation(_noop_probe("did work", 0)) is False
    # Empty but non-zero exit → a crash/kill handled elsewhere, not a no-op.
    assert is_noop_invocation(_noop_probe("", 1)) is False


def test_invocation_result_is_frozen() -> None:
    result = AgentInvocationResult(
        raw_output="hello",
        tokens_in=10,
        tokens_out=20,
        dollar_cost=0.001,
        duration_ms=150,
        exit_code=0,
    )
    with pytest.raises(FrozenInstanceError):
        result.raw_output = "changed"  # type: ignore[misc]


def test_invocation_result_has_slots() -> None:
    result = AgentInvocationResult(
        raw_output="x", tokens_in=1, tokens_out=2, dollar_cost=0.0, duration_ms=10, exit_code=0
    )
    assert not hasattr(result, "__dict__")


def test_task_record_is_frozen() -> None:
    record = TaskRecord(play_id="p1", play_type=PlayType.ISSUE_PICKUP, success=True, branch="main")
    with pytest.raises(FrozenInstanceError):
        record.success = False  # type: ignore[misc]


def _make_handle(status: AgentStatus = AgentStatus.IDLE) -> AgentHandle:
    return AgentHandle(
        agent_id="agent-1",
        agent_type=AgentType.CLAUDE_CODE,
        status=status,
        working_dir=Path("/tmp"),
    )


def test_handle_initial_status() -> None:
    handle = _make_handle(AgentStatus.IDLE)
    assert handle.status == AgentStatus.IDLE
    assert handle.timeout_count == 0


def test_transition_updates_status() -> None:
    handle = _make_handle()
    handle.transition_to(AgentStatus.BUSY)
    assert handle.status == AgentStatus.BUSY


def test_transition_to_error() -> None:
    handle = _make_handle(AgentStatus.BUSY)
    handle.transition_to(AgentStatus.ERROR)
    assert handle.status == AgentStatus.ERROR


def test_transition_to_terminated() -> None:
    handle = _make_handle()
    handle.transition_to(AgentStatus.TERMINATED)
    assert handle.status == AgentStatus.TERMINATED


def test_transition_updates_last_active() -> None:
    handle = _make_handle()
    assert handle.last_active is None
    handle.transition_to(AgentStatus.BUSY)
    assert handle.last_active is not None


def test_handle_has_slots() -> None:
    handle = _make_handle()
    assert not hasattr(handle, "__dict__")


def test_accumulate_tokens_and_cost() -> None:
    handle = _make_handle()
    handle.accumulate(tokens_in=100, tokens_out=200, dollar_cost=0.05)
    handle.accumulate(tokens_in=50, tokens_out=50, dollar_cost=0.02)
    assert handle.total_tokens == 400
    assert abs(handle.total_cost - 0.07) < 1e-9


def test_add_task_appends_to_history() -> None:
    handle = _make_handle()
    r1 = TaskRecord(play_id="p1", play_type=PlayType.ISSUE_PICKUP, success=True, branch="feat-x")
    r2 = TaskRecord(play_id="p2", play_type=PlayType.CODE_REVIEW, success=False)
    handle.add_task(r1)
    handle.add_task(r2)
    assert len(handle.task_history) == 2
    assert handle.task_history[0].play_id == "p1"
    assert handle.task_history[1].success is False


def test_agent_process_error_is_alias() -> None:
    assert AgentProcessError is AgentProcessCrashed


def test_play_timeout_error_is_subclass_of_agent_timeout() -> None:
    assert issubclass(PlayTimeoutError, AgentTimeout)


def test_play_timeout_error_is_catchable_as_agent_timeout() -> None:
    with pytest.raises(AgentTimeout):
        raise PlayTimeoutError("play timed out after 30s")


def test_capabilities_has_all_agent_types() -> None:
    from agentshore.agents.capabilities import AGENT_CAPABILITIES

    for agent_type in AgentType:
        assert agent_type in AGENT_CAPABILITIES, f"{agent_type} missing from AGENT_CAPABILITIES"
