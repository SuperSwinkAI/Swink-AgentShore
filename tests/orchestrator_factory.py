"""Shared Orchestrator stub factory for tests.

Provides ``make_test_orchestrator`` which builds an Orchestrator via ``__new__``
(skipping bootstrap) but with a fully-initialised :class:`SessionRuntime` so
tests exercising dispatch, completion, drain, or loop-detection components never
hang on a missing latch. Every shared mutable latch lives on ``orch._runtime``;
the orchestrator's backward-compat ``orch._<latch>`` properties delegate there,
so tests may read/write either form.
"""

from __future__ import annotations

import collections
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from agentshore.config import RuntimeConfig
from agentshore.core.main_repo_guard import MainRepoGuard
from agentshore.core.mixins.completion import CompletionProcessor
from agentshore.core.mixins.dispatch import Dispatcher
from agentshore.core.mixins.drain import DrainController
from agentshore.core.mixins.lifecycle import LifecycleController
from agentshore.core.mixins.loop import LoopRunner
from agentshore.core.mixins.snapshots import SnapshotProjector
from agentshore.core.mixins.state import StateBuilder
from agentshore.core.override_queue import OverrideQueue
from agentshore.core.recovery_tracker import RecoveryTracker
from agentshore.core.session_runtime import SessionRuntime
from agentshore.core.velocity_tracker import VelocityTracker
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

    # The single owner of all shared mutable session state. Constructed first so
    # the orchestrator's ``orch._<latch>`` compat properties (which delegate to
    # ``orch._runtime``) resolve for the rest of this factory and for tests.
    runtime = SessionRuntime(cfg=cfg, selector=selector, state_provider=NullStateProvider())
    orch._runtime = runtime
    runtime.policy_version = "test"
    # __new__-bypass parity with bootstrap: never auto-refresh on the first tick.
    runtime.last_refresh_time = float("inf")
    runtime.feedback_cadence_last_ack_monotonic = 0.0

    # Stable identity / collaborators owned directly by the orchestrator.
    orch._repo_root = tmp_path
    orch._session_id = "test-session"
    orch._store = store
    orch._manager = MagicMock()
    orch._manager.worktrees = SimpleNamespace(main_repo=tmp_path)
    orch._executor = MagicMock()
    orch._seed_path = None
    orch._step_index = 0
    orch._config_hash = "abc"
    orch._last_warned_failure_streak = None
    orch._last_warned_any_streak = None
    orch._fleet_idle_persistent_active = False
    orch._overrides = OverrideQueue()
    orch._velocity = VelocityTracker(velocity_window_size=50)
    orch._recovery = RecoveryTracker()
    orch._main_repo = MainRepoGuard()
    orch._snapshots = SnapshotProjector(
        manager=orch._manager, store=store, session_id=orch._session_id
    )
    orch._state_builder = StateBuilder(
        host=orch,
        runtime=runtime,
        store=store,
        manager=orch._manager,
        executor=orch._executor,
        session_id=orch._session_id,
        repo_root=orch._repo_root,
        main_repo=orch._main_repo,
        snapshots=orch._snapshots,
        velocity=orch._velocity,
        recovery=orch._recovery,
        overrides=orch._overrides,
    )
    orch._lifecycle = LifecycleController(
        host=orch,
        runtime=runtime,
        store=store,
        session_id=orch._session_id,
        repo_root=orch._repo_root,
        main_repo=orch._main_repo,
    )
    orch._drain = DrainController(
        host=orch,
        runtime=runtime,
        store=store,
        manager=orch._manager,
        session_id=orch._session_id,
        repo_root=orch._repo_root,
        state_builder=orch._state_builder,
    )
    orch._completion = CompletionProcessor(
        host=orch,
        runtime=runtime,
        store=store,
        manager=orch._manager,
        executor=orch._executor,
        session_id=orch._session_id,
        repo_root=orch._repo_root,
        main_repo=orch._main_repo,
        velocity=orch._velocity,
        recovery=orch._recovery,
        overrides=orch._overrides,
        snapshots=orch._snapshots,
        state_builder=orch._state_builder,
        lifecycle=orch._lifecycle,
        drain=orch._drain,
    )
    orch._completion.refresh_issues = AsyncMock()
    orch._dispatcher = Dispatcher(
        host=orch,
        runtime=runtime,
        store=store,
        manager=orch._manager,
        executor=orch._executor,
        session_id=orch._session_id,
        repo_root=orch._repo_root,
        main_repo=orch._main_repo,
        overrides=orch._overrides,
        state_builder=orch._state_builder,
        completion=orch._completion,
    )
    # The conductor — constructed last; references every sibling component +
    # the 1a collaborators. Owns the loop-only counters (tick-failure streak,
    # wedge counter, watchdog handle, heartbeat, fleet-idle latch, warning memos,
    # stagnation stage). On this __new__-bypass stub the heartbeat keeps its
    # float('inf') default (loop never started → not stale to the watchdog).
    orch._loop = LoopRunner(
        host=orch,
        runtime=runtime,
        session_id=orch._session_id,
        main_repo=orch._main_repo,
        overrides=orch._overrides,
        velocity=orch._velocity,
        state_builder=orch._state_builder,
        dispatcher=orch._dispatcher,
        completion=orch._completion,
        lifecycle=orch._lifecycle,
        drain=orch._drain,
    )
    return orch
