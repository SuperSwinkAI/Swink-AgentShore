"""Tests for Orchestrator drain-mode: begin_drain idempotency, _should_terminate, adjust_budget."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.core import Orchestrator
from agentshore.plays.base import PlayParams
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    OrchestratorState,
    PlayType,
    SessionState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orch() -> Orchestrator:
    """Create a minimal Orchestrator via __new__ to avoid full bootstrap."""
    orch = Orchestrator.__new__(Orchestrator)
    orch._state_provider = MagicMock()
    orch._state_provider.on_session_draining = AsyncMock()
    orch._store = AsyncMock()
    orch._store.update_session_state = AsyncMock()
    orch._session_id = "sess-test"
    orch._pause_event = MagicMock()
    orch._pause_event.set = MagicMock()
    orch._pause_event.is_set = MagicMock(return_value=True)
    orch._pause_reason = None
    orch._extra_budget = 0.0
    orch._in_flight = {}
    orch._stop_requested = False
    orch._draining = False
    orch._drain_initialized = False
    orch._drain_reason = None
    orch._end_session_report_requested = False
    orch._end_session_report_open_browser = False
    return orch


def _snap(
    agent_id: str = "a1",
    status: AgentStatus = AgentStatus.IDLE,
) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CLAUDE_CODE,
        status=status,
        context_size=10_000,
        total_cost=0.1,
        total_tokens=50_000,
        tasks_completed=5,
        tasks_failed=0,
    )


def _state(
    session_state: SessionState,
    agents: list[AgentSnapshot] | None = None,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="sess-test",
        session_state=session_state,
        total_plays=5,
        total_cost=0.2,
        agents=[_snap()] if agents is None else agents,
    )


# ---------------------------------------------------------------------------
# begin_drain idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_begin_drain_sets_draining_flag() -> None:
    """begin_drain() sets _draining and _drain_initialized."""
    orch = _make_orch()
    await orch.begin_drain("test_reason")
    assert orch._draining is True
    assert orch._drain_initialized is True
    assert orch._drain_reason == "test_reason"
    assert orch._end_session_report_requested is True
    assert orch._end_session_report_open_browser is True


@pytest.mark.asyncio
async def test_begin_drain_is_idempotent() -> None:
    """Second call to begin_drain() is a no-op — provider called once."""
    orch = _make_orch()
    await orch.begin_drain("first")
    await orch.begin_drain("second")
    orch._state_provider.on_session_draining.assert_awaited_once()


@pytest.mark.asyncio
async def test_begin_drain_broadcasts_on_session_draining() -> None:
    """begin_drain() calls on_session_draining with the reason."""
    orch = _make_orch()
    await orch.begin_drain("budget_exhausted")
    orch._state_provider.on_session_draining.assert_awaited_once_with("budget_exhausted")


@pytest.mark.asyncio
async def test_begin_drain_updates_db_state() -> None:
    """begin_drain() writes 'draining' to the database."""
    orch = _make_orch()
    await orch.begin_drain("user_request")
    orch._store.update_session_state.assert_awaited_once_with("sess-test", "draining")


@pytest.mark.asyncio
async def test_begin_drain_wakes_pause_event() -> None:
    """begin_drain() sets the pause event so a paused loop unblocks."""
    orch = _make_orch()
    await orch.begin_drain("goals_complete")
    orch._pause_event.set.assert_called()


@pytest.mark.asyncio
async def test_begin_drain_skipped_when_stop_requested() -> None:
    """begin_drain() is a no-op when _stop_requested is already True."""
    orch = _make_orch()
    orch._stop_requested = True
    await orch.begin_drain("user_request")
    orch._state_provider.on_session_draining.assert_not_awaited()
    assert orch._end_session_report_requested is False


# ---------------------------------------------------------------------------
# request_drain (sync)
# ---------------------------------------------------------------------------


def test_request_drain_sets_flag() -> None:
    """request_drain() sets _draining without blocking."""
    orch = _make_orch()
    orch.request_drain("signal_sigterm")
    assert orch._draining is True
    assert orch._drain_reason == "signal_sigterm"


def test_request_drain_wakes_pause_event() -> None:
    """request_drain() sets the pause event to unblock the loop."""
    orch = _make_orch()
    orch.request_drain("signal_sigterm")
    orch._pause_event.set.assert_called()


# ---------------------------------------------------------------------------
# _should_terminate in drain mode
# ---------------------------------------------------------------------------


def test_should_terminate_drain_with_in_flight_returns_false() -> None:
    """_should_terminate returns False while plays are still in flight."""
    orch = _make_orch()
    orch._in_flight = {"play-1": MagicMock()}  # one play in flight
    state = _state(SessionState.DRAINING, agents=[_snap("a1", AgentStatus.BUSY)])
    result, reason = orch._should_terminate(state)
    assert result is False
    assert reason is None


def test_should_terminate_drain_with_live_agents_returns_false() -> None:
    """_should_terminate returns False while non-terminated agents remain."""
    orch = _make_orch()
    orch._in_flight = {}
    state = _state(SessionState.DRAINING, agents=[_snap("a1", AgentStatus.IDLE)])
    result, reason = orch._should_terminate(state)
    assert result is False


def test_should_terminate_drain_complete_when_all_terminated() -> None:
    """_should_terminate returns (True, 'drain_complete') when all agents terminated and no in-flight."""
    orch = _make_orch()
    orch._in_flight = {}
    state = _state(
        SessionState.DRAINING,
        agents=[
            _snap("a1", AgentStatus.TERMINATED),
            _snap("a2", AgentStatus.TERMINATED),
        ],
    )
    result, reason = orch._should_terminate(state)
    assert result is True
    assert reason == "drain_complete"


def test_should_terminate_drain_complete_with_no_agents() -> None:
    """_should_terminate returns (True, 'drain_complete') when agents list is empty and no in-flight."""
    orch = _make_orch()
    orch._in_flight = {}
    state = _state(SessionState.DRAINING, agents=[])
    result, reason = orch._should_terminate(state)
    assert result is True
    assert reason == "drain_complete"


def test_should_terminate_stop_requested_overrides_drain() -> None:
    """When _stop_requested is True, terminate immediately regardless of drain state."""
    orch = _make_orch()
    orch._stop_requested = True
    orch._in_flight = {"x": MagicMock()}
    state = _state(SessionState.DRAINING, agents=[_snap("a1", AgentStatus.BUSY)])
    result, reason = orch._should_terminate(state)
    assert result is True
    assert reason == "stop_requested"


@pytest.mark.asyncio
async def test_consume_override_drops_non_end_agent_after_drain_even_with_bypass() -> None:
    """Drain mode drops queued bootstrap/override plays except end_agent."""
    orch = _make_orch()
    orch._draining = True
    orch._first_play_override = None
    orch._override_queue = asyncio.Queue()
    from agentshore.plays.override import OverrideEntry, OverrideKind

    orch._override_queue.put_nowait(
        OverrideEntry(
            play_type=PlayType.INSTANTIATE_AGENT,
            params=PlayParams(bypass_preconditions=True),
            kind=OverrideKind.BOOTSTRAP,
        )
    )
    state = _state(SessionState.DRAINING, agents=[_snap("a1", AgentStatus.IDLE)])

    assert await orch._consume_override(state) is None
    assert orch._override_queue.empty()


# ---------------------------------------------------------------------------
# adjust_budget
# ---------------------------------------------------------------------------


def test_adjust_budget_increments_extra_budget() -> None:
    """adjust_budget() adds delta_usd to _extra_budget."""
    orch = _make_orch()
    should_resume = orch.adjust_budget(5.0)
    assert orch._extra_budget == pytest.approx(5.0)
    assert should_resume is False


def test_adjust_budget_accumulates() -> None:
    """Multiple adjust_budget calls accumulate."""
    orch = _make_orch()
    orch._extra_budget = 0.0
    orch.adjust_budget(3.0)
    orch.adjust_budget(2.5)
    assert orch._extra_budget == pytest.approx(5.5)


@pytest.mark.parametrize("reason", ["budget_exhausted", "budget_predictive"])
def test_adjust_budget_requests_resume_for_budget_pause(reason: str) -> None:
    orch = _make_orch()
    orch._pause_event.is_set = MagicMock(return_value=False)
    orch._pause_reason = reason

    assert orch.adjust_budget(5.0) is True


@pytest.mark.parametrize("reason", ["user_request", "loop_detected", None])
def test_adjust_budget_does_not_resume_non_budget_pause(reason: str | None) -> None:
    orch = _make_orch()
    orch._pause_event.is_set = MagicMock(return_value=False)
    orch._pause_reason = reason

    assert orch.adjust_budget(5.0) is False


def test_adjust_budget_does_not_resume_while_draining() -> None:
    orch = _make_orch()
    orch._pause_event.is_set = MagicMock(return_value=False)
    orch._pause_reason = "budget_exhausted"
    orch._draining = True

    assert orch.adjust_budget(5.0) is False


def test_adjust_budget_ignored_delta_never_resumes() -> None:
    orch = _make_orch()
    orch._pause_event.is_set = MagicMock(return_value=False)
    orch._pause_reason = "budget_exhausted"

    assert orch.adjust_budget(-1.0) is False


def test_request_drain_twice_keeps_first_reason() -> None:
    """Second request_drain() is a no-op once _drain_initialized is True."""
    orch = _make_orch()
    orch.request_drain("first_reason")
    orch._drain_initialized = True  # simulate begin_drain having run
    orch.request_drain("second_reason")
    assert orch._drain_reason == "first_reason"


@pytest.mark.asyncio
async def test_stop_inner_generates_esr_before_close_and_opens_last(tmp_path: Path) -> None:
    orch = Orchestrator.__new__(Orchestrator)
    events: list[str] = []
    report_path = tmp_path / ".agentshore" / "reports" / "end-session-sess-test.html"

    async def _refresh() -> None:
        events.append("refresh")

    async def _generate() -> Path:
        events.append("generate")
        return report_path

    async def _complete(*_: object) -> None:
        events.append("complete")

    async def _close() -> None:
        events.append("store_close")

    async def _ended(_: str) -> None:
        events.append("ended")

    orch._session_id = "sess-test"
    orch._repo_root = tmp_path
    orch._stop_reason = "ppo_selected"
    orch._in_flight = {}
    orch._dispatch_ctx = {}
    orch._manager = MagicMock()
    orch._manager.handles = {}
    orch._health = None
    orch._integrity = None
    orch._power_assertion = None
    orch._end_session_report_requested = True
    orch._end_session_report_open_browser = True
    orch._build_state = AsyncMock(return_value=_state(SessionState.DRAINING, agents=[]))
    orch._refresh_issues = AsyncMock(side_effect=_refresh)
    orch._generate_end_session_report = AsyncMock(side_effect=_generate)
    orch._store = AsyncMock()
    orch._store.complete_session = AsyncMock(side_effect=_complete)
    orch._store.close = AsyncMock(side_effect=_close)
    orch._state_provider = MagicMock()
    orch._state_provider.on_session_ended = AsyncMock(side_effect=_ended)

    with patch("webbrowser.open", side_effect=lambda _: events.append("open")):
        await orch._stop_inner(0.0)

    assert events.index("generate") < events.index("store_close")
    assert events.index("ended") < events.index("open")
