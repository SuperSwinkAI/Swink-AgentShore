"""Phase 5A: asyncio.Event-based pause/resume + on_agent_changed wiring."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentshore.config import FeedbackConfig, RuntimeConfig
from agentshore.plays.base import PlayParams
from agentshore.state import AgentStatus, NullStateProvider, PlayOutcome, PlayType, SessionState


def _make_orch(tmp_path: Path, cfg: RuntimeConfig | None = None) -> Any:
    from agentshore.core import Orchestrator

    mock_store = AsyncMock()
    mock_store.update_session_state = AsyncMock()
    mock_store.get_play_history = AsyncMock(return_value=[])
    mock_store.get_open_issues = AsyncMock(return_value=[])
    mock_store.get_latest_trajectory = AsyncMock(return_value=None)

    mock_selector = MagicMock()
    mock_selector.__class__.__name__ = "MockSelector"
    mock_selector.consume_pending = MagicMock(return_value=None)
    mock_selector.should_update = MagicMock(return_value=False)
    mock_selector.should_checkpoint = MagicMock(return_value=False)
    mock_selector.on_play_completed = AsyncMock()
    # Eligibility refactor: the loop drains confirm-repicks once per cycle via
    # consume_repick_count(). Return a real int so _record_selection_repicks
    # doesn't choke on a MagicMock in the ``repicks > 0`` comparison.
    mock_selector.consume_repick_count = MagicMock(return_value=0)

    from types import SimpleNamespace

    orch = Orchestrator.__new__(Orchestrator)
    orch._cfg = cfg or RuntimeConfig()
    orch._repo_root = tmp_path
    orch._session_id = "test-session"
    orch._store = mock_store
    orch._manager = MagicMock()
    # desktop-mr1i: dispatch consults _manager.worktrees.main_repo. A bare
    # tmp_path isn't a git work tree, so _is_git_work_tree short-circuits to
    # TrunkAllocation and skips the async allocate_for_dispatch call.
    orch._manager.worktrees = SimpleNamespace(main_repo=tmp_path)
    orch._executor = MagicMock()
    orch._selector = mock_selector
    orch._state_provider = NullStateProvider()
    orch._stop_requested = False
    orch._exit_stack = MagicMock()
    orch._health = None
    orch._integrity = None
    orch._power_assertion = None
    orch._in_flight = {}
    orch._dispatch_ctx = {}
    orch._completion_processing_count = 0
    orch._completion_processing_idle = asyncio.Event()
    orch._completion_processing_idle.set()
    orch.context_pressure_hints = {}
    orch._seed_path = None
    orch._step_index = 0
    orch._policy_version = "test"
    orch._config_hash = "abc"
    orch._metrics = None
    orch._first_play_override = None
    orch._override_queue = asyncio.Queue()
    orch._loop_started_at = 0.0
    orch._registry = None
    orch._pause_event = asyncio.Event()
    orch._pause_event.set()
    orch._pause_reason = None
    orch._last_play_id = None
    orch._draining = False
    orch._drain_reason = None
    orch._last_warned_failure_streak = None
    orch._last_warned_any_streak = None
    orch._forced_mask_play_types = ()
    orch._executor.inflight_issues = set()
    orch._executor.planned_issues = frozenset()
    orch._feedback_cadence_plays_since_ack = 0
    orch._feedback_cadence_last_ack_monotonic = 0.0
    # v0.15 Phase 5: state-divergence signals consumed by _assemble_state.
    import collections as _collections

    from agentshore.core.velocity_tracker import VelocityTracker

    orch._velocity = VelocityTracker(velocity_window_size=50)
    orch._recent_play_outcomes = _collections.deque(maxlen=50)
    # desktop-65bg: in-memory shadow of just-completed plays.
    orch._recent_play_completions = _collections.deque(maxlen=64)
    # desktop-quv9: in-memory shadow of just-applied issue labels so the next
    # selector tick sees them across the gh-CLI / SQLite WAL-flush lag window.
    orch._recent_applied_labels = _collections.deque(maxlen=64)
    # desktop-yrr: loop-detector filter — override-dispatched plays excluded.
    orch._pending_override_kind = None
    orch._override_dispatched_play_ids = set()
    # desktop-rni0: loop-side rate-limit recovery + take_break failure escalation.
    from agentshore.core.recovery_tracker import RecoveryTracker

    orch._recovery = RecoveryTracker()
    return orch


def _make_outcome(play_type: PlayType = PlayType.TAKE_BREAK) -> PlayOutcome:
    return PlayOutcome(
        play_type=play_type,
        agent_id="agent-1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=100,
        dollar_cost=0.01,
        artifacts=[],
        alignment_delta=0.0,
        play_id=1,
    )


# ---------------------------------------------------------------------------
# _pause_event initial state
# ---------------------------------------------------------------------------


def test_pause_event_initially_set(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    assert orch._pause_event.is_set(), "_pause_event must start set (running)"


# ---------------------------------------------------------------------------
# pause() / resume()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_clears_event(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    await orch.pause("test_reason")
    assert not orch._pause_event.is_set()
    assert orch._pause_reason == "test_reason"


def test_assemble_state_reports_paused_when_pause_event_cleared(tmp_path: Path) -> None:
    """Authoritative snapshots reflect pause state even before the next play."""
    from agentshore.core.context import _StateData

    orch = _make_orch(tmp_path)
    orch._manager.handles = {}
    orch._pause_event.clear()

    state = orch._assemble_state(
        _StateData(
            issue_records=[],
            pr_records=[],
            pending_reviews=[],
            play_history=[],
            trajectory_record=None,
            graph=None,
        )
    )

    assert state.session_state is SessionState.PAUSED


@pytest.mark.asyncio
async def test_pause_fires_provider_hooks(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    orch._last_play_id = 7
    paused_reasons: list[str] = []
    feedback_reasons: list[str] = []

    class TrackingProvider(NullStateProvider):
        async def on_session_paused(self, reason: str) -> None:
            paused_reasons.append(reason)

        async def on_feedback_requested(self, reason: str) -> None:
            feedback_reasons.append(reason)

    orch._state_provider = TrackingProvider()
    await orch.pause("budget_exhausted")

    assert paused_reasons == ["budget_exhausted"]
    assert feedback_reasons == ["budget_exhausted"]
    orch._store.record_human_feedback.assert_awaited_once()
    call = orch._store.record_human_feedback.await_args.args[0]
    assert call.play_id == 7
    assert call.trigger == "budget_exhausted"
    assert call.action_taken == "pause_requested"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "feedback_cfg"),
    [
        ("stagnation", FeedbackConfig(on_stagnation=False)),
        ("loop_detected", FeedbackConfig(on_loop_escalation=False)),
        ("budget_exhausted", FeedbackConfig(on_budget_exhaustion=False)),
        ("budget_predictive", FeedbackConfig(on_budget_exhaustion=False)),
        ("unknown_reason", FeedbackConfig(on_ambiguous_intake=False)),
    ],
)
async def test_pause_respects_feedback_trigger_flags(
    tmp_path: Path, reason: str, feedback_cfg: FeedbackConfig
) -> None:
    cfg = RuntimeConfig(feedback=feedback_cfg)
    orch = _make_orch(tmp_path, cfg=cfg)
    paused_reasons: list[str] = []
    feedback_reasons: list[str] = []

    class TrackingProvider(NullStateProvider):
        async def on_session_paused(self, paused_reason: str) -> None:
            paused_reasons.append(paused_reason)

        async def on_feedback_requested(self, feedback_reason: str) -> None:
            feedback_reasons.append(feedback_reason)

    orch._state_provider = TrackingProvider()
    await orch.pause(reason)

    assert paused_reasons == [reason]
    assert feedback_reasons == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["user_request", "ipc_request"])
async def test_user_initiated_pause_always_requests_feedback(tmp_path: Path, reason: str) -> None:
    cfg = RuntimeConfig(feedback=FeedbackConfig(on_ambiguous_intake=False))
    orch = _make_orch(tmp_path, cfg=cfg)
    feedback_reasons: list[str] = []

    class TrackingProvider(NullStateProvider):
        async def on_feedback_requested(self, feedback_reason: str) -> None:
            feedback_reasons.append(feedback_reason)

    orch._state_provider = TrackingProvider()
    await orch.pause(reason)

    assert feedback_reasons == [reason]


@pytest.mark.asyncio
async def test_resume_sets_event(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    await orch.pause("loop_detected")
    assert not orch._pause_event.is_set()
    await orch.resume()
    assert orch._pause_event.is_set()
    assert orch._pause_reason is None


@pytest.mark.asyncio
async def test_resume_updates_session_state(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    await orch.pause("loop_detected")
    await orch.resume()
    orch._store.update_session_state.assert_called_with("test-session", "running")


# ---------------------------------------------------------------------------
# Loop awaits pause_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_awaits_pause_event(tmp_path: Path) -> None:
    """Pause during execution; loop blocks until resumed."""
    orch = _make_orch(tmp_path)

    play_count = 0
    pause_triggered = asyncio.Event()

    async def mock_select(state: Any) -> tuple[PlayType, PlayParams] | None:
        nonlocal play_count
        if play_count >= 3:
            return None
        return (PlayType.ISSUE_PICKUP, PlayParams())

    orch._selector.select = mock_select

    async def mock_execute(play_type: PlayType, state: Any, override: Any = None) -> Any:
        nonlocal play_count
        play_count += 1
        if play_count == 1:
            await orch.pause("user_request")
            pause_triggered.set()
        return _make_outcome()

    orch._executor.execute = mock_execute

    task = asyncio.create_task(orch.run_until_idle())
    await asyncio.wait_for(pause_triggered.wait(), timeout=2.0)

    # Loop should be paused
    assert not orch._pause_event.is_set()
    count_at_pause = play_count

    # Resume → loop finishes remaining plays
    await orch.resume()
    await asyncio.wait_for(task, timeout=5.0)
    assert play_count > count_at_pause, "More plays should have run after resume"


@pytest.mark.asyncio
async def test_paused_loop_harvests_completed_in_flight_without_dispatching(
    tmp_path: Path,
) -> None:
    """Pause blocks new dispatch, but completed in-flight plays are still processed."""
    orch = _make_orch(tmp_path)

    execute_started = asyncio.Event()
    first_can_finish = asyncio.Event()
    first_harvested = asyncio.Event()
    execute_count = 0

    async def mock_select(state: Any) -> tuple[PlayType, PlayParams] | None:
        if execute_count < 2:
            return (PlayType.TAKE_BREAK, PlayParams())
        return None

    orch._selector.select = mock_select

    async def mock_execute(play_type: PlayType, state: Any, override: Any = None) -> Any:
        nonlocal execute_count
        execute_count += 1
        if execute_count == 1:
            execute_started.set()
            await first_can_finish.wait()
        return _make_outcome(play_type)

    original_process_completion = orch._process_completion

    async def wrapped_process_completion(dispatch_id: str, task: asyncio.Task[PlayOutcome]) -> None:
        await original_process_completion(dispatch_id, task)
        if execute_count == 1:
            first_harvested.set()

    orch._executor.execute = mock_execute
    orch._process_completion = wrapped_process_completion  # type: ignore[method-assign]

    task = asyncio.create_task(orch.run_until_idle())
    await asyncio.wait_for(execute_started.wait(), timeout=2.0)
    await orch.pause("user_request")
    first_can_finish.set()

    await asyncio.wait_for(first_harvested.wait(), timeout=2.0)
    assert not orch._pause_event.is_set()
    assert execute_count == 1
    assert orch._in_flight == {}

    await asyncio.sleep(0.05)
    assert execute_count == 1, "Paused loop must not dispatch more work"

    await orch.resume()
    await asyncio.wait_for(task, timeout=2.0)
    assert execute_count == 2


@pytest.mark.asyncio
async def test_pause_with_reason_does_not_exit_loop(tmp_path: Path) -> None:
    """_pause_with_reason no longer causes run_until_idle to return immediately."""
    orch = _make_orch(tmp_path)

    called = False
    pause_done = asyncio.Event()

    async def mock_select(state: Any) -> tuple[PlayType, PlayParams] | None:
        nonlocal called
        if not called:
            return (PlayType.ISSUE_PICKUP, PlayParams())
        return None

    orch._selector.select = mock_select

    async def mock_execute(play_type: PlayType, state: Any, override: Any = None) -> Any:
        nonlocal called
        called = True
        await orch._pause_with_reason("loop_detected")
        pause_done.set()
        return _make_outcome()

    orch._executor.execute = mock_execute

    # Loop pauses; need to resume it
    task = asyncio.create_task(orch.run_until_idle())
    # Wait until the pause has been triggered (but don't resume yet)
    # Use a short sleep as fallback since _pause_with_reason blocks
    for _ in range(20):
        await asyncio.sleep(0.01)
        if not orch._pause_event.is_set():
            break
    # Loop is alive but blocked — NOT returned
    assert not task.done(), "_pause_with_reason must not exit the loop"

    # Unblock and let it finish
    await orch.resume()
    await asyncio.wait_for(task, timeout=2.0)


# ---------------------------------------------------------------------------
# _dispatch_ctx tracking (replaces _play_started_at)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_ctx_set_during_execute(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    captured_in_flight: list[int] = []

    play_done = asyncio.Event()
    execute_started = asyncio.Event()

    async def mock_select(state: Any) -> tuple[PlayType, PlayParams] | None:
        if not play_done.is_set():
            return (PlayType.ISSUE_PICKUP, PlayParams())
        return None

    orch._selector.select = mock_select

    async def mock_execute(play_type: PlayType, state: Any, override: Any = None) -> Any:
        execute_started.set()
        captured_in_flight.append(len(orch._in_flight))
        play_done.set()
        return _make_outcome()

    orch._executor.execute = mock_execute

    task = asyncio.create_task(orch.run_until_idle())
    await asyncio.wait_for(execute_started.wait(), timeout=2.0)
    assert captured_in_flight[0] >= 1, "_in_flight must have entries while executing"
    await asyncio.wait_for(task, timeout=2.0)
    assert len(orch._in_flight) == 0, "_in_flight must be empty after loop exits"


# ---------------------------------------------------------------------------
# on_agent_changed wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_agent_changed_called_on_crash(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    changed_events: list[tuple[str, AgentStatus]] = []

    class TrackingProvider(NullStateProvider):
        async def on_agent_changed(self, agent_id: str, status: AgentStatus) -> None:
            changed_events.append((agent_id, status))

    orch._state_provider = TrackingProvider()
    await orch._on_crash("agent-99", return_code=1)

    assert len(changed_events) == 1
    assert changed_events[0] == ("agent-99", AgentStatus.ERROR)


@pytest.mark.asyncio
async def test_on_agent_changed_called_on_context_pressure(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    changed_events: list[tuple[str, AgentStatus]] = []

    class TrackingProvider(NullStateProvider):
        async def on_agent_changed(self, agent_id: str, status: AgentStatus) -> None:
            changed_events.append((agent_id, status))

    orch._state_provider = TrackingProvider()
    await orch._on_context_pressure("agent-42", ratio=0.95)

    assert len(changed_events) == 1
    assert changed_events[0][0] == "agent-42"
    assert orch.context_pressure_hints["agent-42"] == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# Feedback cadence checkpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feedback_cadence_plays_pauses_after_configured_completed_plays(
    tmp_path: Path,
) -> None:
    cfg = RuntimeConfig(feedback=FeedbackConfig(cadence_plays=2))
    orch = _make_orch(tmp_path, cfg=cfg)
    orch._feedback_cadence_plays_since_ack = 2

    paused_reasons: list[str] = []
    feedback_reasons: list[str] = []

    class TrackingProvider(NullStateProvider):
        async def on_session_paused(self, reason: str) -> None:
            paused_reasons.append(reason)

        async def on_feedback_requested(self, reason: str) -> None:
            feedback_reasons.append(reason)

    orch._state_provider = TrackingProvider()
    paused = await orch._pause_for_feedback_cadence_if_due()

    assert paused
    assert paused_reasons == ["feedback_cadence_plays"]
    assert feedback_reasons == ["feedback_cadence_plays"]


@pytest.mark.asyncio
async def test_feedback_cadence_minutes_pauses_after_configured_elapsed_time(
    tmp_path: Path,
) -> None:
    cfg = RuntimeConfig(feedback=FeedbackConfig(cadence_minutes=5))
    orch = _make_orch(tmp_path, cfg=cfg)
    # 0.0 is far in the past; monotonic() is always well above 300 on any running system
    orch._feedback_cadence_last_ack_monotonic = 0.0

    paused_reasons: list[str] = []
    feedback_reasons: list[str] = []

    class TrackingProvider(NullStateProvider):
        async def on_session_paused(self, reason: str) -> None:
            paused_reasons.append(reason)

        async def on_feedback_requested(self, reason: str) -> None:
            feedback_reasons.append(reason)

    orch._state_provider = TrackingProvider()
    paused = await orch._pause_for_feedback_cadence_if_due()

    assert paused
    assert paused_reasons == ["feedback_cadence_minutes"]
    assert feedback_reasons == ["feedback_cadence_minutes"]


@pytest.mark.asyncio
async def test_feedback_cadence_disabled_does_not_pause(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)  # default config: cadence_plays=None, cadence_minutes=None
    orch._feedback_cadence_plays_since_ack = 100

    paused_reasons: list[str] = []

    class TrackingProvider(NullStateProvider):
        async def on_session_paused(self, reason: str) -> None:
            paused_reasons.append(reason)

    orch._state_provider = TrackingProvider()
    paused = await orch._pause_for_feedback_cadence_if_due()

    assert not paused
    assert paused_reasons == []
    assert orch._pause_event.is_set()


@pytest.mark.asyncio
async def test_feedback_cadence_resets_after_resume(tmp_path: Path) -> None:
    cfg = RuntimeConfig(feedback=FeedbackConfig(cadence_plays=2))
    orch = _make_orch(tmp_path, cfg=cfg)
    orch._feedback_cadence_plays_since_ack = 2

    paused_reasons: list[str] = []

    class TrackingProvider(NullStateProvider):
        async def on_session_paused(self, reason: str) -> None:
            paused_reasons.append(reason)

        async def on_feedback_requested(self, reason: str) -> None:
            pass

    orch._state_provider = TrackingProvider()

    # Trigger first cadence pause
    await orch._pause_for_feedback_cadence_if_due()
    assert paused_reasons == ["feedback_cadence_plays"]
    assert not orch._pause_event.is_set()

    # resume() resets the play-count baseline
    await orch.resume()
    assert orch._feedback_cadence_plays_since_ack == 0

    # Same play count (0 < 2) must not immediately re-trigger
    paused_reasons.clear()
    paused = await orch._pause_for_feedback_cadence_if_due()
    assert not paused
    assert paused_reasons == []
