"""Tests for explicit orchestrator session lifecycle transitions."""

from __future__ import annotations

from agentshore.core.session_runtime import LifecyclePhase, SessionRuntime


def test_lifecycle_progresses_through_drain_and_stop_without_contradictory_flags() -> None:
    runtime = SessionRuntime()

    assert runtime.lifecycle.phase is LifecyclePhase.RUNNING
    assert runtime.lifecycle.begin_drain("budget_reserve_reached") is True
    assert runtime.draining is True
    assert runtime.drain_initialized is True
    assert runtime.stop_requested is False

    runtime.lifecycle.request_stop("operator_stop")

    assert runtime.lifecycle.phase is LifecyclePhase.STOP_REQUESTED
    assert runtime.stop_requested is True
    assert runtime.draining is False
    assert runtime.stopped is False

    assert runtime.lifecycle.begin_stop() is True
    assert runtime.lifecycle.phase is LifecyclePhase.STOPPING
    assert runtime.stopped is True
    assert runtime.lifecycle.begin_stop() is False

    runtime.lifecycle.mark_stopped()
    assert runtime.lifecycle.phase is LifecyclePhase.STOPPED
    assert runtime.lifecycle.begin_drain("too_late") is False


def test_budget_drain_can_resume_to_running() -> None:
    runtime = SessionRuntime()
    assert runtime.lifecycle.begin_drain("time_budget_reserve_reached") is True

    assert runtime.lifecycle.resume_from_drain() is True

    assert runtime.lifecycle.phase is LifecyclePhase.RUNNING
    assert runtime.draining is False
    assert runtime.drain_initialized is False
    assert runtime.drain_reason is None
    assert runtime.stop_reason == ""


def test_compatibility_flags_delegate_to_the_explicit_phase() -> None:
    runtime = SessionRuntime()

    runtime.draining = True
    runtime.drain_initialized = True
    runtime.stop_requested = True

    assert runtime.lifecycle.phase is LifecyclePhase.STOP_REQUESTED
    assert runtime.draining is False
    assert runtime.stop_requested is True
    assert runtime.drain_initialized is True
