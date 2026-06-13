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

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.core import Orchestrator
from agentshore.core.main_repo_guard import MainRepoGuard
from agentshore.core.mixins.loop import LoopRunner
from agentshore.core.override_queue import OverrideQueue
from agentshore.state import SessionState


@dataclass
class _CfgLoopDetection:
    fleet_idle_threshold: int = 5
    warn_after: int = 3
    force_switch_after: int = 5
    escalate_after: int = 7


@dataclass
class _CfgRL:
    loop_detection: _CfgLoopDetection = field(default_factory=_CfgLoopDetection)
    reverse_failsafe_enabled: bool = False


@dataclass
class _Cfg:
    rl: _CfgRL = field(default_factory=_CfgRL)


@dataclass
class _StateStub:
    session_state: SessionState = SessionState.RUNNING
    in_flight_plays: tuple[Any, ...] = ()
    action_mask: tuple[bool, ...] = ()
    open_issues: tuple[Any, ...] = ()
    pull_requests: tuple[Any, ...] = ()
    agents: tuple[Any, ...] = ()


def _orch() -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch._in_flight = {}
    orch._overrides = OverrideQueue()
    orch._main_repo = MainRepoGuard()
    orch._idle_streak = 0
    orch._last_selection_digest = None
    orch._session_id = "sess-562"
    orch._registry = None
    # Default to a non-PPO selector: the all-masked + no-work branch treats a
    # scripted/mock selector's idle as terminal (break). Tests exercising the
    # live-PPO keep-polling path swap this for a stub matched by a patched
    # _ppo_selector_cls.
    orch._selector = MagicMock()
    orch._cfg = _Cfg()  # type: ignore[assignment]
    orch._loop = LoopRunner(
        host=orch,
        session_id=orch._session_id,
        main_repo=orch._main_repo,
        overrides=orch._overrides,
        velocity=MagicMock(),
        state_builder=MagicMock(),
        dispatcher=MagicMock(),
        completion=MagicMock(),
        lifecycle=MagicMock(),
        drain=MagicMock(),
    )
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
    info_calls: MagicMock,
) -> None:
    """Mask shows 1+ ready play but has_remaining_work=False → wait (return True)."""
    orch = _orch()
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
async def test_all_masked_live_ppo_keeps_polling(
    info_calls: MagicMock,
) -> None:
    """All-masked + no graph work under the LIVE PPO selector → keep polling.

    An all-masked idle tick is common and transient for a live session (agents
    mid-issue, work reconciling between refreshes); it must NOT end the run. The
    loop keeps polling (return True + sleep) — a live session only ends from here
    via the fleet-idle backstop or once PPO unmasks and selects END_SESSION. It
    must also NOT bare-``return False`` (which would park: break without
    ``_natural_exit_reason`` → the sidecar supervisor never calls ``stop()``).
    """
    orch = _orch()
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
async def test_all_masked_scripted_selector_breaks(
    info_calls: MagicMock,
) -> None:
    """All-masked + no graph work under a scripted/mock (non-PPO) selector →
    break the loop. An exhausted FixedPlanSelector / test mock will never produce
    another play and there is no fleet-idle backstop semantics in replay, so the
    loop must terminate (return False) and let the harness own teardown."""
    orch = _orch()  # default _selector is a non-PPO MagicMock
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
async def test_fleet_idle_past_limit_ends_session_via_drain() -> None:
    """Whole fleet idle (no in-flight, no busy agents) past the limit → the loop
    initiates a clean autonomous drain (fire_natural_exit) rather than polling
    forever. This is the liveness backstop for the end-session wedge."""
    import time as _time

    from agentshore.core.mixins import loop as loop_mod

    orch = _orch()
    state = _StateStub(action_mask=(False, False, False, False), agents=())
    orch._loop._fleet_idle_since = _time.monotonic() - (
        loop_mod._FLEET_IDLE_END_SESSION_SECONDS + 5.0
    )

    with (
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


@pytest.mark.asyncio
async def test_busy_agent_resets_fleet_idle_clock(info_calls: MagicMock) -> None:
    """A busy agent means the fleet is not idle — the end-session clock resets to
    None and the drain never fires, even if the prior idle stretch was long."""
    import time as _time
    from types import SimpleNamespace

    from agentshore.core.mixins import loop as loop_mod
    from agentshore.state import AgentStatus

    orch = _orch()
    busy = SimpleNamespace(status=AgentStatus.BUSY)
    state = _StateStub(action_mask=(True, False), agents=(busy,))
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
async def test_continue_when_graph_has_work_regardless_of_mask(
    info_calls: MagicMock,
) -> None:
    """Existing behavior: has_remaining_work=True → wait (return True)."""
    orch = _orch()
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
