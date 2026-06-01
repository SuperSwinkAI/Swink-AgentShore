"""Phase 4F: Termination + escalation ladder tests."""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentshore.config import RuntimeConfig, SessionConfig
from agentshore.data.models import PlayRecord
from agentshore.plays.base import PlayParams
from agentshore.state import BudgetSnapshot, OrchestratorState, PlayOutcome, PlayType, SessionState


def _make_state(
    *,
    streak: int = 0,
    total_plays: int = 0,
    remaining: float = 10.0,
    total_budget: float = 20.0,
    last_play_type: PlayType | None = None,
    session_state: SessionState = SessionState.RUNNING,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="test",
        session_state=session_state,
        total_plays=total_plays,
        total_cost=total_budget - remaining,
        same_type_failure_streak=streak,
        last_play_type=last_play_type,
        budget=BudgetSnapshot(
            total_budget=total_budget,
            spent=total_budget - remaining,
            remaining=remaining,
            estimated_cost_per_play=0.05,
        ),
    )


def _make_orch(tmp_path: Path, cfg: RuntimeConfig) -> Any:
    """Build a minimal Orchestrator-like object without bootstrap for unit testing."""
    from tests.orchestrator_factory import make_test_orchestrator

    return make_test_orchestrator(tmp_path, cfg)


# ---------------------------------------------------------------------------
# State construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_state_includes_in_flight_plays(tmp_path: Path) -> None:
    import asyncio

    from agentshore.core import _DispatchContext

    cfg = RuntimeConfig()
    orch = _make_orch(tmp_path, cfg)
    orch._manager.handles = {}
    orch._store.get_open_issues = AsyncMock(return_value=[])
    orch._store.list_active_pull_requests = AsyncMock(return_value=[])
    orch._store.list_recently_merged_pull_requests = AsyncMock(return_value=[])
    orch._store.get_play_history = AsyncMock(return_value=[])
    orch._store.get_latest_trajectory = AsyncMock(return_value=None)

    pending = asyncio.get_running_loop().create_future()
    done = asyncio.get_running_loop().create_future()
    done.set_result(MagicMock())
    orch._in_flight = {"pending": pending, "done": done}
    orch._dispatch_ctx = {
        "pending": _DispatchContext(
            dispatch_id="pending",
            play_type=PlayType.SEED_PROJECT,
            params=PlayParams(),
            state_at_dispatch=_make_state(),
            pending_step=None,
            dispatched_at=0.0,
        ),
        "done": _DispatchContext(
            dispatch_id="done",
            play_type=PlayType.CODE_REVIEW,
            params=PlayParams(),
            state_at_dispatch=_make_state(),
            pending_step=None,
            dispatched_at=0.0,
        ),
    }

    state = await orch._build_state()

    assert state.in_flight_plays == [PlayType.SEED_PROJECT]


# ---------------------------------------------------------------------------
# _should_terminate
# ---------------------------------------------------------------------------


def test_should_not_terminate_normally() -> None:
    cfg = RuntimeConfig()

    orch = _make_orch(Path("/tmp"), cfg)
    state = _make_state()
    should_stop, reason = orch._should_terminate(state)
    assert not should_stop
    assert reason is None


def test_should_terminate_on_max_plays() -> None:
    import dataclasses

    cfg = dataclasses.replace(RuntimeConfig(), session=SessionConfig(max_plays=5))
    orch = _make_orch(Path("/tmp"), cfg)
    state = _make_state(total_plays=5)
    should_stop, reason = orch._should_terminate(state)
    assert should_stop
    assert reason == "max_plays"


def test_should_terminate_ignores_budget_reserve_check() -> None:
    cfg = RuntimeConfig()
    orch = _make_orch(Path("/tmp"), cfg)
    state = _make_state(remaining=0.0)
    should_stop, reason = orch._should_terminate(state)
    assert not should_stop
    assert reason is None


@pytest.mark.asyncio
async def test_budget_reserve_not_reached_keeps_running(tmp_path: Path) -> None:
    cfg = RuntimeConfig()
    orch = _make_orch(tmp_path, cfg)
    state = _make_state(remaining=5.01)

    result = await orch._begin_budget_reserve_drain_if_needed(state)

    assert result is state
    assert orch._store.update_session_state.await_count == 0


@pytest.mark.asyncio
async def test_budget_reserve_reached_begins_drain(tmp_path: Path) -> None:
    cfg = RuntimeConfig()
    orch = _make_orch(tmp_path, cfg)
    state = _make_state(remaining=5.0)
    draining_state = _make_state(remaining=5.0, session_state=SessionState.DRAINING)
    orch._build_state = AsyncMock(return_value=draining_state)

    result = await orch._begin_budget_reserve_drain_if_needed(state)

    assert result is draining_state
    assert orch._draining is True
    assert orch._drain_reason == "budget_reserve_reached"
    assert orch._end_session_report_requested is True
    assert orch._end_session_report_open_browser is True
    orch._store.update_session_state.assert_awaited_once_with("test-session", "draining")


def test_should_terminate_on_timeout(monkeypatch: Any) -> None:
    import dataclasses

    cfg = dataclasses.replace(RuntimeConfig(), session=SessionConfig(timeout_minutes=1))
    orch = _make_orch(Path("/tmp"), cfg)
    orch._loop_started_at = time.monotonic() - 70  # 70 seconds ago = past 1-min timeout
    state = _make_state()
    should_stop, reason = orch._should_terminate(state)
    assert should_stop
    assert reason == "timeout"


def test_compute_trajectory_record_uses_budget_and_graph(tmp_path: Path) -> None:
    from agentshore.beads import ProjectGraph

    cfg = RuntimeConfig()
    orch = _make_orch(tmp_path, cfg)
    next_state = _make_state(total_plays=4, remaining=6.0, total_budget=10.0)
    next_state = dataclasses.replace(next_state, graph=ProjectGraph(global_closure_ratio=0.6))
    outcome = PlayOutcome(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id=None,
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=0,
        dollar_cost=0.1,
        artifacts=[],
        alignment_delta=0.1,
        play_id=12,
    )
    history: list[PlayRecord] = []

    rec = orch._compute_trajectory_record(outcome, next_state, history)

    assert rec is not None
    assert rec.session_id == "test-session"
    assert rec.play_id == 12
    assert rec.projected_alignment_at_budget_end == pytest.approx(0.6)
    assert rec.estimated_remaining_plays == 120
    assert rec.estimated_remaining_cost == pytest.approx(6.0)


def test_compute_trajectory_record_returns_none_without_play_id(tmp_path: Path) -> None:
    cfg = RuntimeConfig()
    orch = _make_orch(tmp_path, cfg)
    outcome = PlayOutcome(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id=None,
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=0,
        dollar_cost=0.1,
        artifacts=[],
        alignment_delta=0.1,
        play_id=None,
    )

    assert orch._compute_trajectory_record(outcome, _make_state(), []) is None


def test_compute_trajectory_record_handles_disabled_budget(tmp_path: Path) -> None:
    cfg = RuntimeConfig()
    orch = _make_orch(tmp_path, cfg)
    state = OrchestratorState(
        session_id="test",
        session_state=SessionState.RUNNING,
        total_plays=1,
        total_cost=0.0,
        budget=BudgetSnapshot(
            total_budget=10.0,
            spent=4.0,
            remaining=6.0,
            estimated_cost_per_play=0.5,
            enabled=False,
        ),
    )
    outcome = PlayOutcome(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id=None,
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=0,
        dollar_cost=0.1,
        artifacts=[],
        alignment_delta=0.1,
        play_id=1,
    )

    rec = orch._compute_trajectory_record(outcome, state, [])
    assert rec is not None
    assert rec.estimated_remaining_plays == 0
    assert rec.estimated_remaining_cost == pytest.approx(0.0)


def _make_play_record(alignment_delta: float | None) -> PlayRecord:
    return PlayRecord(
        session_id="test-session",
        play_type="issue_pickup",
        started_at="2026-01-01T00:00:00Z",
        success=True,
        alignment_delta=alignment_delta,
    )


def _make_outcome_with_play_id(play_id: int = 99) -> PlayOutcome:
    return PlayOutcome(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id=None,
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=0,
        dollar_cost=0.1,
        artifacts=[],
        alignment_delta=0.01,
        play_id=play_id,
    )


# ---------------------------------------------------------------------------
# _compute_trajectory_record: slope projection path
# ---------------------------------------------------------------------------


def test_compute_trajectory_record_slope_path_two_deltas(tmp_path: Path) -> None:
    # When history has exactly 2 non-None deltas, slope = avg of both deltas;
    # projected = current_alignment + slope * estimated_remaining_plays.
    from agentshore.beads import ProjectGraph

    cfg = RuntimeConfig()
    orch = _make_orch(tmp_path, cfg)
    # remaining=1.0, estimated_cost_per_play=0.05 → avg_cost=0.05 → remaining_plays=20
    state = dataclasses.replace(
        _make_state(remaining=1.0),
        graph=ProjectGraph(global_closure_ratio=0.4),
    )
    history = [_make_play_record(0.01), _make_play_record(0.01)]

    rec = orch._compute_trajectory_record(_make_outcome_with_play_id(), state, history)

    assert rec is not None
    # slope = (0.01 + 0.01) / 2 = 0.01; projected = 0.4 + 0.01 * 20 = 0.6
    assert rec.projected_alignment_at_budget_end == pytest.approx(0.6)


def test_compute_trajectory_record_slope_path_three_deltas(tmp_path: Path) -> None:
    from agentshore.beads import ProjectGraph

    cfg = RuntimeConfig()
    orch = _make_orch(tmp_path, cfg)
    state = dataclasses.replace(
        _make_state(remaining=1.0),
        graph=ProjectGraph(global_closure_ratio=0.5),
    )
    # Three deltas: average = 0.01; projected = 0.5 + 0.01 * 20 = 0.7
    history = [
        _make_play_record(0.01),
        _make_play_record(0.01),
        _make_play_record(0.01),
    ]

    rec = orch._compute_trajectory_record(_make_outcome_with_play_id(), state, history)

    assert rec is not None
    assert rec.projected_alignment_at_budget_end == pytest.approx(0.7)


def test_compute_trajectory_record_slope_path_clamps_high(tmp_path: Path) -> None:
    # Large positive slope → projected > 1.0 → clamped to 1.0.
    from agentshore.beads import ProjectGraph

    cfg = RuntimeConfig()
    orch = _make_orch(tmp_path, cfg)
    state = dataclasses.replace(
        _make_state(remaining=1.0),
        graph=ProjectGraph(global_closure_ratio=0.9),
    )
    # slope = 0.05; projected = 0.9 + 0.05 * 20 = 1.9 → clamped to 1.0
    history = [_make_play_record(0.05), _make_play_record(0.05)]

    rec = orch._compute_trajectory_record(_make_outcome_with_play_id(), state, history)

    assert rec is not None
    assert rec.projected_alignment_at_budget_end == pytest.approx(1.0)


def test_compute_trajectory_record_slope_path_clamps_low(tmp_path: Path) -> None:
    # Large negative slope → projected < 0.0 → clamped to 0.0.
    from agentshore.beads import ProjectGraph

    cfg = RuntimeConfig()
    orch = _make_orch(tmp_path, cfg)
    state = dataclasses.replace(
        _make_state(remaining=1.0),
        graph=ProjectGraph(global_closure_ratio=0.3),
    )
    # slope = -0.05; projected = 0.3 + (-0.05) * 20 = -0.7 → clamped to 0.0
    history = [_make_play_record(-0.05), _make_play_record(-0.05)]

    rec = orch._compute_trajectory_record(_make_outcome_with_play_id(), state, history)

    assert rec is not None
    assert rec.projected_alignment_at_budget_end == pytest.approx(0.0)


def test_compute_trajectory_record_isfinite_guard(tmp_path: Path) -> None:
    # If inf/-inf deltas make slope NaN, projected falls back to current_alignment.
    from agentshore.beads import ProjectGraph

    cfg = RuntimeConfig()
    orch = _make_orch(tmp_path, cfg)
    state = dataclasses.replace(
        _make_state(remaining=1.0),
        graph=ProjectGraph(global_closure_ratio=0.5),
    )
    # sum([inf, -inf]) = nan → slope = nan → projected = nan → fallback
    history = [_make_play_record(float("inf")), _make_play_record(float("-inf"))]

    rec = orch._compute_trajectory_record(_make_outcome_with_play_id(), state, history)

    assert rec is not None
    assert rec.projected_alignment_at_budget_end == pytest.approx(0.5)


def test_compute_trajectory_record_slope_window_uses_last_ten(tmp_path: Path) -> None:
    # History of 12 records: first 2 have delta=1.0, last 10 have delta=0.0.
    # The window is capped at 10, so only the zero deltas are averaged → slope=0.0.
    from agentshore.beads import ProjectGraph

    cfg = RuntimeConfig()
    orch = _make_orch(tmp_path, cfg)
    state = dataclasses.replace(
        _make_state(remaining=1.0),
        graph=ProjectGraph(global_closure_ratio=0.5),
    )
    history = [_make_play_record(1.0), _make_play_record(1.0)] + [_make_play_record(0.0)] * 10

    rec = orch._compute_trajectory_record(_make_outcome_with_play_id(), state, history)

    assert rec is not None
    # If all 12 were averaged: slope ≈ 0.167, projected ≠ 0.5
    # With last-10 window: slope = 0.0, projected = 0.5 + 0.0 * 20 = 0.5
    assert rec.projected_alignment_at_budget_end == pytest.approx(0.5)


def test_compute_trajectory_record_slope_none_deltas_excluded(tmp_path: Path) -> None:
    # Records with alignment_delta=None are filtered out; only non-None deltas count.
    # If fewer than 2 non-None deltas remain, the else branch echoes current_alignment.
    from agentshore.beads import ProjectGraph

    cfg = RuntimeConfig()
    orch = _make_orch(tmp_path, cfg)
    state = dataclasses.replace(
        _make_state(remaining=1.0),
        graph=ProjectGraph(global_closure_ratio=0.6),
    )
    # 2 None records + 1 non-None → only 1 valid delta → else branch
    history = [
        _make_play_record(None),
        _make_play_record(None),
        _make_play_record(0.05),
    ]

    rec = orch._compute_trajectory_record(_make_outcome_with_play_id(), state, history)

    assert rec is not None
    assert rec.projected_alignment_at_budget_end == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_skipped_completion_updates_state_without_play_event_or_ppo(tmp_path: Path) -> None:
    """Skipped plays are observability events, not completed agent plays or PPO samples."""
    import asyncio

    from agentshore.core import _DispatchContext

    class Provider:
        def __init__(self) -> None:
            self.state_updates = 0
            self.play_completed = 0

        async def on_state_update(self, state: object) -> None:
            self.state_updates += 1

        async def on_play_completed(self, play: object) -> None:
            self.play_completed += 1

    cfg = RuntimeConfig()
    orch = _make_orch(tmp_path, cfg)
    provider = Provider()
    orch._state_provider = provider
    orch._build_state = AsyncMock(return_value=_make_state())
    orch._selector.on_play_completed = AsyncMock()

    task: asyncio.Future[PlayOutcome] = asyncio.Future()
    task.set_result(
        PlayOutcome.skipped_outcome(
            PlayType.MERGE_PR,
            "no_target",
            error="unresolved parameters",
        )
    )
    orch._dispatch_ctx = {
        "skip": _DispatchContext(
            dispatch_id="skip",
            play_type=PlayType.MERGE_PR,
            params=PlayParams(),
            state_at_dispatch=_make_state(),
            pending_step=object(),
            dispatched_at=0.0,
        )
    }

    await orch._process_completion("skip", task)

    assert provider.play_completed == 0
    assert provider.state_updates == 1
    orch._selector.on_play_completed.assert_not_awaited()
    orch._store.record_trajectory_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_orchestrator_records_trajectory_snapshot_on_success(tmp_path: Path) -> None:
    import asyncio

    from agentshore.core import _DispatchContext

    cfg = RuntimeConfig()
    orch = _make_orch(tmp_path, cfg)
    next_state = dataclasses.replace(_make_state(), graph=MagicMock(global_closure_ratio=0.6))
    orch._build_state = AsyncMock(return_value=next_state)

    task: asyncio.Future[PlayOutcome] = asyncio.Future()
    task.set_result(
        PlayOutcome(
            play_type=PlayType.ISSUE_PICKUP,
            agent_id=None,
            success=True,
            partial=False,
            duration_seconds=1.0,
            token_cost=0,
            dollar_cost=0.1,
            artifacts=[],
            alignment_delta=0.05,
            play_id=7,
        )
    )
    orch._dispatch_ctx = {
        "ok": _DispatchContext(
            dispatch_id="ok",
            play_type=PlayType.ISSUE_PICKUP,
            params=PlayParams(),
            state_at_dispatch=_make_state(),
            pending_step=None,
            dispatched_at=0.0,
        )
    }
    orch._store.get_play_history = AsyncMock(return_value=[])

    await orch._process_completion("ok", task)

    orch._store.get_play_history.assert_awaited_once_with(orch._session_id)
    orch._store.record_trajectory_snapshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_orchestrator_skips_trajectory_snapshot_on_failure(tmp_path: Path) -> None:
    import asyncio

    from agentshore.core import _DispatchContext

    cfg = RuntimeConfig()
    orch = _make_orch(tmp_path, cfg)
    orch._build_state = AsyncMock(return_value=_make_state())

    task: asyncio.Future[PlayOutcome] = asyncio.Future()
    task.set_result(
        PlayOutcome.failed(
            PlayType.ISSUE_PICKUP,
            "failed",
            agent_id=None,
            dollar_cost=0.1,
        )
    )
    orch._dispatch_ctx = {
        "fail": _DispatchContext(
            dispatch_id="fail",
            play_type=PlayType.ISSUE_PICKUP,
            params=PlayParams(),
            state_at_dispatch=_make_state(),
            pending_step=None,
            dispatched_at=0.0,
        )
    }

    await orch._process_completion("fail", task)

    orch._store.record_trajectory_snapshot.assert_not_awaited()


# ---------------------------------------------------------------------------
# Override queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_queue_drains_one_per_iteration(tmp_path: Path) -> None:
    """The override queue delivers plays in FIFO order, one per iteration."""
    cfg = RuntimeConfig()
    orch = _make_orch(tmp_path, cfg)

    executed_plays: list[PlayType] = []

    mock_outcome = MagicMock()
    mock_outcome.play_type = PlayType.ISSUE_PICKUP
    mock_outcome.success = True
    mock_outcome.partial = False
    mock_outcome.dollar_cost = 0.01
    mock_outcome.duration_seconds = 1.0
    mock_outcome.alignment_delta = 0.1
    mock_outcome.play_id = 1
    mock_outcome.inflation_raised = False

    async def mock_execute(play_type: PlayType, state: Any, override: Any) -> Any:
        executed_plays.append(play_type)
        return mock_outcome

    orch._executor.execute = mock_execute
    orch._selector.select = AsyncMock(return_value=None)
    orch._selector.consume_pending = MagicMock(return_value=None)
    orch._selector.should_update = MagicMock(return_value=False)
    orch._selector.should_checkpoint = MagicMock(return_value=False)
    orch._selector.on_play_completed = AsyncMock()

    orch._store.get_play_history = AsyncMock(return_value=[])
    orch._store.get_open_issues = AsyncMock(return_value=[])
    orch._store.get_latest_trajectory = AsyncMock(return_value=None)

    from agentshore.plays.override import OverrideEntry, OverrideKind

    for pt in (PlayType.CODE_REVIEW, PlayType.RUN_QA):
        orch._override_queue.put_nowait(
            OverrideEntry(
                play_type=pt,
                params=PlayParams(bypass_preconditions=True),
                kind=OverrideKind.BOOTSTRAP,
            )
        )

    # After both overrides, selector returns None and loop exits
    select_calls = 0

    async def mock_select_after_overrides(state: Any) -> Any:
        nonlocal select_calls
        select_calls += 1
        return None

    orch._selector.select = mock_select_after_overrides

    await orch.run_until_idle()

    assert executed_plays == [PlayType.CODE_REVIEW, PlayType.RUN_QA], (
        f"Wrong execution order: {executed_plays}"
    )
