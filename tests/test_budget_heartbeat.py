"""Coverage for the dashboard budget-countdown heartbeat.

The orchestrator emits a lightweight, budget-only ``budget_update`` frame on a
fixed cadence so the dashboard's remaining-time figure keeps ticking down during
quiet stretches (idle fleet, or one long-running play) when no full state update
fires. It is deliberately budget-only so the office sprites never re-process and
jitter on these frequent frames.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentshore.config.models import BudgetConfig
from agentshore.core.mixins.loop import LoopRunner
from agentshore.core.mixins.snapshots import SnapshotProjector
from agentshore.core.mixins.state import StateBuilder
from agentshore.ipc.provider import IpcStateProvider
from agentshore.ipc.serializer import make_message, serialize_budget_update
from agentshore.state import NullStateProvider


def _projector() -> SnapshotProjector:
    return SnapshotProjector(manager=MagicMock(), store=MagicMock(), session_id="hb")


def _budget_snapshot(*, time_total: int = 60, elapsed: float = 15.0) -> Any:
    cfg = BudgetConfig(time_enabled=True, time_total_minutes=time_total)
    return _projector().build_budget_snapshot(3, 1.25, budget_cfg=cfg, elapsed_minutes=elapsed)


def test_serialize_budget_update_wraps_budget_with_time_fields() -> None:
    snap = _budget_snapshot(time_total=60, elapsed=15.0)
    payload = serialize_budget_update(snap)
    assert set(payload) == {"budget"}
    budget = payload["budget"]
    assert isinstance(budget, dict)
    assert budget["time_remaining_minutes"] == pytest.approx(45.0)
    assert budget["time_enabled"] is True


@pytest.mark.asyncio
async def test_provider_emits_budget_update_event_not_state() -> None:
    """on_budget_update appends a budget-only event; it never replaces the
    cached full state (a reconnecting client must still get the last snapshot)."""
    writer = MagicMock()
    writer.append_event = AsyncMock()
    writer.write_state = AsyncMock()
    server = MagicMock()
    provider = IpcStateProvider(writer, server, session_id="sess-7")

    await provider.on_budget_update(_budget_snapshot())

    writer.write_state.assert_not_awaited()
    server.set_cached_state.assert_not_called()
    writer.append_event.assert_awaited_once()
    # Wire envelope nests under "payload"; the dashboard client (ws.ts) flattens it.
    msg = json.loads(writer.append_event.await_args.args[0])
    assert msg["type"] == "budget_update"
    assert msg["payload"]["session_id"] == "sess-7"
    assert msg["payload"]["budget"]["time_remaining_minutes"] == pytest.approx(45.0)


@pytest.mark.asyncio
async def test_null_provider_budget_update_is_noop() -> None:
    await NullStateProvider().on_budget_update(_budget_snapshot())


def test_make_message_budget_update_roundtrips() -> None:
    raw = make_message("budget_update", serialize_budget_update(_budget_snapshot()))
    msg = json.loads(raw)
    assert msg["type"] == "budget_update"
    assert "budget" in msg["payload"]


# StateBuilder.current_budget_snapshot — recompute from cache + fresh clock


def _state_builder_stub(
    *, inputs: tuple[int, float] | None, time_enabled: bool, loop_started_at: float
) -> SimpleNamespace:
    cfg = BudgetConfig(enabled=True, total=200.0, time_enabled=time_enabled, time_total_minutes=120)
    return SimpleNamespace(
        _last_budget_inputs=inputs,
        _host=SimpleNamespace(effective_budget_caps=lambda: cfg),
        _runtime=SimpleNamespace(loop_started_at=loop_started_at),
        _snapshots=_projector(),
    )


def test_current_budget_snapshot_none_before_first_assembly() -> None:
    stub = _state_builder_stub(inputs=None, time_enabled=True, loop_started_at=1.0)
    assert StateBuilder.current_budget_snapshot(stub) is None  # type: ignore[arg-type]


def test_current_budget_snapshot_none_without_time_cap() -> None:
    stub = _state_builder_stub(inputs=(2, 0.5), time_enabled=False, loop_started_at=1.0)
    assert StateBuilder.current_budget_snapshot(stub) is None  # type: ignore[arg-type]


def test_current_budget_snapshot_recomputes_time_from_fresh_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Loop started at monotonic=100; "now" = 100 + 30min, so 30 of 120 elapsed.
    stub = _state_builder_stub(inputs=(5, 2.0), time_enabled=True, loop_started_at=100.0)
    monkeypatch.setattr("agentshore.core.mixins.state.time.monotonic", lambda: 100.0 + 30 * 60)
    snap = StateBuilder.current_budget_snapshot(stub)  # type: ignore[arg-type]
    assert snap is not None
    assert snap.time_elapsed_minutes == pytest.approx(30.0)
    assert snap.time_remaining_minutes == pytest.approx(90.0)
    # Dollar fields carry the cached inputs unchanged.
    assert snap.spent == pytest.approx(2.0)


# --------------------------------------------------------------------------- #
# LoopRunner._maybe_emit_budget_heartbeat — throttle + emit
# --------------------------------------------------------------------------- #


def _loop_stub(*, budget: Any, last_at: float) -> SimpleNamespace:
    async def _safe_call(coro: Any, _name: str) -> None:
        await coro

    provider = MagicMock()
    provider.on_budget_update = AsyncMock()
    return SimpleNamespace(
        _last_budget_heartbeat_at=last_at,
        _state_builder=SimpleNamespace(current_budget_snapshot=lambda: budget),
        _runtime=SimpleNamespace(state_provider=provider),
        _host=SimpleNamespace(_safe_call=_safe_call),
    )


@pytest.mark.asyncio
async def test_heartbeat_emits_after_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    budget = _budget_snapshot()
    stub = _loop_stub(budget=budget, last_at=0.0)
    monkeypatch.setattr("agentshore.core.mixins.loop.time.monotonic", lambda: 1000.0)
    await LoopRunner._maybe_emit_budget_heartbeat(stub)  # type: ignore[arg-type]
    stub._runtime.state_provider.on_budget_update.assert_awaited_once_with(budget)
    assert stub._last_budget_heartbeat_at == 1000.0


@pytest.mark.asyncio
async def test_heartbeat_throttled_within_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _loop_stub(budget=_budget_snapshot(), last_at=995.0)
    # Only 5s since the last emit (< 30s cadence) → no emit.
    monkeypatch.setattr("agentshore.core.mixins.loop.time.monotonic", lambda: 1000.0)
    await LoopRunner._maybe_emit_budget_heartbeat(stub)  # type: ignore[arg-type]
    stub._runtime.state_provider.on_budget_update.assert_not_awaited()
    assert stub._last_budget_heartbeat_at == 995.0


@pytest.mark.asyncio
async def test_heartbeat_noop_when_no_time_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # current_budget_snapshot returns None (no time cap / no cache) → no emit,
    # and the throttle clock is NOT advanced so the next eligible tick can fire.
    stub = _loop_stub(budget=None, last_at=0.0)
    monkeypatch.setattr("agentshore.core.mixins.loop.time.monotonic", lambda: 1000.0)
    await LoopRunner._maybe_emit_budget_heartbeat(stub)  # type: ignore[arg-type]
    stub._runtime.state_provider.on_budget_update.assert_not_awaited()
    assert stub._last_budget_heartbeat_at == 0.0
