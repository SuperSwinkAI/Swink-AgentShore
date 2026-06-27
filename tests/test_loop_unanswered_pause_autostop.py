"""Tests for the unanswered-feedback-pause auto-stop backstop (#9).

A loop-detection (or other automated-escalation) pause that nobody answers must
auto-stop after ``feedback.unanswered_timeout_seconds`` instead of wedging the
loop indefinitely. Explicit user/ipc pauses are NOT subject to the timeout.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agentshore.config import FeedbackConfig, RuntimeConfig


def _make_orch(tmp_path: Path, feedback: FeedbackConfig | None = None) -> Any:
    from tests.orchestrator_factory import make_test_orchestrator

    orch = make_test_orchestrator(tmp_path, RuntimeConfig(feedback=feedback or FeedbackConfig()))
    orch._session_id = "test-session"
    orch._loop._auto_stop_reprieves_used = 0
    return orch


@pytest.mark.asyncio
async def test_loop_detected_pause_arms_deadline(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path, FeedbackConfig(unanswered_timeout_seconds=120.0))
    before = time.monotonic()
    await orch.pause("loop_detected")
    assert orch._pause_deadline is not None
    assert orch._pause_deadline >= before + 119.0


@pytest.mark.asyncio
async def test_user_request_pause_does_not_arm_deadline(tmp_path: Path) -> None:
    """An explicit operator pause must not auto-stop — they are present."""
    orch = _make_orch(tmp_path, FeedbackConfig(unanswered_timeout_seconds=120.0))
    await orch.pause("user_request")
    assert orch._pause_deadline is None


@pytest.mark.asyncio
async def test_timeout_none_disables_backstop(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path, FeedbackConfig(unanswered_timeout_seconds=None))
    await orch.pause("loop_detected")
    assert orch._pause_deadline is None


@pytest.mark.asyncio
async def test_resume_clears_deadline(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path, FeedbackConfig(unanswered_timeout_seconds=120.0))
    await orch.pause("loop_detected")
    assert orch._pause_deadline is not None
    await orch.resume()
    assert orch._pause_deadline is None
    assert orch._pause_event.is_set()


@pytest.mark.asyncio
async def test_auto_stop_unanswered_pause_drains_and_unblocks(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path, FeedbackConfig(unanswered_timeout_seconds=120.0))
    # No actionable work → the guard does not defer; the pause auto-stops.
    orch._loop.actionable_work_remains = AsyncMock(return_value=(False, 0, 0))
    await orch.pause("loop_detected")
    orch._pause_event.clear()  # still paused
    await orch._loop.auto_stop_unanswered_pause()
    assert orch._draining is True
    assert orch._drain_reason == "loop_detection_prompt_timeout"
    assert orch._pause_event.is_set()  # gate unblocked so loop reaches drain
    assert orch._pause_deadline is None


@pytest.mark.asyncio
async def test_auto_stop_always_drains_even_with_work(tmp_path: Path) -> None:
    """The work/progress reprieve is gone: an unanswered pause always drains.

    Autonomous no-progress stops are now handled directly by the forward-progress
    monitor (``_check_no_forward_progress`` → ``begin_drain``). This #9 path only
    covers genuine operator/feedback pauses, which auto-stop once the deadline
    passes regardless of remaining work.
    """
    orch = _make_orch(tmp_path, FeedbackConfig(unanswered_timeout_seconds=120.0))
    # Even with actionable work present, the simplified path drains.
    orch._loop.actionable_work_remains = AsyncMock(return_value=(True, 2, 0))
    await orch.pause("loop_detected")
    orch._pause_event.clear()
    await orch._loop.auto_stop_unanswered_pause()
    assert orch._draining is True
    assert orch._drain_reason == "loop_detection_prompt_timeout"
    assert orch._pause_event.is_set()
    assert orch._pause_deadline is None
