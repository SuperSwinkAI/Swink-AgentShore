"""Tests for the bounded graceful-drain deadline (#180 residual).

The HARD-stop path already bounds its in-flight wait by the shutdown grace
period. The GRACEFUL drain — ``begin_drain``, which only dispatches
``end_agent`` and waits for in-flight plays to finish — previously waited
UNBOUNDED, so a single in-flight play stuck for hours (a 3600s issue-pickup
hang, or a never-finalizing broken-worktree play) made ``agentshore stop`` hang
~1h until SIGINT.

An independent watchdog (mirroring the loop-liveness watchdog, #9) now records a
monotonic deadline at drain start and escalates to the bounded hard stop once
``feedback.graceful_drain_timeout_seconds`` elapses with in-flight plays still
outstanding.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import agentshore.core.mixins.drain as drain_mod
from agentshore.config import FeedbackConfig, RuntimeConfig


def _make_orch(tmp_path: Path, feedback: FeedbackConfig | None = None) -> Any:
    from tests.orchestrator_factory import make_test_orchestrator

    orch = make_test_orchestrator(tmp_path, RuntimeConfig(feedback=feedback or FeedbackConfig()))
    orch._session_id = "test-session"
    # The watchdog drives the bounded teardown via ``self.stop()``; stub it so
    # the unit test observes the escalation without running the full teardown.
    orch._drain.stop = AsyncMock(name="stop")  # type: ignore[method-assign]
    return orch


def test_disabled_timeout_does_not_start_task(tmp_path: Path) -> None:
    """None deadline ⇒ unbounded graceful drain ⇒ no watchdog task."""
    orch = _make_orch(tmp_path, FeedbackConfig(graceful_drain_timeout_seconds=None))
    assert orch._drain.graceful_drain_timeout_seconds() is None
    orch._drain._start_graceful_drain_watchdog()
    assert orch._drain._graceful_drain_watchdog_task is None


@pytest.mark.asyncio
async def test_stuck_in_flight_escalates_to_hard_stop(tmp_path: Path, monkeypatch: Any) -> None:
    """A graceful drain whose in-flight play never finishes escalates within the deadline.

    Without the deadline this would hang for ~1h (the #180 bug). With it, the
    watchdog logs ``graceful_drain_deadline_escalation`` and calls the bounded
    ``stop`` instead of waiting forever.
    """
    orch = _make_orch(tmp_path, FeedbackConfig(graceful_drain_timeout_seconds=0.05))
    # Shrink the poll interval so the watchdog reacts fast in-test.
    monkeypatch.setattr(drain_mod, "_GRACEFUL_DRAIN_CHECK_INTERVAL_SECONDS", 0.01)

    # A fake in-flight play that never finishes — the wedge signature.
    async def _never_finishes() -> None:
        await asyncio.sleep(3600)

    stuck = asyncio.create_task(_never_finishes())
    orch._runtime.in_flight = {"play-1": stuck}
    # Simulate an active graceful drain (begin_drain already ran).
    orch._runtime.draining = True

    try:
        await asyncio.wait_for(orch._drain._graceful_drain_watchdog(), timeout=2.0)
    finally:
        stuck.cancel()

    orch._drain.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_begin_drain_arms_the_watchdog(tmp_path: Path) -> None:
    """begin_drain launches the bounded-drain watchdog task."""
    orch = _make_orch(tmp_path, FeedbackConfig(graceful_drain_timeout_seconds=300.0))

    await orch._drain.begin_drain("signal_sigterm")
    try:
        task = orch._drain._graceful_drain_watchdog_task
        assert task is not None
        assert not task.done()
    finally:
        orch._drain._stop_graceful_drain_watchdog()


@pytest.mark.asyncio
async def test_completed_drain_does_not_escalate(tmp_path: Path, monkeypatch: Any) -> None:
    """A drain that completes (draining cleared) before the deadline is not reaped."""
    orch = _make_orch(tmp_path, FeedbackConfig(graceful_drain_timeout_seconds=10.0))
    monkeypatch.setattr(drain_mod, "_GRACEFUL_DRAIN_CHECK_INTERVAL_SECONDS", 0.01)
    orch._runtime.draining = True
    orch._runtime.in_flight = {}

    async def _complete_soon() -> None:
        # Drain finishes (all agents ended) well before the deadline.
        await asyncio.sleep(0.03)
        orch._runtime.draining = False

    completer = asyncio.create_task(_complete_soon())
    await asyncio.wait_for(orch._drain._graceful_drain_watchdog(), timeout=2.0)
    await completer

    orch._drain.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_stopping_does_not_escalate(tmp_path: Path, monkeypatch: Any) -> None:
    """The watchdog exits without re-entering stop once a stop is already underway."""
    orch = _make_orch(tmp_path, FeedbackConfig(graceful_drain_timeout_seconds=0.05))
    monkeypatch.setattr(drain_mod, "_GRACEFUL_DRAIN_CHECK_INTERVAL_SECONDS", 0.01)
    orch._runtime.draining = True
    orch._runtime.stopped = True  # a stop body is already running

    await asyncio.wait_for(orch._drain._graceful_drain_watchdog(), timeout=2.0)

    orch._drain.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_escalation_during_completion_processing_runs_teardown(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Escalation while a play completion is in-flight must still reach ``do_stop``.

    Regression for the self-cancel wedge: the watchdog escalation calls
    ``self.stop()`` from within the watchdog task itself, and ``stop()`` cancels
    the bounded-drain watchdog as its first act. When a completion is being
    processed (``completion_processing_count > 0``), ``stop()`` suspends on the
    *unshielded* completion gate before reaching ``do_stop`` — and the queued
    self-cancellation lands there, aborting teardown so the session wedges
    half-stopped forever (in-flight never cancelled, ``stop_done`` never set).

    With the fix, ``do_stop`` always runs: the watchdog no longer self-cancels,
    and the gate await is wrapped so teardown runs even if it is cancelled.
    """
    from tests.orchestrator_factory import make_test_orchestrator

    orch = make_test_orchestrator(
        tmp_path, RuntimeConfig(feedback=FeedbackConfig(graceful_drain_timeout_seconds=0.02))
    )
    orch._session_id = "test-session"
    monkeypatch.setattr(drain_mod, "_GRACEFUL_DRAIN_CHECK_INTERVAL_SECONDS", 0.01)

    # Observe that real ``stop()`` reaches teardown without running the heavy
    # ``stop_inner`` machinery: the bug aborts ``stop()`` *before* ``do_stop``.
    do_stop_called = asyncio.Event()

    async def _record_do_stop(_grace: float) -> None:
        do_stop_called.set()
        orch._runtime.stop_done.set()

    orch._drain.do_stop = _record_do_stop  # type: ignore[method-assign]

    # A play completion is mid-flight: force the completion gate so ``stop()``
    # suspends there (idle cleared ⇒ a genuine yield where the self-cancel lands).
    orch._runtime.completion_processing_count = 1
    orch._runtime.completion_processing_idle.clear()

    async def _never_finishes() -> None:
        await asyncio.sleep(3600)

    stuck = asyncio.create_task(_never_finishes())
    orch._runtime.in_flight = {"play-1": stuck}

    # The completion finishes shortly AFTER the deadline escalates (~0.02s), so
    # ``stop()`` is genuinely suspended on the gate when the self-cancel fires.
    async def _finish_completion() -> None:
        await asyncio.sleep(0.1)
        orch._runtime.completion_processing_count = 0
        orch._runtime.completion_processing_idle.set()

    finisher = asyncio.create_task(_finish_completion())

    try:
        await orch._drain.begin_drain("time_budget_reserve_reached")
        # The buggy path aborts stop() before do_stop, so this wait times out.
        await asyncio.wait_for(do_stop_called.wait(), timeout=2.0)
    finally:
        stuck.cancel()
        finisher.cancel()
        orch._drain._stop_graceful_drain_watchdog()

    assert do_stop_called.is_set()
    assert orch._runtime.stop_done.is_set()


def test_start_is_idempotent(tmp_path: Path) -> None:
    """_start_graceful_drain_watchdog does not spawn a second live task."""
    orch = _make_orch(tmp_path, FeedbackConfig(graceful_drain_timeout_seconds=300.0))

    async def _run() -> None:
        orch._drain._start_graceful_drain_watchdog()
        first = orch._drain._graceful_drain_watchdog_task
        orch._drain._start_graceful_drain_watchdog()
        try:
            assert orch._drain._graceful_drain_watchdog_task is first
        finally:
            orch._drain._stop_graceful_drain_watchdog()

    asyncio.run(_run())
