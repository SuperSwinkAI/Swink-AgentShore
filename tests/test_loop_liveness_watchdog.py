"""Tests for the loop-liveness watchdog (#9).

The orchestrator run loop was observed to hard-freeze mid-tick: no further
plays, no periodic GitHub refresh, and crucially no clean
drain/stop. The idle/unanswered-pause backstops cannot catch this because they
require the loop to keep ticking. The loop-liveness watchdog is an independent
task that stamps a heartbeat each loop iteration and force-drains/stops the
session if that heartbeat goes stale past
``feedback.loop_liveness_timeout_seconds``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

import agentshore.core.mixins.loop as loop_mod
from agentshore.config import FeedbackConfig, RuntimeConfig


def _make_orch(feedback: FeedbackConfig | None = None) -> Any:
    from agentshore.core import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._cfg = RuntimeConfig(feedback=feedback or FeedbackConfig())
    orch._session_id = "test-session"
    orch._stop_requested = False
    orch._stopped = False
    orch._draining = False
    orch._drain_reason = None
    orch._in_flight = {}
    orch._loop_liveness_task = None
    orch._last_loop_iteration_at = 0.0
    # begin_drain / stop are driven by the watchdog; stub them so the unit test
    # observes the calls without running the full teardown body.
    orch.begin_drain = AsyncMock(name="begin_drain")
    orch.stop = AsyncMock(name="stop")

    async def _safe_call(coro: Any, _label: str) -> None:
        await coro

    orch._safe_call = _safe_call
    return orch


def test_disabled_timeout_does_not_start_task() -> None:
    orch = _make_orch(FeedbackConfig(loop_liveness_timeout_seconds=None))
    assert orch._loop_liveness_timeout_seconds() is None


@pytest.mark.asyncio
async def test_stale_heartbeat_force_drains_and_stops(monkeypatch: Any) -> None:
    """A heartbeat older than the timeout triggers drain + stop within ~one interval."""
    orch = _make_orch(FeedbackConfig(loop_liveness_timeout_seconds=1.0))
    # Heartbeat stamped well in the past → stale immediately.
    orch._last_loop_iteration_at = time.monotonic() - 100.0
    # Shrink the check interval so the watchdog reacts fast in-test.
    monkeypatch.setattr(loop_mod, "_LOOP_LIVENESS_CHECK_INTERVAL_SECONDS", 0.01)

    await asyncio.wait_for(orch._loop_liveness_watchdog(), timeout=2.0)

    orch.begin_drain.assert_awaited_once_with("loop_liveness_timeout")
    orch.stop.assert_awaited_once()
    assert orch._drain_reason == "loop_liveness_timeout"


@pytest.mark.asyncio
async def test_fresh_heartbeat_does_not_fire(monkeypatch: Any) -> None:
    """A loop that keeps advancing its heartbeat is never reaped."""
    orch = _make_orch(FeedbackConfig(loop_liveness_timeout_seconds=10.0))
    monkeypatch.setattr(loop_mod, "_LOOP_LIVENESS_CHECK_INTERVAL_SECONDS", 0.01)

    async def _advance_heartbeat() -> None:
        # Keep the heartbeat fresh for several check cycles, then stop the loop.
        for _ in range(5):
            orch._last_loop_iteration_at = time.monotonic()
            await asyncio.sleep(0.01)
        orch._stop_requested = True

    advancer = asyncio.create_task(_advance_heartbeat())
    await asyncio.wait_for(orch._loop_liveness_watchdog(), timeout=2.0)
    await advancer

    orch.begin_drain.assert_not_awaited()
    orch.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_unarmed_heartbeat_does_not_fire(monkeypatch: Any) -> None:
    """A loop that has not started iterating (heartbeat 0.0) is not stale."""
    orch = _make_orch(FeedbackConfig(loop_liveness_timeout_seconds=1.0))
    orch._last_loop_iteration_at = 0.0
    monkeypatch.setattr(loop_mod, "_LOOP_LIVENESS_CHECK_INTERVAL_SECONDS", 0.01)

    async def _stop_soon() -> None:
        await asyncio.sleep(0.05)
        orch._stop_requested = True

    stopper = asyncio.create_task(_stop_soon())
    await asyncio.wait_for(orch._loop_liveness_watchdog(), timeout=2.0)
    await stopper

    orch.begin_drain.assert_not_awaited()
    orch.stop.assert_not_awaited()


def test_start_is_idempotent_and_respects_disable() -> None:
    """start_loop_liveness_watchdog is a no-op when disabled and idempotent."""
    disabled = _make_orch(FeedbackConfig(loop_liveness_timeout_seconds=None))
    disabled.start_loop_liveness_watchdog()
    assert disabled._loop_liveness_task is None


@pytest.mark.asyncio
async def test_run_until_idle_stamps_heartbeat() -> None:
    """The loop stamps the heartbeat on entry so the watchdog has a baseline."""
    orch = _make_orch(FeedbackConfig(loop_liveness_timeout_seconds=600.0))
    # Force an immediate exit: stop requested before the first body iteration.
    orch._stop_requested = True
    orch._natural_exit_reason = None
    orch._natural_exit_callback = None
    before = time.monotonic()
    await orch.run_until_idle()
    assert orch._last_loop_iteration_at >= before
