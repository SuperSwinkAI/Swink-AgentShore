"""Regression for issue #562 — loop must wait, not exit, when the action
mask shows ready plays even if ``candidate_plan.has_remaining_work`` is False.

Before the fix, ``_continue_if_selector_idle_work_remains`` returned ``False``
the moment ``has_remaining_work`` was False, which collapsed the outer
``while`` in ``loop.py`` (``break  # truly idle``). After the idle_tick
removal (PR #535) that exits the session even when PPO has mask-eligible
plays — a real-world hang for users starting against an already-seeded
project.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.core import Orchestrator
from agentshore.state import SessionState


@dataclass
class _StateStub:
    session_state: SessionState = SessionState.RUNNING
    in_flight_plays: tuple[Any, ...] = ()
    action_mask: tuple[bool, ...] = ()
    open_issues: tuple[Any, ...] = ()
    pull_requests: tuple[Any, ...] = ()
    agents: tuple[Any, ...] = ()


def _orch(tmp_path: Path) -> Orchestrator:
    from tests.orchestrator_factory import make_test_orchestrator

    # Default selector is a non-PPO MagicMock: all-masked idle is terminal (break).
    # Live-PPO keep-polling tests swap this via a patched _ppo_selector_cls.
    orch = make_test_orchestrator(tmp_path)
    orch._session_id = "sess-562"
    return orch


@pytest.fixture
def info_calls(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock_logger = MagicMock()
    monkeypatch.setattr("agentshore.core.mixins.loop._logger", mock_logger)
    return mock_logger.info


def _candidate_plan_stub(*, has_remaining_work: bool) -> Any:
    plan = MagicMock()
    plan.has_remaining_work = has_remaining_work
    plan.work_availability = MagicMock(
        tracked_issue_count=0,
        github_open_issue_count=0,
        workable_issue_count=0,
        blocked_issue_count=0,
        disallowed_issue_count=0,
        covered_by_open_pr_count=0,
        resolved_by_merged_pr_count=0,
        in_flight_issue_count=0,
        bead_in_progress_issue_count=0,
        ready_task_count=0,
        beads_blocks_issue_pickup=0,
        untracked_gh_issue_count=0,
        unlinked_ready_task_count=0,
        backlog_sync_work_count=0,
        planning_eligible_count=0,
        implementation_eligible_count=0,
        refinement_eligible_count=0,
        debugging_eligible_count=0,
        reviewable_pr_count=0,
        mergeable_pr_count=0,
        unblockable_pr_count=0,
        actionable_pr_work_count=0,
        terminal_no_work=False,
    )
    plan.candidates_by_play_type = {}
    return plan


@pytest.mark.asyncio
async def test_continue_when_mask_has_plays_even_if_graph_has_no_work(
    info_calls: MagicMock, tmp_path: Path
) -> None:
    """Mask shows 1+ ready play but has_remaining_work=False → wait (return True)."""
    orch = _orch(tmp_path)
    state = _StateStub(action_mask=(True, False, True, False))

    with (
        patch(
            "agentshore.plays.candidates.build_candidate_plan",
            return_value=_candidate_plan_stub(has_remaining_work=False),
        ),
        patch.object(orch._loop, "check_fleet_idle_persistent", new=AsyncMock()),
        patch("asyncio.sleep", new=AsyncMock()) as sleep_mock,
    ):
        result = await orch._loop.continue_if_selector_idle_work_remains(  # type: ignore[arg-type]
            state, reason="unchanged_digest"
        )

    assert result is True, (
        "loop must keep waiting when mask has eligible plays, "
        "even if the candidate-plan graph signal says no work"
    )
    sleep_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_all_masked_live_ppo_keeps_polling(info_calls: MagicMock, tmp_path: Path) -> None:
    """All-masked + no graph work under the LIVE PPO selector → keep polling.

    An all-masked idle tick is common and transient for a live session (agents
    mid-issue, work reconciling between refreshes); it must NOT end the run. The
    loop keeps polling (return True + sleep) — a live session only ends from here
    via the fleet-idle backstop or once PPO unmasks and selects END_SESSION. It
    must also NOT bare-``return False`` (which would park: break without
    ``_natural_exit_reason`` → the sidecar supervisor never calls ``stop()``).
    """
    orch = _orch(tmp_path)
    stub_ppo = MagicMock()
    orch._selector = stub_ppo
    state = _StateStub(action_mask=(False, False, False, False))

    with (
        patch(
            "agentshore.plays.candidates.build_candidate_plan",
            return_value=_candidate_plan_stub(has_remaining_work=False),
        ),
        patch(
            "agentshore.core.mixins.loop._ppo_selector_cls",
            return_value=type(stub_ppo),
        ),
        patch.object(orch._loop, "initiate_autonomous_stop", new=AsyncMock()) as stop_mock,
        patch.object(orch._loop, "check_fleet_idle_persistent", new=AsyncMock()),
        patch("asyncio.sleep", new=AsyncMock()) as sleep_mock,
    ):
        result = await orch._loop.continue_if_selector_idle_work_remains(  # type: ignore[arg-type]
            state, reason="unchanged_digest"
        )

    assert result is True, "live PPO all-masked is transient — keep polling, never park"
    sleep_mock.assert_awaited_once()
    stop_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_masked_scripted_selector_breaks(info_calls: MagicMock, tmp_path: Path) -> None:
    """All-masked + no graph work under a scripted/mock (non-PPO) selector →
    break the loop. An exhausted FixedPlanSelector / test mock will never produce
    another play and there is no fleet-idle backstop semantics in replay, so the
    loop must terminate (return False) and let the harness own teardown."""
    orch = _orch(tmp_path)  # default _selector is a non-PPO MagicMock
    state = _StateStub(action_mask=(False, False, False, False))

    with (
        patch(
            "agentshore.plays.candidates.build_candidate_plan",
            return_value=_candidate_plan_stub(has_remaining_work=False),
        ),
        patch.object(orch._loop, "initiate_autonomous_stop", new=AsyncMock()) as stop_mock,
        patch.object(orch._loop, "check_fleet_idle_persistent", new=AsyncMock()),
        patch("asyncio.sleep", new=AsyncMock()) as sleep_mock,
    ):
        result = await orch._loop.continue_if_selector_idle_work_remains(  # type: ignore[arg-type]
            state, reason="unchanged_digest"
        )

    assert result is False, "scripted selector exhaustion is terminal — break"
    stop_mock.assert_not_awaited()
    sleep_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_fleet_idle_past_limit_ends_session_via_drain(tmp_path: Path) -> None:
    """Whole fleet idle (no in-flight, no busy agents) past the limit → the loop
    initiates a clean autonomous drain (fire_natural_exit) rather than polling
    forever. This is the liveness backstop for the end-session wedge.

    No workable backlog remains here, so the plain ``fleet_idle_end_session``
    event fires (not the backlog-remaining variant below)."""
    import time as _time

    from agentshore.core.mixins import loop as loop_mod

    orch = _orch(tmp_path)
    state = _StateStub(action_mask=(False, False, False, False), agents=())
    orch._loop._fleet_idle_since = _time.monotonic() - (
        loop_mod._FLEET_IDLE_END_SESSION_SECONDS + 5.0
    )

    mock_logger = MagicMock()
    with (
        patch("agentshore.core.mixins.loop._logger", mock_logger),
        patch(
            "agentshore.plays.candidates.build_candidate_plan",
            return_value=_candidate_plan_stub(has_remaining_work=False),
        ),
        patch.object(orch._loop, "initiate_autonomous_stop", new=AsyncMock()) as stop_mock,
        patch.object(orch._loop, "check_fleet_idle_persistent", new=AsyncMock()),
    ):
        result = await orch._loop.continue_if_selector_idle_work_remains(  # type: ignore[arg-type]
            state, reason="unchanged_digest"
        )

    assert result is False
    stop_mock.assert_awaited_once()
    args, kwargs = stop_mock.call_args
    assert args[0] == "fleet_idle_timeout"
    assert kwargs.get("fire_natural_exit") is True
    warning_events = [call.args[0] for call in mock_logger.warning.call_args_list]
    assert "fleet_idle_end_session" in warning_events
    assert "fleet_idle_end_session_with_backlog_remaining" not in warning_events


@pytest.mark.asyncio
async def test_fleet_idle_past_limit_with_backlog_remaining_emits_distinct_event(
    tmp_path: Path,
) -> None:
    """Regression for the theta_rl f0026bb2 session (2026-07-08): the fleet went
    fully idle for 20+ minutes and auto-stopped via this exact backstop even
    though beads still reported workable issues and ready tasks — the real
    cause was a candidate stuck behind a mask/label exclusion that never
    cleared (a beads dependency-cycle conflict, see the beads/executor fix),
    not a genuine work shortage. When that happens, a distinct
    ``fleet_idle_end_session_with_backlog_remaining`` warning must fire so the
    anomaly is visible without reverse-engineering it from raw NDJSON."""
    import time as _time

    from agentshore.core.mixins import loop as loop_mod

    orch = _orch(tmp_path)
    state = _StateStub(action_mask=(False, False, False, False), agents=())
    orch._loop._fleet_idle_since = _time.monotonic() - (
        loop_mod._FLEET_IDLE_END_SESSION_SECONDS + 5.0
    )
    plan = _candidate_plan_stub(has_remaining_work=True)
    plan.work_availability.workable_issue_count = 2
    plan.work_availability.ready_task_count = 9

    mock_logger = MagicMock()
    with (
        patch("agentshore.core.mixins.loop._logger", mock_logger),
        patch("agentshore.plays.candidates.build_candidate_plan", return_value=plan),
        patch.object(orch._loop, "initiate_autonomous_stop", new=AsyncMock()) as stop_mock,
        patch.object(orch._loop, "check_fleet_idle_persistent", new=AsyncMock()),
    ):
        result = await orch._loop.continue_if_selector_idle_work_remains(  # type: ignore[arg-type]
            state, reason="unchanged_digest"
        )

    assert result is False
    stop_mock.assert_awaited_once()
    warning_events = [call.args[0] for call in mock_logger.warning.call_args_list]
    assert "fleet_idle_end_session_with_backlog_remaining" in warning_events
    assert "fleet_idle_end_session" not in warning_events
    call = next(
        c
        for c in mock_logger.warning.call_args_list
        if c.args[0] == "fleet_idle_end_session_with_backlog_remaining"
    )
    assert call.kwargs["workable_issues"] == 2
    assert call.kwargs["ready_tasks"] == 9


@pytest.mark.asyncio
async def test_busy_agent_resets_fleet_idle_clock(info_calls: MagicMock, tmp_path: Path) -> None:
    """A busy agent running a *work* play means the fleet is productively active —
    the end-session clock resets to None and the drain never fires, even if the
    prior idle stretch was long. (Lifecycle-only churn does NOT reset it; that is
    covered in the drained-idle self-terminate suite, #166.)"""
    import time as _time
    from types import SimpleNamespace

    from agentshore.core.mixins import loop as loop_mod
    from agentshore.state import AgentStatus, PlayType

    orch = _orch(tmp_path)
    busy = SimpleNamespace(status=AgentStatus.BUSY)
    state = _StateStub(
        action_mask=(True, False),
        agents=(busy,),
        in_flight_plays=(PlayType.ISSUE_PICKUP,),
    )
    orch._loop._fleet_idle_since = _time.monotonic() - (
        loop_mod._FLEET_IDLE_END_SESSION_SECONDS + 5.0
    )

    with (
        patch(
            "agentshore.plays.candidates.build_candidate_plan",
            return_value=_candidate_plan_stub(has_remaining_work=False),
        ),
        patch.object(orch._loop, "initiate_autonomous_stop", new=AsyncMock()) as stop_mock,
        patch.object(orch._loop, "check_fleet_idle_persistent", new=AsyncMock()),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        result = await orch._loop.continue_if_selector_idle_work_remains(  # type: ignore[arg-type]
            state, reason="unchanged_digest"
        )

    stop_mock.assert_not_awaited()
    assert orch._loop._fleet_idle_since is None
    assert result is True


@pytest.mark.asyncio
async def test_lifecycle_only_in_flight_does_not_reset_fleet_idle_clock(
    info_calls: MagicMock, tmp_path: Path
) -> None:
    """#166: a fleet whose only in-flight play is lifecycle (END_AGENT) — and the
    BUSY agent it produces — is productively idle. The end-session clock keeps
    accumulating, so the drain still fires past the deadline instead of being
    reset by the instantiate<->end churn."""
    import time as _time
    from types import SimpleNamespace

    from agentshore.core.mixins import loop as loop_mod
    from agentshore.state import AgentStatus, PlayType

    orch = _orch(tmp_path)
    busy = SimpleNamespace(status=AgentStatus.BUSY)
    state = _StateStub(
        action_mask=(False, False),
        agents=(busy,),
        in_flight_plays=(PlayType.END_AGENT,),
    )
    orch._loop._fleet_idle_since = _time.monotonic() - (
        loop_mod._FLEET_IDLE_END_SESSION_SECONDS + 5.0
    )

    with (
        patch(
            "agentshore.plays.candidates.build_candidate_plan",
            return_value=_candidate_plan_stub(has_remaining_work=False),
        ),
        patch.object(orch._loop, "initiate_autonomous_stop", new=AsyncMock()) as stop_mock,
        patch.object(orch._loop, "check_fleet_idle_persistent", new=AsyncMock()),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        result = await orch._loop.continue_if_selector_idle_work_remains(  # type: ignore[arg-type]
            state, reason="unchanged_digest"
        )

    stop_mock.assert_awaited_once()
    args, kwargs = stop_mock.call_args
    assert args[0] == "fleet_idle_timeout"
    assert kwargs.get("fire_natural_exit") is True
    assert result is False


@pytest.mark.asyncio
async def test_continue_when_graph_has_work_regardless_of_mask(
    info_calls: MagicMock, tmp_path: Path
) -> None:
    """Existing behavior: has_remaining_work=True → wait (return True)."""
    orch = _orch(tmp_path)
    state = _StateStub(action_mask=(False, False, False, False))

    with (
        patch(
            "agentshore.plays.candidates.build_candidate_plan",
            return_value=_candidate_plan_stub(has_remaining_work=True),
        ),
        patch.object(orch._loop, "check_fleet_idle_persistent", new=AsyncMock()),
        patch.object(orch._loop, "classify_selector_idle", return_value="waiting_for_capacity"),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        result = await orch._loop.continue_if_selector_idle_work_remains(  # type: ignore[arg-type]
            state, reason="selector_none"
        )

    assert result is True
