"""Shared Orchestrator stub factory for tests.

Provides ``make_test_orchestrator`` which mirrors every attribute set by
``_OrchestratorBase.__init__`` so tests exercising dispatch, completion,
drain, or loop-detection mixins never hang on a missing field.
"""

from __future__ import annotations

import asyncio
import collections
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from agentshore.config import RuntimeConfig
from agentshore.state import NullStateProvider


def make_test_orchestrator(
    tmp_path: Path,
    cfg: RuntimeConfig | None = None,
    *,
    store: Any | None = None,
    selector: Any | None = None,
) -> Any:
    """Build a fully-initialised Orchestrator stub without bootstrap.

    Accepts optional pre-configured mocks for ``store`` and ``selector`` so
    callers can wire up return values before the test body runs.
    """
    from agentshore.core import Orchestrator

    if cfg is None:
        cfg = RuntimeConfig()

    if store is None:
        store = AsyncMock()
        store.update_session_state = AsyncMock()

    if selector is None:
        selector = MagicMock()
        selector.__class__.__name__ = "MockSelector"
        # Eligibility refactor: the loop drains the selector's confirm-repick
        # tally once per cycle via consume_repick_count(). On a bare MagicMock
        # that returns a MagicMock, which then explodes on ``repicks > 0`` in
        # _record_selection_repicks. Return a real int so the window stays clean.
        selector.consume_repick_count = MagicMock(return_value=0)

    orch = Orchestrator.__new__(Orchestrator)
    orch._cfg = cfg
    orch._repo_root = tmp_path
    orch._session_id = "test-session"
    orch._store = store
    orch._manager = MagicMock()
    orch._manager.worktrees = SimpleNamespace(main_repo=tmp_path)
    orch._worktrees = None
    orch._executor = MagicMock()
    orch._selector = selector
    orch._state_provider = NullStateProvider()
    orch._stop_requested = False
    orch._stopped = False
    orch._draining = False
    orch._drain_reason = None
    orch._drain_initialized = False
    orch._end_session_dispatch_started = False
    orch._natural_exit_reason = None
    orch._natural_exit_callback = None
    orch._end_session_report_requested = False
    orch._end_session_report_open_browser = False
    orch._embedded_mode = False
    orch._esr_ready_callback = None
    orch._extra_budget = 0.0
    orch._last_stagnation_stage = 0
    orch._stop_reason = "unknown"
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
    orch._pending_override_kind = None
    orch._override_dispatched_play_ids = set()
    orch._last_warned_failure_streak = None
    orch._last_warned_any_streak = None
    orch._forced_mask_play_types = ()
    orch._loop_started_at = 0.0
    orch._registry = None
    orch._pause_event = asyncio.Event()
    orch._pause_event.set()
    orch._pause_reason = None
    orch._last_play_id = None
    orch._recent_executor_skip = False
    orch._executor_skip_window = collections.deque(maxlen=50)
    orch._recent_play_outcomes = collections.deque(maxlen=50)
    orch._budget_override = False
    orch._stop_done = asyncio.Event()
    orch._config_path = None
    orch._velocity_window_start_play_id = None
    orch._velocity_events = collections.deque(maxlen=50)
    orch._recent_agent_types = collections.deque(maxlen=50)
    orch._recent_play_completions = collections.deque(maxlen=64)
    orch._recent_applied_labels = collections.deque(maxlen=64)
    orch._break_recovery_failures = {}
    orch._rate_limit_recovery_enqueued = set()
    orch._last_selection_digest = None
    orch._idle_streak = 0
    orch._fleet_idle_persistent_active = False
    orch._idle_agent_claim_ticks = {}
    orch._last_refresh_time = float("inf")
    orch._default_branch = "main"
    orch._pre_play_branches = {}
    orch._main_repo_dispatch_paused = False
    orch._feedback_cadence_plays_since_ack = 0
    orch._feedback_cadence_last_ack_monotonic = 0.0
    orch._refresh_issues = AsyncMock()
    return orch
