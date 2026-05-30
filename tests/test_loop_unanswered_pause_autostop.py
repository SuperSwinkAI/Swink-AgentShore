"""Tests for the unanswered-feedback-pause auto-stop backstop (#9).

A loop-detection (or other automated-escalation) pause that nobody answers must
auto-stop after ``feedback.unanswered_timeout_seconds`` instead of wedging the
loop indefinitely. Explicit user/ipc pauses are NOT subject to the timeout.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentshore.config import FeedbackConfig, RuntimeConfig
from agentshore.state import NullStateProvider


def _make_orch(tmp_path: Path, feedback: FeedbackConfig | None = None) -> Any:
    from agentshore.core import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._cfg = RuntimeConfig(feedback=feedback or FeedbackConfig())
    orch._session_id = "test-session"
    orch._store = AsyncMock()
    orch._state_provider = NullStateProvider()
    orch._selector = MagicMock()
    orch._pause_event = asyncio.Event()
    orch._pause_event.set()
    orch._pause_reason = None
    orch._pause_deadline = None
    orch._last_play_id = None
    orch._draining = False
    orch._drain_reason = None
    orch._budget_override = False
    orch._feedback_cadence_plays_since_ack = 0
    orch._feedback_cadence_last_ack_monotonic = 0.0
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
    await orch.pause("loop_detected")
    orch._pause_event.clear()  # simulate still-paused
    await orch._auto_stop_unanswered_pause()
    assert orch._draining is True
    assert orch._drain_reason == "loop_detection_prompt_timeout"
    assert orch._pause_event.is_set()  # gate unblocked so loop reaches drain
    assert orch._pause_deadline is None
