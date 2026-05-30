"""Regression tests for OverrideEntry.wait_for_play_type (issue #569).

The bootstrap medium INSTANTIATE_AGENT entry sets ``wait_for_play_type`` so it
stays masked until the first-play (cleanup or seed_project) appears in
``state.plays_since_last_play_type``. This is *additive* to
``bypass_preconditions=True`` — the cooldown skip still works, but the
sequencing gate holds until the awaited play has completed.
"""

from __future__ import annotations

import asyncio
import collections
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentshore.config import RuntimeConfig
from agentshore.plays.base import PlayParams
from agentshore.plays.override import OverrideEntry, OverrideKind
from agentshore.rl.mask_reason import MaskClassification
from agentshore.state import (
    NullStateProvider,
    OrchestratorState,
    PlayType,
    SessionState,
)


def _make_orch(tmp_path: Path) -> Any:
    """Mirror of the canonical orchestrator stub used elsewhere in the suite."""
    from agentshore.core import Orchestrator

    mock_store = AsyncMock()
    mock_store.update_session_state = AsyncMock()
    mock_selector = MagicMock()
    mock_selector.__class__.__name__ = "MockSelector"

    orch = Orchestrator.__new__(Orchestrator)
    orch._cfg = RuntimeConfig()
    orch._repo_root = tmp_path
    orch._session_id = "test-session"
    orch._store = mock_store
    orch._manager = MagicMock()
    orch._manager.worktrees = SimpleNamespace(main_repo=tmp_path)
    orch._executor = MagicMock()
    orch._selector = mock_selector
    orch._state_provider = NullStateProvider()
    orch._stop_requested = False
    orch._draining = False
    orch._exit_stack = MagicMock()
    orch._health = None
    orch._integrity = None
    orch._power_assertion = None
    orch._in_flight = {}
    orch._dispatch_ctx = {}
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
    orch._last_warned_failure_streak = None
    orch._last_warned_any_streak = None
    orch._forced_mask_play_types = ()
    orch._feedback_cadence_plays_since_ack = 0
    orch._feedback_cadence_last_ack_monotonic = 0.0
    orch._recent_executor_skip = False
    orch._executor_skip_window = collections.deque(maxlen=50)
    orch._recent_play_completions = collections.deque(maxlen=64)
    orch._recent_applied_labels = collections.deque(maxlen=64)
    orch._pending_override_kind = None
    orch._override_dispatched_play_ids = set()
    return orch


def _state(plays_since: dict[PlayType, int]) -> OrchestratorState:
    return OrchestratorState(
        session_id="test-session",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[],
        pull_requests=[],
        plays_since_last_play_type=plays_since,
    )


def _bootstrap_medium_entry(awaited: PlayType) -> OverrideEntry:
    """Mirror the production entry from phases.py (medium INSTANTIATE_AGENT)."""
    return OverrideEntry(
        play_type=PlayType.INSTANTIATE_AGENT,
        params=PlayParams(
            target_agent_type="codex",
            target_model_tier="medium",
            bypass_preconditions=True,
        ),
        kind=OverrideKind.BOOTSTRAP,
        enqueue_classification=MaskClassification.INDEFINITE_WAIT,
        wait_for_play_type=awaited,
    )


def test_wait_for_play_type_field_defaults_to_none() -> None:
    """Existing callers that don't pass wait_for_play_type get None — no behavioral change."""
    entry = OverrideEntry(
        play_type=PlayType.INSTANTIATE_AGENT,
        params=PlayParams(),
        kind=OverrideKind.BOOTSTRAP,
    )
    assert entry.wait_for_play_type is None


@pytest.mark.asyncio
async def test_override_masked_when_awaited_play_not_yet_completed(tmp_path: Path) -> None:
    """Case 1: cleanup NOT in plays_since_last_play_type -> entry stays masked.

    Even with ``bypass_preconditions=True``, the wait_for_play_type gate holds
    the entry. It re-queues (BOOTSTRAP kind never drops) so the next tick
    re-evaluates.
    """
    orch = _make_orch(tmp_path)
    orch._override_queue.put_nowait(_bootstrap_medium_entry(PlayType.CLEANUP))

    state = _state({})  # cleanup hasn't completed yet

    result = await orch._consume_override(state)

    assert result is None, "entry should not dispatch while wait_for_play_type unmet"
    assert not orch._override_queue.empty(), "BOOTSTRAP entry must re-queue, not drop"

    requeued = orch._override_queue.get_nowait()
    assert requeued.play_type == PlayType.INSTANTIATE_AGENT
    assert requeued.wait_for_play_type == PlayType.CLEANUP
    # BOOTSTRAP -> MASK_REQUEUE on re-queue (preserved across handle_masked_override).
    assert requeued.kind == OverrideKind.MASK_REQUEUE
    # INDEFINITE_WAIT classification: no retry-counter bump.
    assert requeued.requeue_attempts == 0


@pytest.mark.asyncio
async def test_override_released_once_awaited_play_completed(tmp_path: Path) -> None:
    """Case 2: cleanup present in plays_since_last_play_type -> entry dispatches."""
    orch = _make_orch(tmp_path)
    orch._override_queue.put_nowait(_bootstrap_medium_entry(PlayType.CLEANUP))

    # Mark cleanup as having completed at least once.
    state = _state({PlayType.CLEANUP: 0})

    result = await orch._consume_override(state)

    assert result is not None, "entry should dispatch once wait_for_play_type is satisfied"
    play_type, params = result
    assert play_type == PlayType.INSTANTIATE_AGENT
    assert params.target_model_tier == "medium"
    assert orch._override_queue.empty(), "entry consumed, queue drains"
    assert orch._pending_override_kind == OverrideKind.BOOTSTRAP


@pytest.mark.asyncio
async def test_wait_for_seed_project_also_works(tmp_path: Path) -> None:
    """The gate is generic — also gates on seed_project when that is the first play."""
    orch = _make_orch(tmp_path)
    orch._override_queue.put_nowait(_bootstrap_medium_entry(PlayType.SEED_PROJECT))

    # cleanup completed but the awaited play (seed_project) has not.
    state = _state({PlayType.CLEANUP: 5})

    result = await orch._consume_override(state)

    assert result is None
    assert not orch._override_queue.empty()
    requeued = orch._override_queue.get_nowait()
    assert requeued.wait_for_play_type == PlayType.SEED_PROJECT
