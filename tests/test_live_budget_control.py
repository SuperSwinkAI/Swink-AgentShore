"""Wave-1 core coverage for live budget control (#41/#42 foundation).

Exercises the shared Orchestrator core that both transports build on:
``effective_budget_caps`` resolution, absolute ``set_budget``, additive
``add_budget``, bounds validation, persistence to ``agentshore.yaml``, the
``current_budget`` echo, and drain re-arm / reversal. Transport-specific tests
(sidecar RPC, CLI command, desktop dialog) live with each transport.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agentshore.config import load_config
from agentshore.config.models import BudgetConfig, RuntimeConfig
from agentshore.errors import OrchestratorError
from agentshore.state import BudgetSnapshot

from .orchestrator_factory import make_test_orchestrator


def _snap(
    *,
    total: float = 200.0,
    spent: float = 10.0,
    enabled: bool = True,
    time_enabled: bool = False,
    time_total: float | None = None,
    time_elapsed: float | None = None,
) -> SimpleNamespace:
    remaining = max(0.0, total - spent) if enabled else float("inf")
    time_remaining = (
        max(0.0, time_total - time_elapsed)
        if (time_enabled and time_total is not None and time_elapsed is not None)
        else None
    )
    snap = BudgetSnapshot(
        total_budget=total,
        spent=spent,
        remaining=remaining,
        estimated_cost_per_play=0.25,
        enabled=enabled,
        time_enabled=time_enabled,
        time_total_minutes=time_total,
        time_elapsed_minutes=time_elapsed,
        time_remaining_minutes=time_remaining,
    )
    return SimpleNamespace(budget=snap)


def _mock_state(orch: object, snap: SimpleNamespace) -> None:
    orch._state_builder.build_state = AsyncMock(return_value=snap)  # type: ignore[attr-defined]


def _mock_budget_only(orch: object, snap: SimpleNamespace) -> None:
    # current_budget uses the cheap side-effect-free build_budget_only path (#281).
    orch._state_builder.build_budget_only = AsyncMock(  # type: ignore[attr-defined]
        return_value=snap.budget
    )


def test_effective_caps_fall_through_to_cfg(tmp_path: Path) -> None:
    cfg = RuntimeConfig(
        budget=BudgetConfig(enabled=True, total=150.0, time_enabled=True, time_total_minutes=1440)
    )
    orch = make_test_orchestrator(tmp_path, cfg=cfg)
    caps = orch.effective_budget_caps()
    assert caps.enabled is True
    assert caps.total == 150.0
    assert caps.time_enabled is True
    assert caps.time_total_minutes == 1440


def test_effective_caps_overrides_shadow_cfg(tmp_path: Path) -> None:
    cfg = RuntimeConfig(budget=BudgetConfig(enabled=True, total=150.0))
    orch = make_test_orchestrator(tmp_path, cfg=cfg)
    orch._runtime.budget_override_enabled = True
    orch._runtime.budget_override_total = 500.0
    orch._runtime.time_override_enabled = True
    orch._runtime.time_override_minutes = 720
    caps = orch.effective_budget_caps()
    assert caps.total == 500.0
    assert caps.time_enabled is True
    assert caps.time_total_minutes == 720
    # cfg itself is never mutated.
    assert orch._runtime.cfg.budget.total == 150.0


@pytest.mark.asyncio
async def test_set_budget_applies_and_echoes(tmp_path: Path) -> None:
    orch = make_test_orchestrator(tmp_path)
    _mock_state(
        orch, _snap(total=300.0, spent=12.5, time_enabled=True, time_total=1440, time_elapsed=100)
    )
    applied = await orch.set_budget(
        dollars_enabled=True, dollars=300.0, time_enabled=True, time_minutes=1440, persist=False
    )
    assert orch._runtime.budget_override_total == 300.0
    assert orch._runtime.time_override_minutes == 1440
    assert applied["total"] == 300.0
    assert applied["time_remaining_minutes"] == 1340.0


@pytest.mark.asyncio
async def test_set_budget_persists_to_yaml(tmp_path: Path) -> None:
    cfg_path = tmp_path / "agentshore.yaml"
    orch = make_test_orchestrator(tmp_path)
    orch._runtime.config_path = cfg_path
    _mock_state(orch, _snap(total=250.0, time_enabled=True, time_total=720, time_elapsed=0))
    await orch.set_budget(
        dollars_enabled=True, dollars=250.0, time_enabled=True, time_minutes=720, persist=True
    )
    assert cfg_path.exists()
    reloaded = load_config(cfg_path)
    assert reloaded.budget.enabled is True
    assert reloaded.budget.total == 250.0
    assert reloaded.budget.time_enabled is True
    assert reloaded.budget.time_total_minutes == 720


@pytest.mark.asyncio
async def test_set_budget_unlimited_disables_both(tmp_path: Path) -> None:
    orch = make_test_orchestrator(tmp_path)
    _mock_state(orch, _snap(enabled=False, time_enabled=False))
    applied = await orch.set_budget(
        dollars_enabled=False, dollars=None, time_enabled=False, time_minutes=None, persist=False
    )
    assert orch._runtime.budget_override_enabled is False
    assert orch._runtime.time_override_enabled is False
    assert applied["enabled"] is False
    assert applied["time_enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        (
            {"dollars_enabled": True, "dollars": 5.0, "time_enabled": False, "time_minutes": None},
            "dollar cap",
        ),
        (
            {"dollars_enabled": False, "dollars": None, "time_enabled": True, "time_minutes": 30},
            "time cap",
        ),
        (
            {"dollars_enabled": False, "dollars": None, "time_enabled": True, "time_minutes": 5000},
            "time cap",
        ),
    ],
)
async def test_set_budget_bounds_rejected(tmp_path: Path, kwargs: dict, match: str) -> None:
    orch = make_test_orchestrator(tmp_path)
    with pytest.raises(OrchestratorError, match=match):
        await orch.set_budget(persist=False, **kwargs)
    # No override applied on rejection.
    assert orch._runtime.budget_override_enabled is None or kwargs["dollars_enabled"] is False


@pytest.mark.asyncio
async def test_add_budget_tops_up_dollars(tmp_path: Path) -> None:
    cfg = RuntimeConfig(budget=BudgetConfig(enabled=True, total=50.0))
    orch = make_test_orchestrator(tmp_path, cfg=cfg)
    _mock_state(orch, _snap(total=100.0, spent=10.0))
    await orch.add_budget(delta_usd=50.0, persist=False)
    assert orch._runtime.budget_override_total == 100.0


@pytest.mark.asyncio
async def test_add_budget_extends_time(tmp_path: Path) -> None:
    cfg = RuntimeConfig(budget=BudgetConfig(time_enabled=True, time_total_minutes=60))
    orch = make_test_orchestrator(tmp_path, cfg=cfg)
    _mock_state(orch, _snap(time_enabled=True, time_total=180, time_elapsed=0))
    await orch.add_budget(delta_minutes=120, persist=False)
    assert orch._runtime.time_override_minutes == 180


@pytest.mark.asyncio
async def test_add_budget_requires_a_positive_delta(tmp_path: Path) -> None:
    orch = make_test_orchestrator(tmp_path)
    with pytest.raises(OrchestratorError, match="positive"):
        await orch.add_budget(persist=False)


@pytest.mark.asyncio
async def test_add_budget_rejects_over_max_time(tmp_path: Path) -> None:
    cfg = RuntimeConfig(budget=BudgetConfig(time_enabled=True, time_total_minutes=4200))
    orch = make_test_orchestrator(tmp_path, cfg=cfg)
    with pytest.raises(OrchestratorError, match="outside"):
        await orch.add_budget(delta_minutes=600, persist=False)


@pytest.mark.asyncio
async def test_set_budget_pushes_state_update_for_live_dashboard(tmp_path: Path) -> None:
    # A live cap change must repaint the dashboard immediately, not wait for the next tick.
    orch = make_test_orchestrator(tmp_path)
    orch._runtime.state_provider = AsyncMock()
    _mock_state(orch, _snap(total=40.0, time_enabled=True, time_total=120, time_elapsed=0))
    await orch.set_budget(
        dollars_enabled=True, dollars=40.0, time_enabled=True, time_minutes=120, persist=False
    )
    orch._runtime.state_provider.on_state_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_budget_pushes_state_update(tmp_path: Path) -> None:
    cfg = RuntimeConfig(budget=BudgetConfig(enabled=True, total=50.0))
    orch = make_test_orchestrator(tmp_path, cfg=cfg)
    orch._runtime.state_provider = AsyncMock()
    _mock_state(orch, _snap(total=100.0, spent=10.0))
    await orch.add_budget(delta_usd=50.0, persist=False)
    orch._runtime.state_provider.on_state_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_current_budget_does_not_push_state(tmp_path: Path) -> None:
    # Read-only prefill must not emit a state_update.
    orch = make_test_orchestrator(tmp_path)
    orch._runtime.state_provider = AsyncMock()
    _mock_budget_only(orch, _snap(total=200.0, spent=10.0))
    await orch.current_budget()
    orch._runtime.state_provider.on_state_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_budget_echoes_effective(tmp_path: Path) -> None:
    orch = make_test_orchestrator(tmp_path)
    _mock_budget_only(
        orch, _snap(total=200.0, spent=42.0, time_enabled=True, time_total=1440, time_elapsed=240)
    )
    echo = await orch.current_budget()
    assert echo["total"] == 200.0
    assert echo["spent"] == 42.0
    assert echo["time_remaining_minutes"] == 1200.0
    assert echo["resumed"] is False


@pytest.mark.asyncio
async def test_current_budget_uses_cheap_read_not_build_state(tmp_path: Path) -> None:
    # #281: prefill must use build_budget_only, not build_state (which abandons work /
    # releases claims and stalled the desktop "Adjust Budget…" dialog under load).
    orch = make_test_orchestrator(tmp_path)
    orch._state_builder.build_state = AsyncMock(  # type: ignore[attr-defined]
        side_effect=AssertionError("current_budget must not call build_state (#281)")
    )
    _mock_budget_only(orch, _snap(total=120.0, spent=5.0))
    echo = await orch.current_budget()
    assert echo["total"] == 120.0
    assert echo["spent"] == 5.0
    orch._state_builder.build_state.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_build_budget_only_sources_spend_from_cheap_aggregate(tmp_path: Path) -> None:
    # #281: build_budget_only reads spend from the single-query session_play_totals
    # aggregate (COUNT(*)/SUM(dollar_cost)), not the ten-read build_state fan-out.
    store = AsyncMock()
    store.session_play_totals = AsyncMock(return_value=(7, 18.5))
    orch = make_test_orchestrator(
        tmp_path,
        cfg=RuntimeConfig(budget=BudgetConfig(enabled=True, total=100.0)),
        store=store,
    )
    snap = await orch._state_builder.build_budget_only()
    assert snap.spent == 18.5
    assert snap.total_budget == 100.0
    assert snap.remaining == pytest.approx(81.5)
    store.session_play_totals.assert_awaited_once()


@pytest.mark.asyncio
async def test_raising_time_cap_reverses_drain(tmp_path: Path) -> None:
    # Additive add_budget (CLI escape hatch) is the only path that reverses a reserve
    # drain; absolute set_budget is hard-rejected while draining.
    cfg = RuntimeConfig(budget=BudgetConfig(time_enabled=True, time_total_minutes=60))
    orch = make_test_orchestrator(tmp_path, cfg=cfg)
    orch._runtime.config_path = None
    # Session is draining because the 1h time reserve was reached.
    orch._runtime.draining = True
    orch._runtime.drain_initialized = True
    orch._runtime.drain_reason = "time_budget_reserve_reached"
    orch._runtime.end_session_report_requested = True
    # Extending to 4h with ~45m elapsed clears the reserve.
    _mock_state(orch, _snap(time_enabled=True, time_total=240, time_elapsed=45))
    applied = await orch.add_budget(delta_minutes=180, persist=False)
    assert applied["resumed"] is True
    assert orch._runtime.draining is False
    assert orch._runtime.drain_reason is None
    assert orch._runtime.end_session_report_requested is False
    orch._store.update_session_state.assert_awaited()


@pytest.mark.asyncio
async def test_raising_cap_while_still_in_reserve_does_not_reverse(tmp_path: Path) -> None:
    cfg = RuntimeConfig(budget=BudgetConfig(time_enabled=True, time_total_minutes=60))
    orch = make_test_orchestrator(tmp_path, cfg=cfg)
    orch._runtime.config_path = None
    orch._runtime.draining = True
    orch._runtime.drain_initialized = True
    orch._runtime.drain_reason = "time_budget_reserve_reached"
    # Still within the 20-min reserve even after the bump (55m elapsed of 60m).
    _mock_state(orch, _snap(time_enabled=True, time_total=60, time_elapsed=55))
    applied = await orch.add_budget(delta_minutes=1, persist=False)
    assert applied["resumed"] is False
    assert orch._runtime.draining is True


@pytest.mark.asyncio
async def test_set_budget_rejected_while_draining(tmp_path: Path) -> None:
    # #244: absolute set_budget is hard-rejected once winding down (the loop only
    # dispatches end_agent past drain, so it would silently no-op).
    orch = make_test_orchestrator(tmp_path)
    orch._runtime.config_path = None
    orch._runtime.draining = True
    before_enabled = orch._runtime.budget_override_enabled
    before_total = orch._runtime.budget_override_total
    with pytest.raises(OrchestratorError, match="winding down"):
        await orch.set_budget(
            dollars_enabled=True,
            dollars=300.0,
            time_enabled=False,
            time_minutes=None,
            persist=False,
        )
    # No override mutated on rejection.
    assert orch._runtime.budget_override_enabled == before_enabled
    assert orch._runtime.budget_override_total == before_total


@pytest.mark.asyncio
async def test_set_budget_rejected_while_stop_requested(tmp_path: Path) -> None:
    orch = make_test_orchestrator(tmp_path)
    orch._runtime.config_path = None
    orch._runtime.stop_requested = True
    with pytest.raises(OrchestratorError, match="winding down"):
        await orch.set_budget(
            dollars_enabled=True,
            dollars=300.0,
            time_enabled=False,
            time_minutes=None,
            persist=False,
        )


@pytest.mark.asyncio
async def test_add_budget_not_blocked_by_drain(tmp_path: Path) -> None:
    # add_budget is the escape hatch — must work while draining (reverses a reserve drain).
    cfg = RuntimeConfig(budget=BudgetConfig(enabled=True, total=50.0))
    orch = make_test_orchestrator(tmp_path, cfg=cfg)
    orch._runtime.config_path = None
    orch._runtime.draining = True
    orch._runtime.drain_reason = "budget_reserve_reached"
    _mock_state(orch, _snap(total=100.0, spent=10.0))
    await orch.add_budget(delta_usd=50.0, persist=False)
    assert orch._runtime.budget_override_total == 100.0
