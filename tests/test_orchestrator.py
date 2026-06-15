"""Tests for Orchestrator — sub-phase 2O."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.config import AgentConfig, BootstrapConfig, RuntimeConfig
from agentshore.config.models import ModelTierConfig
from agentshore.core import Orchestrator
from agentshore.core.context import _DispatchContext
from agentshore.core.mixins.snapshots import SnapshotProjector
from agentshore.core.mixins.state import StateBuilder
from agentshore.core.override_queue import OverrideQueue
from agentshore.core.phases import (
    _author_labels_for_config,
    _phase_queue_agent_instantiation,
)
from agentshore.data.models import PlayRecord
from agentshore.plays.base import PlayParams
from agentshore.plays.override import OverrideKind
from agentshore.plays.selector import FixedPlanSelector
from agentshore.rl.constants import STAGNATION_ENTROPY_MULTIPLIER
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    BudgetSnapshot,
    IssueSnapshot,
    OrchestratorState,
    PlayOutcome,
    PlayType,
    SessionState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg() -> RuntimeConfig:
    return RuntimeConfig()


def _idle_outcome(play_type: PlayType) -> PlayOutcome:
    return PlayOutcome(
        play_type=play_type,
        agent_id=None,
        success=True,
        partial=False,
        duration_seconds=0.1,
        token_cost=0,
        dollar_cost=0.0,
        artifacts=[],
        alignment_delta=0.0,
    )


def _idle_agent(agent_id: str = "agent-1") -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CODEX,
        status=AgentStatus.IDLE,
        context_size=10_000,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
        model_tier="medium",
    )


def _open_issue(issue_number: int = 42) -> IssueSnapshot:
    return IssueSnapshot(
        issue_number=issue_number,
        title=f"Issue {issue_number}",
        state="open",
        priority=None,
        labels=[],
        source=None,
    )


def _idle_state_with_issue() -> OrchestratorState:
    return OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[_idle_agent()],
        open_issues=[_open_issue()],
        budget=BudgetSnapshot(
            total_budget=200.0,
            spent=0.0,
            remaining=200.0,
            estimated_cost_per_play=0.1,
        ),
    )


def _idle_state_no_work() -> OrchestratorState:
    return OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        budget=BudgetSnapshot(
            total_budget=200.0,
            spent=0.0,
            remaining=200.0,
            estimated_cost_per_play=0.1,
        ),
    )


def _terminal_state_no_work_with_agent() -> OrchestratorState:
    graph = MagicMock()
    graph.has_epics = True
    graph.has_ready_tasks = False
    graph.tasks = ()
    graph.tasks_ready = 0
    graph.global_closure_ratio = 1.0
    return OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=60,
        total_cost=0.0,
        agents=[_idle_agent()],
        budget=BudgetSnapshot(
            total_budget=200.0,
            spent=0.0,
            remaining=200.0,
            estimated_cost_per_play=0.1,
        ),
        graph=graph,
        plays_since_last_play_type={
            PlayType.SEED_PROJECT: 0,
            PlayType.DESIGN_AUDIT: 0,
            PlayType.RUN_QA: 0,
        },
        last_play_success_by_type={
            PlayType.SEED_PROJECT: True,
            PlayType.DESIGN_AUDIT: True,
            PlayType.RUN_QA: True,
        },
    )


def test_compute_play_streaks_ignores_unfinished_rows() -> None:
    history = [
        PlayRecord(
            session_id="s1",
            play_type="code_review",
            started_at="2026-01-01T00:00:00+00:00",
            ended_at="2026-01-01T00:01:00+00:00",
            success=True,
        ),
        PlayRecord(
            session_id="s1",
            play_type="unblock_pr",
            started_at="2026-01-01T00:02:00+00:00",
            success=False,
        ),
    ]

    fail_streak, any_streak = SnapshotProjector.compute_play_streaks(history)

    assert fail_streak == 0
    assert any_streak == 1


def test_compute_play_streaks_ignores_override_dispatched_plays() -> None:
    """Bootstrap-recipe overrides queue several instantiate_agent in sequence;
    counting them as a same-type streak would fire spurious loop_detected.

    Regression for desktop-yrr: observed 2026-05-18 on session 3862999e
    (v0.15.2), 4 instantiate_agent overrides triggered streak=6 any_outcome.
    """
    history = [
        PlayRecord(
            session_id="s1",
            play_id=10,
            play_type="instantiate_agent",
            started_at="2026-01-01T00:00:00+00:00",
            ended_at="2026-01-01T00:00:05+00:00",
            success=True,
        ),
        PlayRecord(
            session_id="s1",
            play_id=11,
            play_type="instantiate_agent",
            started_at="2026-01-01T00:00:10+00:00",
            ended_at="2026-01-01T00:00:15+00:00",
            success=True,
        ),
        PlayRecord(
            session_id="s1",
            play_id=12,
            play_type="instantiate_agent",
            started_at="2026-01-01T00:00:20+00:00",
            ended_at="2026-01-01T00:00:25+00:00",
            success=True,
        ),
        PlayRecord(
            session_id="s1",
            play_id=13,
            play_type="instantiate_agent",
            started_at="2026-01-01T00:00:30+00:00",
            ended_at="2026-01-01T00:00:35+00:00",
            success=True,
        ),
    ]
    override_ids = {10, 11, 12, 13}

    fail_streak, any_streak = SnapshotProjector.compute_play_streaks(
        history, override_play_ids=override_ids
    )

    assert fail_streak == 0
    assert any_streak == 0


def test_compute_play_streaks_real_streak_after_override_burst() -> None:
    """An override burst followed by a real PPO-driven same-type streak
    must still register the real streak."""
    history = [
        PlayRecord(
            session_id="s1",
            play_id=10,
            play_type="instantiate_agent",
            started_at="2026-01-01T00:00:00+00:00",
            ended_at="2026-01-01T00:00:05+00:00",
            success=True,
        ),
        PlayRecord(
            session_id="s1",
            play_id=11,
            play_type="instantiate_agent",
            started_at="2026-01-01T00:00:10+00:00",
            ended_at="2026-01-01T00:00:15+00:00",
            success=True,
        ),
        PlayRecord(
            session_id="s1",
            play_id=20,
            play_type="code_review",
            started_at="2026-01-01T00:01:00+00:00",
            ended_at="2026-01-01T00:01:10+00:00",
            success=False,
        ),
        PlayRecord(
            session_id="s1",
            play_id=21,
            play_type="code_review",
            started_at="2026-01-01T00:01:20+00:00",
            ended_at="2026-01-01T00:01:30+00:00",
            success=False,
        ),
        PlayRecord(
            session_id="s1",
            play_id=22,
            play_type="code_review",
            started_at="2026-01-01T00:01:40+00:00",
            ended_at="2026-01-01T00:01:50+00:00",
            success=False,
        ),
    ]

    fail_streak, any_streak = SnapshotProjector.compute_play_streaks(
        history, override_play_ids={10, 11}
    )

    assert fail_streak == 3
    assert any_streak == 3


def test_author_labels_cover_all_agent_types_with_dashboard_colors() -> None:
    # cfg content is irrelevant — all AgentType values are always bootstrapped.
    cfg = RuntimeConfig(agents={})

    labels = _author_labels_for_config(cfg, "agentshore/")

    label_map = dict(labels)
    assert label_map["agentshore/author:claude_code"] == "E07B39"
    assert label_map["agentshore/author:codex"] == "F4D44D"
    assert label_map["agentshore/author:gemini"] == "4285F4"
    assert label_map["agentshore/author:grok"] == "14B8A6"
    assert "agentshore/author:custom_agent" not in label_map


# ---------------------------------------------------------------------------
# bootstrap + __aenter__ creates session row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aenter_creates_session_row(tmp_path: Path) -> None:
    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path)
    async with orch:
        # Query the DB directly to confirm a session row exists
        import aiosqlite

        db_path = tmp_path / ".agentshore" / "agentshore.db"
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT status FROM sessions WHERE session_id = ?",
                (orch._session_id,),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        assert row["status"] == "running"


# ---------------------------------------------------------------------------
# __aexit__ marks session completed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aexit_marks_session_completed(tmp_path: Path) -> None:
    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path)
    sid = orch._session_id
    async with orch:
        pass  # immediate exit

    import aiosqlite

    db_path = tmp_path / ".agentshore" / "agentshore.db"
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT status FROM sessions WHERE session_id = ?", (sid,)) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row["status"] == "completed"


@pytest.mark.asyncio
async def test_bootstrap_clears_in_progress_beads_before_github_fetch(tmp_path: Path) -> None:
    events: list[str] = []

    async def _fake_clear(*, repo_root: Path, sid: str, phase: str) -> int:
        events.append(f"clear:{phase}")
        return 2

    async def _fake_fetch_github(**kwargs: object) -> frozenset[int]:
        events.append("fetch_github")
        return frozenset()

    with (
        patch("agentshore.core.phases._clear_session_scoped_bead_progress", new=_fake_clear),
        patch("agentshore.core.phases._phase_fetch_github", new=_fake_fetch_github),
    ):
        await Orchestrator.bootstrap(
            cfg=_cfg(),
            repo_root=tmp_path,
            selector=FixedPlanSelector([]),
        )

    assert events == ["clear:session_start", "fetch_github"]


@pytest.mark.asyncio
async def test_stop_clears_in_progress_beads_during_shutdown(tmp_path: Path) -> None:
    phases: list[str] = []
    orch = await Orchestrator.bootstrap(
        cfg=_cfg(),
        repo_root=tmp_path,
        selector=FixedPlanSelector([]),
    )

    async def _fake_clear(*, repo_root: Path, sid: str, phase: str) -> int:
        phases.append(phase)
        return 1

    with patch("agentshore.core.phases._clear_session_scoped_bead_progress", new=_fake_clear):
        async with orch:
            pass

    assert phases == ["session_shutdown"]


# ---------------------------------------------------------------------------
# run_until_idle terminates when selector returns None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_until_idle_terminates_on_none_selector(tmp_path: Path) -> None:
    selector = FixedPlanSelector([])  # immediately returns None
    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path, selector=selector)
    async with orch:
        with patch.object(
            orch._state_builder, "build_state", new=AsyncMock(return_value=_idle_state_no_work())
        ):
            await orch.run_until_idle()  # should return without hanging


@pytest.mark.asyncio
async def test_run_until_idle_retries_selector_none_when_work_remains(tmp_path: Path) -> None:
    selector = FixedPlanSelector([])
    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path, selector=selector)
    state = _idle_state_with_issue()
    sleep_calls: list[float] = []

    async def _sleep(delay: float) -> None:
        sleep_calls.append(delay)
        orch.request_stop("test_complete")

    async with orch:
        with (
            patch.object(orch._state_builder, "build_state", new=AsyncMock(return_value=state)),
            patch("agentshore.core.mixins.loop.asyncio.sleep", new=_sleep),
            patch("agentshore.core.helpers._logger.warning") as warning,
        ):
            await orch.run_until_idle()

    assert sleep_calls == [2.0]
    assert any(call.args == ("selector_idle_with_work",) for call in warning.call_args_list)


@pytest.mark.asyncio
async def test_run_until_idle_retries_unchanged_digest_when_work_remains(tmp_path: Path) -> None:
    selector = FixedPlanSelector([])
    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path, selector=selector)
    state = _idle_state_with_issue()
    orch._last_selection_digest = orch._loop.selection_state_digest(state, list(state.agents))
    sleep_calls: list[float] = []

    async def _sleep(delay: float) -> None:
        sleep_calls.append(delay)
        orch.request_stop("test_complete")

    async with orch:
        select = AsyncMock(return_value=None)
        with (
            patch.object(selector, "select", new=select),
            patch.object(orch._state_builder, "build_state", new=AsyncMock(return_value=state)),
            patch("agentshore.core.mixins.loop.asyncio.sleep", new=_sleep),
            patch("agentshore.core.helpers._logger.warning") as warning,
        ):
            await orch.run_until_idle()

    assert sleep_calls == [2.0]
    assert any(call.args == ("selector_idle_with_work",) for call in warning.call_args_list)
    select.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_agent_active_claims_release_after_threshold() -> None:
    from tests.orchestrator_factory import make_test_orchestrator

    claim = SimpleNamespace(agent_id="agent-1", claim_group_id="g1", resource_key="issue:42")
    store = MagicMock()
    store.find_active_work_claims_for_agents = AsyncMock(return_value=[claim])
    store.release_active_work_claims_for_agents = AsyncMock(return_value=1)

    orch = make_test_orchestrator(Path("/tmp"), _cfg(), store=store)
    orch._session_id = "s1"
    orch._state_builder = StateBuilder(
        host=orch,
        runtime=orch._runtime,
        store=store,
        manager=MagicMock(),
        executor=MagicMock(),
        session_id="s1",
        repo_root=Path("/tmp"),
        main_repo=orch._main_repo,
        snapshots=MagicMock(),
        velocity=MagicMock(),
        recovery=MagicMock(),
        overrides=MagicMock(),
    )
    state = OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[_idle_agent("agent-1")],
    )

    await orch._state_builder.release_claims_for_prolonged_idle_agents(state)
    await orch._state_builder.release_claims_for_prolonged_idle_agents(state)

    store.release_active_work_claims_for_agents.assert_not_awaited()

    await orch._state_builder.release_claims_for_prolonged_idle_agents(state)

    store.release_active_work_claims_for_agents.assert_awaited_once_with("s1", ["agent-1"])
    assert orch._state_builder._idle_agent_claim_ticks == {}


@pytest.mark.asyncio
async def test_idle_agent_claim_release_protects_in_flight_claim_group() -> None:
    from tests.orchestrator_factory import make_test_orchestrator

    claim = SimpleNamespace(agent_id="agent-1", claim_group_id="g-stale", resource_key="issue:42")
    store = MagicMock()
    store.find_active_work_claims_for_agents = AsyncMock(return_value=[claim])
    store.release_active_work_claims_for_agents = AsyncMock(return_value=1)

    orch = make_test_orchestrator(Path("/tmp"), _cfg(), store=store)
    orch._session_id = "s1"
    orch._state_builder = StateBuilder(
        host=orch,
        runtime=orch._runtime,
        store=store,
        manager=MagicMock(),
        executor=MagicMock(),
        session_id="s1",
        repo_root=Path("/tmp"),
        main_repo=orch._main_repo,
        snapshots=MagicMock(),
        velocity=MagicMock(),
        recovery=MagicMock(),
        overrides=MagicMock(),
    )
    in_flight_params = PlayParams(agent_id="agent-1", extras={"claim_group_id": "g-live"})
    orch._runtime.dispatch_ctx = {
        "dispatch-live": _DispatchContext(
            dispatch_id="dispatch-live",
            play_type=PlayType.ISSUE_PICKUP,
            params=in_flight_params,
            state_at_dispatch=OrchestratorState(
                session_id="s1",
                session_state=SessionState.RUNNING,
                total_plays=0,
                total_cost=0.0,
            ),
            pending_step=None,
            dispatched_at=0.0,
        )
    }
    state = OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[_idle_agent("agent-1")],
    )

    for _ in range(3):
        await orch._state_builder.release_claims_for_prolonged_idle_agents(state)

    store.release_active_work_claims_for_agents.assert_awaited_once_with(
        "s1",
        ["agent-1"],
        exclude_claim_group_ids={"g-live"},
    )
    assert orch._state_builder._idle_agent_claim_ticks == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("play_type", [PlayType.RUN_QA, PlayType.DESIGN_AUDIT])
async def test_audit_play_completion_forces_issue_refresh(
    tmp_path: Path, play_type: PlayType
) -> None:
    selector = FixedPlanSelector([(play_type, PlayParams(bypass_preconditions=True))])
    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path, selector=selector)
    outcome = _idle_outcome(play_type)
    refresh_issues = AsyncMock()

    async with orch:
        orch._last_refresh_time = time.monotonic()
        with (
            patch.object(orch._completion, "refresh_issues", new=refresh_issues),
            patch.object(
                orch._state_builder,
                "build_state",
                new=AsyncMock(return_value=_idle_state_no_work()),
            ),
            patch.object(orch._executor, "execute", new=AsyncMock(return_value=outcome)),
        ):
            await orch.run_until_idle()

    refresh_issues.assert_awaited_once()


@pytest.mark.asyncio
async def test_end_session_revalidation_blocks_when_refresh_finds_work(
    tmp_path: Path,
) -> None:
    selector = FixedPlanSelector([])
    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path, selector=selector)

    async with orch:
        with (
            patch.object(orch._completion, "refresh_issues", new=AsyncMock()) as refresh_issues,
            patch.object(
                orch._state_builder,
                "build_state",
                new=AsyncMock(return_value=_idle_state_with_issue()),
            ),
            patch("agentshore.core.helpers._logger.warning") as warning,
        ):
            allowed = await orch._dispatcher.revalidate_end_session_before_dispatch()

    assert allowed is False
    refresh_issues.assert_awaited_once()
    assert any(
        call.args == ("end_session_revalidation_blocked",) for call in warning.call_args_list
    )
    assert orch._last_selection_digest is None


@pytest.mark.asyncio
async def test_run_until_idle_does_not_dispatch_stale_end_session(tmp_path: Path) -> None:
    selector = MagicMock()
    # Eligibility refactor: the loop drains the selector's confirm-repick tally
    # once per cycle via consume_repick_count(). Return a real int so
    # _record_selection_repicks doesn't choke on a MagicMock comparison.
    selector.consume_repick_count = MagicMock(return_value=0)
    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path, selector=selector)
    calls = 0

    async def _select(_state: OrchestratorState) -> tuple[PlayType, PlayParams] | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return (PlayType.END_SESSION, PlayParams())
        orch.request_stop("test_complete")
        return None

    selector.select = AsyncMock(side_effect=_select)

    async with orch:
        orch._last_refresh_time = time.monotonic()
        with (
            patch.object(
                orch._state_builder,
                "build_state",
                new=AsyncMock(return_value=_idle_state_no_work()),
            ),
            patch.object(
                orch._dispatcher,
                "revalidate_end_session_before_dispatch",
                new=AsyncMock(return_value=False),
            ) as revalidate,
            patch.object(orch._dispatcher, "dispatch_play", new=AsyncMock()) as dispatch,
        ):
            await orch.run_until_idle()

    revalidate.assert_awaited_once()
    dispatch.assert_not_awaited()


# Removed: test_dispatch_revalidation_blocks_fresh_cooldown_without_play_row.
#
# Eligibility refactor: the dispatch-time revalidation pass (``revalidate=True``
# → ``_dispatch_revalidation_reason`` → ``dispatch_revalidation_block`` mutation)
# was deleted. ``_dispatch_play`` is now purely side-effecting; play validity
# (including cooldown gates) is settled upstream by ``EligibilityAuthority`` —
# the action mask presents only valid plays and ``confirm()`` rejects live
# drift with a clean re-pick (no plays-table skip row, no RL sample). The
# clean-re-pick contract is covered by test_confirm_live_drift_is_clean_repick
# and the parallel-dispatch drained-pool test in tests/test_rl_selector.py.


@pytest.mark.asyncio
async def test_run_until_idle_dispatches_single_end_session(tmp_path: Path) -> None:
    selector = FixedPlanSelector(
        [
            (PlayType.END_SESSION, PlayParams()),
            (PlayType.END_SESSION, PlayParams()),
        ]
    )
    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path, selector=selector)
    end_outcome = _idle_outcome(PlayType.END_SESSION)

    async def _execute(
        _play_type: PlayType, _state: OrchestratorState, *, override: PlayParams
    ) -> PlayOutcome:
        await asyncio.sleep(0.05)
        return end_outcome

    async with orch:
        orch._last_refresh_time = time.monotonic()
        with (
            patch.object(orch._completion, "refresh_issues", new=AsyncMock()),
            patch.object(
                orch._state_builder,
                "build_state",
                new=AsyncMock(return_value=_terminal_state_no_work_with_agent()),
            ),
            patch.object(orch._executor, "execute", new=AsyncMock(side_effect=_execute)) as execute,
            patch("agentshore.core.mixins.loop.AGENT_PING_TIMEOUT_SECONDS", 0.001),
        ):
            await asyncio.wait_for(orch.run_until_idle(), timeout=5.0)

    assert execute.await_count == 1


# ---------------------------------------------------------------------------
# run_until_idle terminates on END_SESSION play
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_until_idle_terminates_on_end_session(tmp_path: Path) -> None:
    plan = [(PlayType.END_SESSION, PlayParams())]
    selector = FixedPlanSelector(plan)

    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path, selector=selector)

    end_outcome = _idle_outcome(PlayType.END_SESSION)

    async with orch:
        orch._last_refresh_time = time.monotonic()
        with (
            patch.object(orch._completion, "refresh_issues", new=AsyncMock()),
            patch.object(
                orch._state_builder,
                "build_state",
                new=AsyncMock(return_value=_terminal_state_no_work_with_agent()),
            ),
            patch.object(orch._executor, "execute", new=AsyncMock(return_value=end_outcome)),
        ):
            await orch.run_until_idle()

    # Just verifying it terminates and session is completed


@pytest.mark.asyncio
async def test_closed_graph_does_not_auto_dispatch_end_session(tmp_path: Path) -> None:
    graph = MagicMock()
    graph.has_epics = True
    graph.has_ready_tasks = False
    graph.tasks = ()
    graph.tasks_ready = 0
    graph.global_closure_ratio = 1.0

    selector = MagicMock()
    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path, selector=selector)

    async def _select_once(_state: OrchestratorState) -> None:
        orch._stop_requested = True
        return None

    selector.select = AsyncMock(side_effect=_select_once)

    async def _fake_state() -> OrchestratorState:
        return OrchestratorState(
            session_id=orch._session_id,
            session_state=SessionState.RUNNING,
            total_plays=0,
            total_cost=0.0,
            agents=[_idle_agent("agent-1")],
            budget=BudgetSnapshot(
                total_budget=200.0,
                spent=0.0,
                remaining=200.0,
                estimated_cost_per_play=0.1,
            ),
            graph=graph,
            last_play_success_by_type={PlayType.SEED_PROJECT: True},
            plays_since_last_play_type={PlayType.SEED_PROJECT: 0},
        )

    orch._state_builder.build_state = AsyncMock(side_effect=_fake_state)
    orch._completion.refresh_issues = AsyncMock()
    orch._drain.generate_end_session_report = AsyncMock(return_value=tmp_path / "esr.html")
    orch._loop.idle_backoff = MagicMock(return_value=0.0)

    await orch.__aenter__()
    try:
        await asyncio.wait_for(orch.run_until_idle(), timeout=5.0)

        history = await orch._store.get_play_history(orch._session_id)
        end_session_rows = [p for p in history if p.play_type == PlayType.END_SESSION.value]
        assert end_session_rows == []
        assert orch._end_session_report_requested is False
        assert orch._drain_reason is None
        selector.select.assert_awaited()
    finally:
        orch._end_session_report_open_browser = False
        await orch.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# run_until_idle starts budget reserve drain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_until_idle_begins_drain_on_budget_reserve(tmp_path: Path) -> None:
    selector = MagicMock()
    selector.select = AsyncMock(return_value=(PlayType.ISSUE_PICKUP, PlayParams()))

    cfg = _cfg()
    orch = await Orchestrator.bootstrap(cfg=cfg, repo_root=tmp_path, selector=selector)

    # Override build_state to return a state inside the final $5 budget reserve.
    from agentshore.state import BudgetSnapshot

    reserve_budget = BudgetSnapshot(
        total_budget=20.0, spent=15.0, remaining=5.0, estimated_cost_per_play=0.05
    )

    async def _fake_state() -> OrchestratorState:
        return OrchestratorState(
            session_id=orch._session_id,
            session_state=SessionState.DRAINING if orch._draining else SessionState.RUNNING,
            total_plays=10,
            total_cost=15.0,
            budget=reserve_budget,
        )

    async with orch:
        with patch.object(orch._state_builder, "build_state", new=_fake_state):
            await orch.run_until_idle()

    assert orch._draining is True
    assert orch._drain_reason == "budget_reserve_reached"
    selector.select.assert_not_called()


@pytest.mark.asyncio
async def test_instantiate_override_queue_dequeues_with_in_flight_work(
    tmp_path: Path,
) -> None:
    selector = MagicMock()
    selector.select = AsyncMock(return_value=None)
    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path, selector=selector)

    busy_state = OrchestratorState(
        session_id=orch._session_id,
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[
            AgentSnapshot(
                agent_id="busy-1",
                agent_type=AgentType.CODEX,
                status=AgentStatus.BUSY,
                context_size=10_000,
                total_cost=0.0,
                total_tokens=0,
                tasks_completed=0,
                tasks_failed=0,
                model_tier="medium",
            )
        ],
        open_issues=[_open_issue()],
        budget=BudgetSnapshot(
            total_budget=200.0,
            spent=0.0,
            remaining=200.0,
            estimated_cost_per_play=0.1,
        ),
    )

    async with orch:
        orch._in_flight["dispatch-1"] = asyncio.create_task(asyncio.sleep(5))
        from agentshore.plays.override import OverrideEntry, OverrideKind

        orch._overrides.put_nowait(
            OverrideEntry(
                play_type=PlayType.INSTANTIATE_AGENT,
                params=PlayParams(bypass_preconditions=True),
                kind=OverrideKind.BOOTSTRAP,
            )
        )

        async def _wait_once(*_args: object, **_kwargs: object) -> None:
            for task in orch._in_flight.values():
                task.cancel()
            orch._in_flight.clear()

        with (
            patch.object(orch._completion, "refresh_issues", new=AsyncMock()),
            patch.object(
                orch._state_builder, "build_state", new=AsyncMock(return_value=busy_state)
            ),
            patch.object(orch._dispatcher, "dispatch_play", new=AsyncMock(return_value=False)),
            patch.object(
                orch._completion, "wait_for_in_flight", new=AsyncMock(side_effect=_wait_once)
            ),
            patch.object(
                orch._loop,
                "continue_if_selector_idle_work_remains",
                new=AsyncMock(return_value=False),
            ),
            patch("agentshore.core.helpers._logger.info") as info_log,
        ):
            await orch.run_until_idle()

    names = [call.args[0] for call in info_log.call_args_list if call.args]
    assert "override_queue_dequeued" in names


@pytest.mark.asyncio
async def test_instantiate_selector_pick_dispatches_with_in_flight_work(
    tmp_path: Path,
) -> None:
    selector = MagicMock()
    selector.select = AsyncMock(return_value=(PlayType.INSTANTIATE_AGENT, PlayParams()))
    # Eligibility refactor: the loop drains confirm-repicks once per cycle via
    # consume_repick_count(); return a real int so _record_selection_repicks
    # doesn't crash on a MagicMock comparison.
    selector.consume_repick_count = MagicMock(return_value=0)
    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path, selector=selector)

    busy_state = OrchestratorState(
        session_id=orch._session_id,
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[
            AgentSnapshot(
                agent_id="busy-1",
                agent_type=AgentType.CODEX,
                status=AgentStatus.BUSY,
                context_size=10_000,
                total_cost=0.0,
                total_tokens=0,
                tasks_completed=0,
                tasks_failed=0,
                model_tier="medium",
            )
        ],
        open_issues=[_open_issue()],
        budget=BudgetSnapshot(
            total_budget=200.0,
            spent=0.0,
            remaining=200.0,
            estimated_cost_per_play=0.1,
        ),
    )

    async with orch:
        orch._in_flight["dispatch-1"] = asyncio.create_task(asyncio.sleep(5))
        dispatch_mock = AsyncMock(return_value=False)

        async def _wait_once(*_args: object, **_kwargs: object) -> None:
            for task in orch._in_flight.values():
                task.cancel()
            orch._in_flight.clear()

        with (
            patch.object(orch._completion, "refresh_issues", new=AsyncMock()),
            patch.object(
                orch._state_builder, "build_state", new=AsyncMock(return_value=busy_state)
            ),
            patch.object(orch._dispatcher, "dispatch_play", new=dispatch_mock),
            patch.object(
                orch._completion, "wait_for_in_flight", new=AsyncMock(side_effect=_wait_once)
            ),
            patch.object(
                orch._loop,
                "continue_if_selector_idle_work_remains",
                new=AsyncMock(return_value=False),
            ),
        ):
            await orch.run_until_idle()

    dispatch_mock.assert_awaited()
    first_play_type = dispatch_mock.await_args_list[0].args[0]
    assert first_play_type == PlayType.INSTANTIATE_AGENT


# ---------------------------------------------------------------------------
# _on_crash logs without recovering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_crash_logs_without_recovering(tmp_path: Path) -> None:
    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path)
    async with orch:
        # Should not raise; should just log
        await orch._completion.on_crash("agent-123", 1)
        # No handles were created so there's nothing to verify beyond no exception


# ---------------------------------------------------------------------------
# _on_context_pressure annotates context_pressure_hints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_context_pressure_annotates_hints(tmp_path: Path) -> None:
    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path)
    async with orch:
        await orch._completion.on_context_pressure("agent-42", 0.87)
        assert orch.context_pressure_hints.get("agent-42") == pytest.approx(0.87)


# ---------------------------------------------------------------------------
# stop() is idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_is_idempotent(tmp_path: Path) -> None:
    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path)
    async with orch:
        await orch.stop()
        await orch.stop()  # second call should be a no-op, not raise


# ---------------------------------------------------------------------------
# KeyboardInterrupt during run_until_idle exits cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keyboard_interrupt_exits_cleanly(tmp_path: Path) -> None:
    selector = MagicMock()
    selector.select = AsyncMock(side_effect=KeyboardInterrupt)

    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path, selector=selector)

    with pytest.raises(KeyboardInterrupt):
        async with orch:
            await orch.run_until_idle()


# ---------------------------------------------------------------------------
# stop() wakes a paused loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_wakes_paused_loop(tmp_path: Path) -> None:
    """stop() should wake a paused orchestrator so it can exit."""
    import asyncio

    selector = FixedPlanSelector(
        [(PlayType.ISSUE_PICKUP, PlayParams(bypass_preconditions=True))] * 10
    )
    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path, selector=selector)

    # Mock executor so plays don't actually run
    end_outcome = _idle_outcome(PlayType.ISSUE_PICKUP)
    paused = asyncio.Event()

    async def mock_execute(
        pt: PlayType,
        state: OrchestratorState,
        override: PlayParams | None = None,
    ) -> PlayOutcome:
        # Pause after first play
        await orch.pause("test")
        paused.set()
        return end_outcome

    async with orch:
        with patch.object(orch._executor, "execute", new=mock_execute):
            task = asyncio.create_task(orch.run_until_idle())
            # Wait deterministically until the loop has actually paused rather
            # than sleeping a fixed interval (the prior wall-clock wait raced
            # under xdist load, #13).
            await asyncio.wait_for(paused.wait(), timeout=5.0)
            assert not orch._pause_event.is_set()
            # stop() should wake it
            await orch.stop()
            await asyncio.wait_for(task, timeout=5.0)
            # Should have exited cleanly
            assert task.done()


# ---------------------------------------------------------------------------
# Error-recovery: executor raises — loop continues, doesn't crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_exception_does_not_crash_loop(tmp_path: Path) -> None:
    """When execute() raises, _process_completion logs and loop continues."""
    import asyncio

    plan = [
        (PlayType.ISSUE_PICKUP, PlayParams(bypass_preconditions=True)),
        (PlayType.END_SESSION, PlayParams(bypass_preconditions=True)),
    ]
    selector = FixedPlanSelector(plan)
    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path, selector=selector)

    call_count = 0
    end_outcome = _idle_outcome(PlayType.END_SESSION)

    async def mock_execute(
        pt: PlayType,
        state: OrchestratorState,
        override: PlayParams | None = None,
    ) -> PlayOutcome:
        nonlocal call_count
        call_count += 1
        if pt == PlayType.ISSUE_PICKUP:
            msg = "simulated agent failure"
            raise RuntimeError(msg)
        return end_outcome

    async with orch:
        orch._last_refresh_time = time.monotonic()
        with (
            patch.object(orch._completion, "refresh_issues", new=AsyncMock()),
            patch.object(
                orch._state_builder,
                "build_state",
                new=AsyncMock(return_value=_idle_state_no_work()),
            ),
            patch.object(orch._executor, "execute", new=mock_execute),
        ):
            await asyncio.wait_for(orch.run_until_idle(), timeout=5.0)

    # Both plays were attempted (failure didn't crash the loop)
    assert call_count == 2


# ---------------------------------------------------------------------------
# _process_completion handles cancelled task without crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_completion_handles_cancelled_task(tmp_path: Path) -> None:
    """_process_completion called with a cancelled task should not raise."""
    import asyncio

    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path)

    async with orch:
        # Create a cancelled task
        async def _noop() -> PlayOutcome:
            return _idle_outcome(PlayType.ISSUE_PICKUP)

        import contextlib

        task: asyncio.Task[PlayOutcome] = asyncio.create_task(_noop())
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # Should not raise even with cancelled task and missing dispatch context
        await orch._completion.process_completion("nonexistent-dispatch-id", task)


# ---------------------------------------------------------------------------
# Bootstrap recipe: small-Claude included when tier is enabled
# ---------------------------------------------------------------------------


def _make_mock_orch() -> MagicMock:
    """Minimal mock Orchestrator with an _override_queue."""
    orch = MagicMock()
    orch._overrides = OverrideQueue()
    return orch


def test_bootstrap_recipe_queues_configured_large_then_different_medium() -> None:
    cfg = RuntimeConfig(
        agents={
            AgentType.CODEX.value: AgentConfig(enabled=True),
            AgentType.CLAUDE_CODE.value: AgentConfig(enabled=True),
        }
    )
    orch = _make_mock_orch()
    _phase_queue_agent_instantiation(orch=orch, cfg=cfg, seed_path=Path("/tmp/seed.md"))

    entries = []
    while not orch._overrides.empty():
        entries.append(orch._overrides.get_nowait())

    assert [(e.play_type, e.params) for e in entries] == [
        (
            PlayType.INSTANTIATE_AGENT,
            PlayParams(
                target_agent_type=AgentType.CODEX.value,
                target_model_tier="large",
                bypass_preconditions=True,
            ),
        ),
        (
            PlayType.SEED_PROJECT,
            PlayParams(seed_path=str(Path("/tmp/seed.md")), bypass_preconditions=True),
        ),
        (
            PlayType.INSTANTIATE_AGENT,
            PlayParams(
                target_agent_type=AgentType.CLAUDE_CODE.value,
                target_model_tier="medium",
                bypass_preconditions=True,
            ),
        ),
        (PlayType.GROOM_BACKLOG, PlayParams(bypass_preconditions=True)),
    ]
    # Groom waits for the seed audit to complete before releasing.
    assert entries[3].wait_for_play_type == PlayType.SEED_PROJECT


def test_bootstrap_recipe_uses_next_large_when_claude_large_disabled() -> None:
    cfg = RuntimeConfig(
        agents={
            AgentType.CLAUDE_CODE.value: AgentConfig(
                enabled=True,
                model_tiers={
                    "medium": ModelTierConfig(enabled=True),
                    "large": ModelTierConfig(enabled=False),
                },
            ),
            AgentType.CODEX.value: AgentConfig(enabled=True),
        }
    )
    orch = _make_mock_orch()
    _phase_queue_agent_instantiation(orch=orch, cfg=cfg, seed_path=Path("/tmp/seed.md"))

    entries = []
    while not orch._overrides.empty():
        entries.append(orch._overrides.get_nowait())

    assert (entries[0].play_type, entries[0].params) == (
        PlayType.INSTANTIATE_AGENT,
        PlayParams(
            target_agent_type=AgentType.CODEX.value,
            target_model_tier="large",
            bypass_preconditions=True,
        ),
    )
    assert entries[1].play_type == PlayType.SEED_PROJECT
    assert (entries[2].play_type, entries[2].params) == (
        PlayType.INSTANTIATE_AGENT,
        PlayParams(
            target_agent_type=AgentType.CLAUDE_CODE.value,
            target_model_tier="medium",
            bypass_preconditions=True,
        ),
    )
    assert (entries[3].play_type, entries[3].params) == (
        PlayType.GROOM_BACKLOG,
        PlayParams(bypass_preconditions=True),
    )
    assert entries[3].wait_for_play_type == PlayType.SEED_PROJECT


def test_bootstrap_recipe_skips_medium_when_no_different_backend_available() -> None:
    cfg = RuntimeConfig(
        agents={
            AgentType.CLAUDE_CODE.value: AgentConfig(
                enabled=True,
                model_tiers={
                    "medium": ModelTierConfig(enabled=True),
                    "large": ModelTierConfig(enabled=True),
                },
            ),
        }
    )
    orch = _make_mock_orch()
    _phase_queue_agent_instantiation(orch=orch, cfg=cfg, seed_path=Path("/tmp/seed.md"))

    entries = []
    while not orch._overrides.empty():
        entries.append(orch._overrides.get_nowait())

    assert [(e.play_type, e.params) for e in entries] == [
        (
            PlayType.INSTANTIATE_AGENT,
            PlayParams(
                target_agent_type=AgentType.CLAUDE_CODE.value,
                target_model_tier="large",
                bypass_preconditions=True,
            ),
        ),
        (
            PlayType.SEED_PROJECT,
            PlayParams(seed_path=str(Path("/tmp/seed.md")), bypass_preconditions=True),
        ),
        (PlayType.GROOM_BACKLOG, PlayParams(bypass_preconditions=True)),
    ]
    # Even with no medium spawn, groom still runs after the seed audit.
    assert entries[2].wait_for_play_type == PlayType.SEED_PROJECT


@pytest.mark.asyncio
async def test_stagnation_escalation_ladder_and_reset(tmp_path: Path) -> None:
    cfg = _cfg()
    orch = await Orchestrator.bootstrap(cfg=cfg, repo_root=tmp_path)
    async with orch:
        state = OrchestratorState(
            session_id=orch._session_id,
            session_state=SessionState.RUNNING,
            total_plays=0,
            total_cost=0.0,
        )
        orch._metrics = SimpleNamespace(
            snapshot=AsyncMock(return_value=SimpleNamespace(stagnation_counter=1))
        )
        assert await orch._check_stagnation_escalation(state) is False
        assert orch._loop._last_stagnation_stage == 1

        orch._metrics = SimpleNamespace(
            snapshot=AsyncMock(return_value=SimpleNamespace(stagnation_counter=3))
        )
        assert await orch._check_stagnation_escalation(state) is True
        assert orch._loop._last_stagnation_stage == 2

        orch._metrics = SimpleNamespace(
            snapshot=AsyncMock(return_value=SimpleNamespace(stagnation_counter=5))
        )
        assert await orch._check_stagnation_escalation(state) is True
        assert orch._loop._last_stagnation_stage == 3

        orch._metrics = SimpleNamespace(
            snapshot=AsyncMock(return_value=SimpleNamespace(stagnation_counter=0))
        )
        assert await orch._check_stagnation_escalation(state) is False
        assert orch._loop._last_stagnation_stage == 0


@pytest.mark.asyncio
async def test_stagnation_stage_one_boosts_entropy_coef(tmp_path: Path) -> None:
    cfg = _cfg()
    orch = await Orchestrator.bootstrap(cfg=cfg, repo_root=tmp_path)
    async with orch:
        state = OrchestratorState(
            session_id=orch._session_id,
            session_state=SessionState.RUNNING,
            total_plays=0,
            total_cost=0.0,
        )
        orch._metrics = SimpleNamespace(
            snapshot=AsyncMock(return_value=SimpleNamespace(stagnation_counter=1))
        )

        class _DummySelector:
            def __init__(self) -> None:
                self.values: list[float] = []

            def set_entropy_coef(self, value: float) -> None:
                self.values.append(value)

        dummy = _DummySelector()
        orch._selector = dummy  # type: ignore[assignment]
        with patch("agentshore.core.mixins.loop._ppo_selector_cls", return_value=_DummySelector):
            await orch._check_stagnation_escalation(state)
            orch._metrics = SimpleNamespace(
                snapshot=AsyncMock(return_value=SimpleNamespace(stagnation_counter=0))
            )
            await orch._check_stagnation_escalation(state)

        assert dummy.values[0] == pytest.approx(
            cfg.rl.entropy_coef * STAGNATION_ENTROPY_MULTIPLIER, abs=1e-6
        )
        assert dummy.values[-1] == pytest.approx(cfg.rl.entropy_coef, abs=1e-6)


def test_bootstrap_recipe_order() -> None:
    """Bootstrap queues a large seed agent, seed_project, then a different medium agent."""
    cfg = RuntimeConfig(
        agents={
            AgentType.CLAUDE_CODE.value: AgentConfig(enabled=True),
            AgentType.CODEX.value: AgentConfig(enabled=True),
        }
    )
    orch = _make_mock_orch()
    _phase_queue_agent_instantiation(orch=orch, cfg=cfg, seed_path=Path("/tmp/seed.md"))

    entries = []
    while not orch._overrides.empty():
        entries.append(orch._overrides.get_nowait())

    play_types = [e.play_type for e in entries]
    assert play_types[0] == PlayType.INSTANTIATE_AGENT
    assert entries[0].params.target_agent_type == AgentType.CLAUDE_CODE.value
    assert entries[0].params.target_model_tier == "large"
    assert play_types[1] == PlayType.SEED_PROJECT
    assert play_types[2] == PlayType.INSTANTIATE_AGENT
    assert entries[2].params.target_agent_type == AgentType.CODEX.value
    assert entries[2].params.target_model_tier == "medium"
    assert entries[2].params.bypass_preconditions is True
    assert play_types[3] == PlayType.GROOM_BACKLOG
    assert entries[3].params.bypass_preconditions is True
    assert entries[3].wait_for_play_type == PlayType.SEED_PROJECT
    assert len(entries) == 4


# ---------------------------------------------------------------------------
# Bootstrap recipe: seed vs cleanup decision (desktop-65mq + desktop-arph)
# ---------------------------------------------------------------------------


def _bootstrap_cfg(*, cleanup_threshold: int = 50) -> RuntimeConfig:
    return RuntimeConfig(
        agents={
            AgentType.CLAUDE_CODE.value: AgentConfig(enabled=True),
            AgentType.CODEX.value: AgentConfig(enabled=True),
        },
        bootstrap=BootstrapConfig(cleanup_threshold=cleanup_threshold),
    )


def test_bootstrap_first_play_is_seed_when_seed_path_provided(tmp_path: Path) -> None:
    """An explicit --seed input always wins, regardless of backlog size."""
    cfg = _bootstrap_cfg()
    orch = _make_mock_orch()
    seed_file = tmp_path / "seed.md"
    seed_file.write_text("# Seed", encoding="utf-8")
    # High issue count would otherwise route to cleanup; seed_path overrides.
    _phase_queue_agent_instantiation(orch=orch, cfg=cfg, seed_path=seed_file, open_issues_count=500)

    entries = []
    while not orch._overrides.empty():
        entries.append(orch._overrides.get_nowait())

    assert [e.play_type for e in entries] == [
        PlayType.INSTANTIATE_AGENT,
        PlayType.SEED_PROJECT,
        PlayType.INSTANTIATE_AGENT,
        PlayType.GROOM_BACKLOG,
    ]
    assert entries[1].params.seed_path == str(seed_file)
    assert entries[1].params.bypass_preconditions is True
    assert entries[3].wait_for_play_type == PlayType.SEED_PROJECT


def test_bootstrap_open_start_queues_instantiate_then_groom_without_seed() -> None:
    """#11: without a seed, open-start queues one large-tier INSTANTIATE_AGENT
    cold-start backstop followed by a GROOM_BACKLOG pass.

    The mask zeroes INSTANTIATE_AGENT for a zero-agent / no-remaining-work /
    non-terminal fleet, so the prior no-op design deadlocked at 0 agents. One
    forced spawn breaks the catch-22; groom then reconciles the beads↔GitHub
    graph before PPO takes over. PPO still owns all subsequent fleet growth (no
    medium spawn, no SEED_PROJECT).
    """
    cfg = _bootstrap_cfg()
    orch = _make_mock_orch()
    _phase_queue_agent_instantiation(
        orch=orch, cfg=cfg, seed_path=None, open_issues_count=120, graph_has_epics=True
    )

    entries = []
    while not orch._overrides.empty():
        entries.append(orch._overrides.get_nowait())

    assert len(entries) == 2
    assert [e.play_type for e in entries] == [
        PlayType.INSTANTIATE_AGENT,
        PlayType.GROOM_BACKLOG,
    ]
    assert entries[0].params == PlayParams(
        target_agent_type=AgentType.CLAUDE_CODE.value,
        target_model_tier="large",
        bypass_preconditions=True,
    )
    assert entries[0].kind is OverrideKind.BOOTSTRAP
    # Groom bypasses the warmup floor / beads gate so it runs immediately at
    # bootstrap. As the first (and only) agent-consumer it carries NO wait_for
    # gate — it claims the agent by queue position (mirroring SEED_PROJECT in
    # the seed recipe) so PPO can't free-select onto the idle agent on a None
    # override tick and starve groom of staffing.
    assert entries[1].params == PlayParams(bypass_preconditions=True)
    assert entries[1].kind is OverrideKind.BOOTSTRAP
    assert entries[1].wait_for_play_type is None


def test_bootstrap_open_start_no_epics_routes_to_seed_recipe() -> None:
    """No seed input AND no epics → run the seedless seed recipe, not open-start.

    Grooming an epic-less graph has nothing to reconcile and fails, so the
    no-epic case must create epics first via a seedless SEED_PROJECT (its
    precondition carve-out makes it eligible exactly when the graph is empty).
    This is the deadlock fix: instantiate → seed (seedless) → instantiate →
    groom, identical to the explicit-seed recipe but with seed_path=None.
    """
    cfg = _bootstrap_cfg()
    orch = _make_mock_orch()
    _phase_queue_agent_instantiation(
        orch=orch, cfg=cfg, seed_path=None, open_issues_count=0, graph_has_epics=False
    )

    entries = []
    while not orch._overrides.empty():
        entries.append(orch._overrides.get_nowait())

    assert [e.play_type for e in entries] == [
        PlayType.INSTANTIATE_AGENT,
        PlayType.SEED_PROJECT,
        PlayType.INSTANTIATE_AGENT,
        PlayType.GROOM_BACKLOG,
    ]
    # Seedless: SEED_PROJECT runs with no seed document.
    assert entries[1].params.seed_path is None
    assert entries[1].params.bypass_preconditions is True
    # Groom still waits for the (seedless) seed audit to complete.
    assert entries[3].wait_for_play_type == PlayType.SEED_PROJECT


def test_bootstrap_open_start_ignores_backlog_size() -> None:
    """Open-start queues the same instantiate→groom recipe regardless of backlog
    size — no backlog-driven forced fleet either way."""
    cfg = _bootstrap_cfg(cleanup_threshold=50)
    orch = _make_mock_orch()
    _phase_queue_agent_instantiation(
        orch=orch, cfg=cfg, seed_path=None, open_issues_count=10, graph_has_epics=True
    )

    entries = []
    while not orch._overrides.empty():
        entries.append(orch._overrides.get_nowait())

    assert [e.play_type for e in entries] == [
        PlayType.INSTANTIATE_AGENT,
        PlayType.GROOM_BACKLOG,
    ]
    assert entries[0].params.target_model_tier == "large"
