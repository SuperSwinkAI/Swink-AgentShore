"""Tests for the per-tick guard circuit breaker in run_until_idle."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agentshore.config import RuntimeConfig
from agentshore.core.mixins.loop import _MAX_CONSECUTIVE_TICK_FAILURES


def _orch(tmp_path: Path):
    from tests.orchestrator_factory import make_test_orchestrator

    orch = make_test_orchestrator(tmp_path, RuntimeConfig())
    orch._tick_failure_streak = 0
    orch._natural_exit_reason = None
    orch._drain_reason = None
    orch.begin_drain = AsyncMock()
    return orch


@pytest.mark.asyncio
async def test_tick_failure_backs_off_below_threshold(tmp_path, monkeypatch) -> None:
    import agentshore.core.mixins.loop as loop_mod

    monkeypatch.setattr(loop_mod.asyncio, "sleep", AsyncMock())
    orch = _orch(tmp_path)

    should_break = await orch._handle_tick_failure(ValueError("boom"))

    assert should_break is False
    assert orch._tick_failure_streak == 1
    orch.begin_drain.assert_not_awaited()


@pytest.mark.asyncio
async def test_circuit_breaker_trips_and_drains_at_threshold(tmp_path, monkeypatch) -> None:
    import agentshore.core.mixins.loop as loop_mod

    monkeypatch.setattr(loop_mod.asyncio, "sleep", AsyncMock())
    orch = _orch(tmp_path)

    result = False
    for _ in range(_MAX_CONSECUTIVE_TICK_FAILURES):
        result = await orch._handle_tick_failure(ValueError("boom"))

    assert result is True  # last call trips the breaker
    assert orch._tick_failure_streak == _MAX_CONSECUTIVE_TICK_FAILURES
    orch.begin_drain.assert_awaited_once_with("tick_failure_circuit_breaker")
    assert orch._drain_reason == "tick_failure_circuit_breaker"
    assert orch._natural_exit_reason == "tick_failure_circuit_breaker"
