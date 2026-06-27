"""Barrier that stops a new session.start racing the previous store close (#283).

The orchestrator runs in-process inside the long-lived sidecar. On a natural
session end the ESR "restart" button is unlocked *before* ``orch.stop()`` (which
closes ``agentshore.db`` via an Online-Backup snapshot + ``os.replace`` under the
SQLite writer lock) has finished. A restart fired from that screen would open a
new store concurrently with the close and hit "database is locked".

``stop_orchestrator_tracked`` publishes the in-flight close on
``ServerState.store_teardown_task``; ``_await_prior_store_teardown`` makes a new
session.start wait for it before opening its own store. These tests exercise that
contract directly (no real Orchestrator boot required).
"""

from __future__ import annotations

import asyncio

import pytest

from agentshore.sidecar import session_lifecycle
from agentshore.sidecar.rpc.protocol import ServerState
from agentshore.sidecar.session_lifecycle import (
    SessionStartError,
    _await_prior_store_teardown,
    stop_orchestrator_tracked,
)


class _FakeOrch:
    """Minimal orchestrator stand-in whose ``stop`` we can gate and observe."""

    def __init__(self, *, gate: asyncio.Event | None = None) -> None:
        self._gate = gate
        self.stopped = False

    async def stop(self, grace_period_s: float = 0.0) -> None:
        if self._gate is not None:
            await self._gate.wait()
        self.stopped = True


@pytest.mark.asyncio
async def test_tracked_stop_sets_then_clears_teardown_slot() -> None:
    """The slot holds the in-flight task while stop runs, and self-clears after."""
    state = ServerState()
    gate = asyncio.Event()
    orch = _FakeOrch(gate=gate)

    runner = asyncio.ensure_future(stop_orchestrator_tracked(state, orch))  # type: ignore[arg-type]
    await asyncio.sleep(0)  # let the tracked task register itself
    assert state.store_teardown_task is not None
    assert not state.store_teardown_task.done()

    gate.set()
    await runner
    assert orch.stopped is True
    assert state.store_teardown_task is None  # cleared on completion


@pytest.mark.asyncio
async def test_await_returns_immediately_with_no_teardown() -> None:
    """No in-flight teardown => the start barrier is a no-op."""
    state = ServerState()
    assert state.store_teardown_task is None
    # Should not raise or block.
    await asyncio.wait_for(_await_prior_store_teardown(state), timeout=1.0)


@pytest.mark.asyncio
async def test_start_barrier_waits_for_close_to_finish() -> None:
    """The barrier returns only after the outgoing store close completes (#283)."""
    state = ServerState()
    gate = asyncio.Event()
    orch = _FakeOrch(gate=gate)

    teardown = asyncio.ensure_future(stop_orchestrator_tracked(state, orch))  # type: ignore[arg-type]
    await asyncio.sleep(0)

    barrier = asyncio.ensure_future(_await_prior_store_teardown(state))
    await asyncio.sleep(0)
    # Close is still gated, so the barrier must not have resolved yet.
    assert not barrier.done()
    assert orch.stopped is False

    gate.set()  # let the close finish
    await asyncio.wait_for(barrier, timeout=1.0)
    assert orch.stopped is True  # barrier resolved only after close completed
    await teardown


@pytest.mark.asyncio
async def test_start_barrier_times_out_without_cancelling_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A close that overruns the budget raises SessionStartError but the close
    itself keeps running (shielded) so the DB is left consistent (#283)."""
    monkeypatch.setattr(session_lifecycle, "STORE_TEARDOWN_WAIT_SECONDS", 0.05)
    state = ServerState()
    gate = asyncio.Event()  # never set during the timeout window
    orch = _FakeOrch(gate=gate)

    teardown = asyncio.ensure_future(stop_orchestrator_tracked(state, orch))  # type: ignore[arg-type]
    await asyncio.sleep(0)

    with pytest.raises(SessionStartError, match="still shutting down"):
        await _await_prior_store_teardown(state)

    # The underlying close must NOT have been cancelled by the timeout.
    assert not teardown.done()
    assert state.store_teardown_task is not None

    # Cleanup: let the close finish.
    gate.set()
    await asyncio.wait_for(teardown, timeout=1.0)
    assert orch.stopped is True
