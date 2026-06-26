"""Tests for HealthMonitor — crash detection, context pressure, circuit breaker."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from agentshore.agents.circuit_breaker import CircuitBreaker
from agentshore.agents.handle import AgentHandle
from agentshore.agents.health import HealthMonitor
from agentshore.state import AgentStatus, AgentType


def _make_handle(
    agent_id: str = "a1",
    agent_type: AgentType = AgentType.CLAUDE_CODE,
    status: AgentStatus = AgentStatus.IDLE,
) -> AgentHandle:
    return AgentHandle(
        agent_id=agent_id,
        agent_type=agent_type,
        status=status,
        working_dir=Path("/tmp"),
    )


def _make_monitor(
    handles: dict[str, AgentHandle],
    circuit_breakers: dict[str, CircuitBreaker] | None = None,
    *,
    on_crash: AsyncMock | None = None,
    on_context_pressure: AsyncMock | None = None,
    on_recovery: AsyncMock | None = None,
    max_context: dict[AgentType, int] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> HealthMonitor:
    circuit_breakers = circuit_breakers or {}
    on_crash = on_crash or AsyncMock()
    on_context_pressure = on_context_pressure or AsyncMock()
    return HealthMonitor(
        handles,
        circuit_breakers,
        on_crash=on_crash,
        on_context_pressure=on_context_pressure,
        on_recovery=on_recovery,
        poll_interval=30.0,
        max_context_per_type=max_context,
        sleep_fn=AsyncMock(),  # never actually sleeps in tests
        monotonic_fn=monotonic,
    )


async def test_crash_detected_sets_error_status_and_calls_callback() -> None:
    handle = _make_handle("cli-1", AgentType.CLAUDE_CODE)

    # Process already exited (returncode set).
    fake_proc = MagicMock()
    fake_proc.returncode = 1
    handle.process = fake_proc  # type: ignore[assignment]

    on_crash = AsyncMock()
    mon = _make_monitor({"cli-1": handle}, on_crash=on_crash)

    await mon._poll_all()

    assert handle.status == AgentStatus.ERROR
    on_crash.assert_awaited_once_with("cli-1", 1)


async def test_no_crash_when_process_still_running() -> None:
    handle = _make_handle("cli-1", AgentType.CLAUDE_CODE)

    fake_proc = MagicMock()
    fake_proc.returncode = None  # still running
    handle.process = fake_proc  # type: ignore[assignment]

    on_crash = AsyncMock()
    mon = _make_monitor({"cli-1": handle}, on_crash=on_crash)

    await mon._poll_all()

    assert handle.status == AgentStatus.IDLE  # unchanged
    on_crash.assert_not_awaited()

    assert handle.status == AgentStatus.IDLE


async def test_crash_records_failure_on_circuit_breaker() -> None:
    handle = _make_handle("cli-2", AgentType.CODEX)

    fake_proc = MagicMock()
    fake_proc.returncode = 137  # SIGKILL
    handle.process = fake_proc  # type: ignore[assignment]

    cb = CircuitBreaker(failures=1, window_seconds=300, cooldown_seconds=60)
    assert cb.allows_dispatch  # CLOSED initially

    on_crash = AsyncMock()
    mon = _make_monitor({"cli-2": handle}, {"cli-2": cb}, on_crash=on_crash)

    await mon._poll_all()

    assert not cb.allows_dispatch  # OPEN after 1 failure (threshold=1)


async def test_terminated_agents_skipped() -> None:
    handle = _make_handle("cli-3", AgentType.CLAUDE_CODE, status=AgentStatus.TERMINATED)

    fake_proc = MagicMock()
    fake_proc.returncode = 1
    handle.process = fake_proc  # type: ignore[assignment]

    on_crash = AsyncMock()
    mon = _make_monitor({"cli-3": handle}, on_crash=on_crash)

    await mon._poll_all()

    # TERMINATED agents are skipped entirely
    on_crash.assert_not_awaited()


async def test_context_pressure_fires_at_threshold() -> None:
    handle = _make_handle("cli-4", AgentType.CLAUDE_CODE)
    handle.context_size = 165_000  # 82.5% of 200_000

    on_pressure = AsyncMock()
    mon = _make_monitor(
        {"cli-4": handle},
        on_context_pressure=on_pressure,
        max_context={AgentType.CLAUDE_CODE: 200_000},
    )

    await mon._poll_all()

    on_pressure.assert_awaited_once()
    _, utilisation = on_pressure.call_args.args
    assert utilisation > 0.80


async def test_context_pressure_not_fired_below_threshold() -> None:
    handle = _make_handle("cli-5", AgentType.CLAUDE_CODE)
    handle.context_size = 100_000  # 50% of 200_000

    on_pressure = AsyncMock()
    mon = _make_monitor(
        {"cli-5": handle},
        on_context_pressure=on_pressure,
        max_context={AgentType.CLAUDE_CODE: 200_000},
    )

    await mon._poll_all()

    on_pressure.assert_not_awaited()


async def test_context_pressure_skipped_without_max_context() -> None:
    handle = _make_handle("cli-6", AgentType.CLAUDE_CODE)
    handle.context_size = 999_999

    on_pressure = AsyncMock()
    mon = _make_monitor(
        {"cli-6": handle},
        on_context_pressure=on_pressure,
        # no max_context provided
    )

    await mon._poll_all()

    on_pressure.assert_not_awaited()


async def test_monitor_start_and_stop() -> None:
    handle = _make_handle()
    poll_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        poll_calls.append(s)
        # Cancel after first sleep so we don't loop forever
        raise asyncio.CancelledError

    mon = HealthMonitor(
        {"a1": handle},
        {},
        on_crash=AsyncMock(),
        on_context_pressure=AsyncMock(),
        sleep_fn=fake_sleep,
    )

    mon.start()
    assert mon.is_running

    # Let the task run until it cancels itself
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    mon.stop()
    await asyncio.sleep(0)  # let cancellation propagate

    assert not mon.is_running


async def test_monitor_stop_is_idempotent() -> None:
    handle = _make_handle()
    mon = _make_monitor({"a1": handle})
    # Stopping before starting should not raise
    mon.stop()
    mon.stop()


async def test_error_agent_recovery_triggered() -> None:
    """An agent in ERROR with a HALF_OPEN breaker should trigger the on_recovery callback."""
    from datetime import UTC, datetime

    handle = _make_handle("err-1", AgentType.CLAUDE_CODE, status=AgentStatus.ERROR)

    # Create a breaker that is HALF_OPEN (cooldown elapsed)
    clock: list[datetime] = [datetime(2026, 1, 1, tzinfo=UTC)]
    cb = CircuitBreaker(failures=1, cooldown_seconds=60, now_fn=lambda: clock[0])
    cb.record_failure()  # trips to OPEN
    clock[0] = clock[0] + __import__("datetime").timedelta(seconds=61)
    assert cb.should_attempt_recovery() is True

    on_recovery = AsyncMock()
    mon = _make_monitor(
        {"err-1": handle},
        {"err-1": cb},
        on_recovery=on_recovery,
    )

    await mon._poll_all()

    on_recovery.assert_awaited_once_with("err-1")


async def test_error_agent_recovery_not_triggered_when_breaker_open() -> None:
    """An agent in ERROR with an OPEN breaker should NOT trigger recovery."""
    handle = _make_handle("err-2", AgentType.CLAUDE_CODE, status=AgentStatus.ERROR)

    cb = CircuitBreaker(failures=1, cooldown_seconds=60)
    cb.record_failure()  # trips to OPEN, cooldown not elapsed
    assert cb.is_open

    on_recovery = AsyncMock()
    mon = _make_monitor(
        {"err-2": handle},
        {"err-2": cb},
        on_recovery=on_recovery,
    )

    await mon._poll_all()

    on_recovery.assert_not_awaited()


async def test_error_recovery_skipped_when_no_callback() -> None:
    """When on_recovery is None, error recovery is silently skipped."""
    handle = _make_handle("err-3", AgentType.CLAUDE_CODE, status=AgentStatus.ERROR)

    cb = CircuitBreaker(failures=1, cooldown_seconds=0)
    cb.record_failure()
    # cooldown=0 means it immediately becomes HALF_OPEN

    # No on_recovery callback — should not raise
    mon = _make_monitor({"err-3": handle}, {"err-3": cb})

    await mon._poll_all()  # must not raise


# Busy-watchdog reaps an agent stuck BUSY past its dispatch deadline.
# Regression (session a3202694): a hung SIGKILL pinned an agy agent BUSY for
# hours, suppressing every session-end backstop.


def _busy_handle(
    agent_id: str = "busy-1",
    agent_type: AgentType = AgentType.ANTIGRAVITY,
    *,
    deadline: float | None,
    returncode: int | None = None,
) -> AgentHandle:
    handle = _make_handle(agent_id, agent_type, status=AgentStatus.BUSY)
    handle.dispatch_deadline_monotonic = deadline
    proc = MagicMock()
    proc.returncode = returncode
    proc.pid = 5151
    handle.process = proc  # type: ignore[assignment]
    return handle


async def test_busy_watchdog_reaps_agent_past_deadline() -> None:
    # Agent BUSY with a deadline in the past; process still appears alive.
    handle = _busy_handle(deadline=999.0, returncode=None)
    cb = CircuitBreaker(failures=1, window_seconds=300, cooldown_seconds=60)
    on_crash = AsyncMock()
    mon = _make_monitor(
        {"busy-1": handle},
        {"busy-1": cb},
        on_crash=on_crash,
        monotonic=lambda: 1000.0,  # past the 999.0 deadline
    )

    await mon._poll_all()

    assert handle.status == AgentStatus.ERROR
    on_crash.assert_awaited_once_with("busy-1", -1)  # sentinel rc for a hung dispatch
    assert not cb.allows_dispatch  # circuit breaker recorded the failure
    handle.process.kill.assert_called_once()  # best-effort leak kill
    assert handle.dispatch_deadline_monotonic is None  # cleared after reap


async def test_busy_watchdog_ignores_agent_within_deadline() -> None:
    handle = _busy_handle(deadline=2000.0, returncode=None)
    on_crash = AsyncMock()
    mon = _make_monitor({"busy-1": handle}, on_crash=on_crash, monotonic=lambda: 1000.0)

    await mon._poll_all()

    assert handle.status == AgentStatus.BUSY  # still working, untouched
    on_crash.assert_not_awaited()
    handle.process.kill.assert_not_called()


async def test_busy_watchdog_ignores_idle_agent_with_stale_deadline() -> None:
    # A stale deadline left on an IDLE agent must never trigger a reap.
    handle = _make_handle("idle-1", AgentType.ANTIGRAVITY, status=AgentStatus.IDLE)
    handle.dispatch_deadline_monotonic = 999.0
    on_crash = AsyncMock()
    mon = _make_monitor({"idle-1": handle}, on_crash=on_crash, monotonic=lambda: 1000.0)

    await mon._poll_all()

    assert handle.status == AgentStatus.IDLE
    on_crash.assert_not_awaited()


async def test_busy_watchdog_noop_when_no_deadline_set() -> None:
    handle = _busy_handle(deadline=None, returncode=None)
    on_crash = AsyncMock()
    mon = _make_monitor({"busy-1": handle}, on_crash=on_crash, monotonic=lambda: 1e12)

    await mon._poll_all()

    assert handle.status == AgentStatus.BUSY
    on_crash.assert_not_awaited()


async def test_busy_watchdog_uses_real_returncode_when_process_exited() -> None:
    # Watchdog forwards a real exit code (not the -1 sentinel) when BUSY is still set.
    handle = _busy_handle(deadline=999.0, returncode=137)
    on_crash = AsyncMock()
    # Liveness runs first: ERROR + on_crash(137); watchdog then sees status != BUSY
    # and no-ops. Either way on_crash carries rc=137.
    mon = _make_monitor({"busy-1": handle}, on_crash=on_crash, monotonic=lambda: 1000.0)

    await mon._poll_all()

    assert handle.status == AgentStatus.ERROR
    on_crash.assert_awaited_once_with("busy-1", 137)
