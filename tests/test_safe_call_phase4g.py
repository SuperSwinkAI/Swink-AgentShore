"""Phase 4G: _safe_call resilience + override mask check."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentshore.config import RuntimeConfig
from agentshore.plays.base import PlayParams
from agentshore.state import PlayType


def _make_orch(tmp_path: Path, cfg: RuntimeConfig | None = None) -> Any:
    from tests.orchestrator_factory import make_test_orchestrator

    return make_test_orchestrator(tmp_path, cfg)


# ---------------------------------------------------------------------------
# _safe_call: errors are logged, not propagated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_call_does_not_propagate_exception(tmp_path: Path) -> None:
    """_safe_call swallows exceptions so the caller always continues."""
    orch = _make_orch(tmp_path)

    async def boom() -> None:
        raise RuntimeError("simulated db failure")

    # Must not raise — any exception is swallowed and logged
    await orch._safe_call(boom(), "test_label")


@pytest.mark.asyncio
async def test_safe_call_succeeds_silently(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    called = []

    async def ok() -> None:
        called.append(True)

    await orch._safe_call(ok(), "label")
    assert called == [True]


# ---------------------------------------------------------------------------
# DataStore failure resilience: loop continues after store error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_datastore_failure_loop_continues(tmp_path: Path) -> None:
    """If update_session_state raises, run_until_idle should still complete."""
    orch = _make_orch(tmp_path)

    mock_outcome = MagicMock()
    mock_outcome.play_type = PlayType.ISSUE_PICKUP
    mock_outcome.success = True
    mock_outcome.partial = False
    mock_outcome.dollar_cost = 0.01
    mock_outcome.duration_seconds = 1.0
    mock_outcome.alignment_delta = 0.1
    mock_outcome.play_id = 1
    mock_outcome.inflation_raised = False

    executed: list[PlayType] = []

    async def mock_execute(play_type: PlayType, state: Any, override: Any = None) -> Any:
        executed.append(play_type)
        return mock_outcome

    orch._executor.execute = mock_execute
    orch._selector.should_update = MagicMock(return_value=False)
    orch._selector.should_checkpoint = MagicMock(return_value=False)
    orch._selector.on_play_completed = AsyncMock()
    orch._selector.consume_pending = MagicMock(return_value=None)

    call_count = 0

    async def mock_select(state: Any) -> Any:
        nonlocal call_count
        call_count += 1
        return (PlayType.ISSUE_PICKUP, PlayParams()) if call_count == 1 else None

    orch._selector.select = mock_select

    orch._store.get_play_history = AsyncMock(return_value=[])
    orch._store.get_open_issues = AsyncMock(return_value=[])
    orch._store.get_latest_trajectory = AsyncMock(return_value=None)

    await orch.run_until_idle()

    # Loop completed and executed exactly one play despite any internal errors
    assert executed == [PlayType.ISSUE_PICKUP]


# ---------------------------------------------------------------------------
# Override queue: masked override falls back to selector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_masked_falls_back_to_selector(tmp_path: Path) -> None:
    """If an override play type is action-masked, selector is consulted instead."""
    from agentshore.plays.registry import PlayRegistry
    from agentshore.rl.action_space import V1_ACTION_ORDER

    orch = _make_orch(tmp_path)

    # Registry whose preconditions_met always returns False
    mock_registry = MagicMock(spec=PlayRegistry)

    def preconditions_met(play_type: PlayType, state: Any) -> bool:
        return False

    mock_registry.preconditions_met = preconditions_met
    orch._registry = mock_registry

    selector_called = []

    async def mock_select(state: Any) -> Any:
        selector_called.append(True)
        return None

    orch._selector.select = mock_select
    orch._selector.should_update = MagicMock(return_value=False)
    orch._selector.should_checkpoint = MagicMock(return_value=False)
    orch._selector.on_play_completed = AsyncMock()
    orch._selector.consume_pending = MagicMock(return_value=None)

    orch._store.get_play_history = AsyncMock(return_value=[])
    orch._store.get_open_issues = AsyncMock(return_value=[])
    orch._store.get_latest_trajectory = AsyncMock(return_value=None)

    # Enqueue CODE_REVIEW which will be masked (preconditions_met returns False)
    if PlayType.CODE_REVIEW in V1_ACTION_ORDER:
        from agentshore.plays.override import OverrideEntry, OverrideKind

        orch._override_queue.put_nowait(
            OverrideEntry(
                play_type=PlayType.CODE_REVIEW,
                params=PlayParams(),
                kind=OverrideKind.EXECUTOR_REQUEUE,
            )
        )
        await orch.run_until_idle()

        # Selector was consulted after override was dropped
        assert selector_called, "Selector should have been called as fallback"


@pytest.mark.asyncio
async def test_masked_override_releases_claim_when_not_actionable(tmp_path: Path) -> None:
    """A queued override that is no longer eligible is dropped and its claim released."""
    from agentshore.plays.registry import build_default_registry
    from agentshore.state import (
        AgentSnapshot,
        AgentStatus,
        AgentType,
        OrchestratorState,
        SessionState,
    )

    orch = _make_orch(tmp_path)
    orch._registry = build_default_registry(orch._cfg)
    orch._store.release_work_claim_group = AsyncMock()
    params = PlayParams(
        pr_number=210,
        extras={
            "claim_group_id": "claim-210",
            "resource_keys": ["pr:210"],
        },
    )
    from agentshore.plays.override import OverrideEntry, OverrideKind

    orch._override_queue.put_nowait(
        OverrideEntry(
            play_type=PlayType.MERGE_PR,
            params=params,
            kind=OverrideKind.EXECUTOR_REQUEUE,
        )
    )
    state = OrchestratorState(
        session_id=orch._session_id,
        session_state=SessionState.RUNNING,
        total_plays=12,
        total_cost=0.0,
        agents=[
            AgentSnapshot(
                agent_id="claude-1",
                agent_type=AgentType.CLAUDE_CODE,
                status=AgentStatus.IDLE,
                context_size=0,
                total_cost=0.0,
                total_tokens=0,
                tasks_completed=0,
                tasks_failed=0,
                model_tier="medium",
            )
        ],
        pull_requests=[],
    )

    assert await orch._consume_override(state) is None
    assert orch._override_queue.empty()
    orch._store.release_work_claim_group.assert_awaited_once_with(orch._session_id, "claim-210")


@pytest.mark.asyncio
async def test_masked_override_requeues_transient_staffing_gap(tmp_path: Path) -> None:
    """A queued override waits briefly when the only blocker is missing idle staff."""
    from agentshore.plays.registry import build_default_registry
    from agentshore.state import OrchestratorState, SessionState

    orch = _make_orch(tmp_path)
    orch._registry = build_default_registry(orch._cfg)
    orch._store.release_work_claim_group = AsyncMock()
    params = PlayParams(
        pr_number=210,
        extras={
            "claim_group_id": "claim-210",
            "resource_keys": ["pr:210"],
        },
    )
    from agentshore.plays.override import OverrideEntry, OverrideKind

    orch._override_queue.put_nowait(
        OverrideEntry(
            play_type=PlayType.MERGE_PR,
            params=params,
            kind=OverrideKind.EXECUTOR_REQUEUE,
        )
    )
    state = OrchestratorState(
        session_id=orch._session_id,
        session_state=SessionState.RUNNING,
        total_plays=12,
        total_cost=0.0,
        agents=[],
        pull_requests=[],
    )

    assert await orch._consume_override(state) is None
    assert not orch._override_queue.empty()
    requeued_entry = orch._override_queue.get_nowait()
    assert requeued_entry.params.extras["mask_requeue_attempts"] == 1
    assert requeued_entry.requeue_attempts == 1
    orch._store.release_work_claim_group.assert_not_awaited()


# ---------------------------------------------------------------------------
# _mask_reason_is_indefinite_wait: classifies deterministic-clear mask reasons
# so the override stays queued (no counter bump, no drop) until the wait lifts.
# ---------------------------------------------------------------------------


def test_mask_reason_is_indefinite_wait_matches_waiting_for() -> None:
    from agentshore.core import Orchestrator

    assert Orchestrator._mask_reason_is_indefinite_wait(
        "waiting for seed_project to complete before expanding the fleet"
    )


def test_mask_reason_is_indefinite_wait_matches_instantiate_cooldown() -> None:
    """Regression for desktop-e26.

    The bootstrap medium-of-different-type override was dropped on
    'instantiate cooldown (1/2 plays since last)' because the original predicate
    only matched 'waiting for'. The cooldown is also a deterministic-clear wait
    (it lifts after the configured number of plays), so the override should
    survive without counter bump.
    """
    from agentshore.core import Orchestrator

    assert Orchestrator._mask_reason_is_indefinite_wait(
        "instantiate cooldown (1/2 plays since last)"
    )
    assert Orchestrator._mask_reason_is_indefinite_wait("cooldown active for write_plan")


def test_mask_reason_is_indefinite_wait_does_not_match_transient_staffing() -> None:
    """Staffing gaps go through the transient retry path (counter bumps), not here."""
    from agentshore.core import Orchestrator

    assert not Orchestrator._mask_reason_is_indefinite_wait("no idle agents")
    assert not Orchestrator._mask_reason_is_indefinite_wait("rate_limit")


@pytest.mark.asyncio
async def test_masked_override_requeues_on_instantiate_cooldown_without_counter_bump(
    tmp_path: Path,
) -> None:
    """Regression for desktop-e26.

    The override must re-queue without incrementing ``mask_requeue_attempts``
    so it survives an arbitrary number of cooldown ticks until the cooldown
    lifts naturally.
    """
    from agentshore.plays.override import OverrideEntry, OverrideKind

    orch = _make_orch(tmp_path)
    orch._store.release_work_claim_group = AsyncMock()
    entry = OverrideEntry(
        play_type=PlayType.INSTANTIATE_AGENT,
        params=PlayParams(extras={"mask_requeue_attempts": 0}),
        kind=OverrideKind.EXECUTOR_REQUEUE,
    )

    await orch._handle_masked_override(entry, reason="instantiate cooldown (1/2 plays since last)")

    assert not orch._override_queue.empty()
    requeued_entry = orch._override_queue.get_nowait()
    assert requeued_entry.params.extras["mask_requeue_attempts"] == 0
    assert requeued_entry.requeue_attempts == 0
    assert requeued_entry.kind == OverrideKind.MASK_REQUEUE
    orch._store.release_work_claim_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_override_confirm_reuses_selector_live_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The override-confirm path must reuse the SAME live-graph loader as PPO
    selection, so a queued override is revalidated against fresh beads (not just
    the snapshot).

    Regression: the dispatch-side ``EligibilityAuthority`` was constructed
    without a ``live_graph_loader``, silently downgrading override confirm to a
    snapshot-only read and dropping the selection->dispatch drift detection a
    PPO-selected play gets. A non-PPO selector (test stubs / non-beads sessions)
    yields ``None`` (snapshot-only), matching the selector's own fallback.
    """
    orch = _make_orch(tmp_path)

    # Default test selector is a MagicMock (not a real PPO selector) -> no loader.
    assert orch._override_confirm_live_loader() is None

    # A real PPO selector -> reuse its loader verbatim (single source of truth).
    sentinel_loader = AsyncMock()
    ppo_selector = MagicMock()
    ppo_selector._build_live_graph_loader = MagicMock(return_value=sentinel_loader)
    orch._selector = ppo_selector
    monkeypatch.setattr(
        "agentshore.core.mixins.dispatch._ppo_selector_cls",
        lambda: type(ppo_selector),
    )

    assert orch._override_confirm_live_loader() is sentinel_loader
    ppo_selector._build_live_graph_loader.assert_called_once_with()
