"""Pause/resume, config reload, feedback-cadence, and budget-drain initiation."""

from __future__ import annotations

import asyncio as _asyncio
import dataclasses
import time
from typing import TYPE_CHECKING, Protocol

from agentshore.config import load_config
from agentshore.core.git_safety import resolve_default_branch
from agentshore.core.helpers import _logger, _ppo_selector_cls
from agentshore.data.store import HumanFeedbackRecord
from agentshore.errors import ConfigError
from agentshore.state import AgentStatus, SessionState
from agentshore.utils import now_iso

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable
    from pathlib import Path

    from agentshore.config import RuntimeConfig
    from agentshore.config.models import BudgetConfig
    from agentshore.core.main_repo_guard import MainRepoGuard
    from agentshore.core.mixins.drain import DrainController
    from agentshore.core.mixins.state import StateBuilder
    from agentshore.data.store import DataStore
    from agentshore.plays.selector import PlaySelector
    from agentshore.state import (
        OrchestratorState,
        StateProvider,
    )


# Automated-escalation pause reasons where the loop is waiting on a human who
# may be AFK; these auto-stop after ``feedback.unanswered_timeout_seconds`` (#9)
# rather than wedging the loop indefinitely. Explicit ``user_request`` /
# ``ipc_request`` pauses are intentionally excluded — an operator who paused is
# present and may be holding deliberately.
_AUTO_STOP_PAUSE_REASONS: frozenset[str] = frozenset(
    {"loop_detected", "stagnation", "budget_exhausted", "budget_predictive"}
)


class _LifecycleHost(Protocol):
    """Orchestrator runtime/control state read OR written live by :class:`LifecycleController`.

    These members are accessed fresh via ``self._host.<attr>`` on every call so
    SIGHUP config swaps (``_cfg``) and per-tick mutation (pause latches, budget
    override, feedback-cadence counters) are always current — never captured at
    construction. Fields the controller *writes* (``_cfg``, ``_pause_reason``,
    ``_pause_deadline``, ``_budget_override``, the feedback-cadence counters) are
    declared as plain annotated attributes (not read-only ``@property``) so the
    assignments type-check. ``_OrchestratorBase`` structurally satisfies this
    Protocol; the cross-component methods (``_safe_call``, ``begin_drain``) and
    the ``_state_builder`` reference are resolved live on the composition root.
    """

    # --- written by the controller -----------------------------------------
    _cfg: RuntimeConfig  # reassigned atomically on SIGHUP reload
    _pause_reason: str | None
    _pause_deadline: float | None
    _budget_override: bool
    _feedback_cadence_plays_since_ack: int
    _feedback_cadence_last_ack_monotonic: float
    # --- read by the controller --------------------------------------------
    _stop_requested: bool
    _draining: bool
    _loop_started_at: float
    _last_play_id: int | None
    _config_path: Path | None
    _selector: PlaySelector | None
    _state_provider: StateProvider
    _pause_event: asyncio.Event
    _state_builder: StateBuilder
    _drain: DrainController

    async def _safe_call(self, coro: Awaitable[object], label: str) -> None: ...

    def effective_budget_caps(self) -> BudgetConfig:
        """Live-effective budget caps (overrides shadowing ``_cfg.budget``)."""
        ...


class LifecycleController:
    """Pause, resume, config reload, budget-drain initiation, and feedback cadence.

    Stable services / collaborators are captured via the constructor; all
    orchestrator runtime/control state (read or written) flows through the
    :class:`_LifecycleHost` Protocol so SIGHUP and per-tick mutation never goes
    stale.
    """

    def __init__(
        self,
        *,
        host: _LifecycleHost,
        store: DataStore,
        session_id: str,
        repo_root: Path,
        main_repo: MainRepoGuard,
    ) -> None:
        self._host = host
        self._store = store
        self._session_id = session_id
        self._repo_root = repo_root
        self._main_repo = main_repo

    # ------------------------------------------------------------------
    # Implementations
    # ------------------------------------------------------------------

    def should_terminate(self, state: OrchestratorState) -> tuple[bool, str | None]:
        """Return (should_stop, reason) based on termination conditions."""
        if self._host._stop_requested:
            return True, "stop_requested"
        if state.session_state == SessionState.DRAINING:
            no_live_agents = not state.agents or all(
                a.status == AgentStatus.TERMINATED for a in state.agents
            )
            if no_live_agents:
                # Pure predicate: the defensive "drain completed with mergeable
                # PRs" visibility check now fires from
                # ``DrainController._on_drain_complete`` during drain
                # finalization, keeping this method free of side effects.
                return True, "drain_complete"
            return False, None  # continue; mask allows only end_agent
        if state.session_state == SessionState.SHUTTING_DOWN:
            return True, "shutting_down"

        max_plays = self._host._cfg.session.max_plays
        if max_plays is not None and state.total_plays >= max_plays:
            return True, "max_plays"

        # Wall-clock time budget hard-stop backstop. The primary path is the
        # 20-minute graceful drain in ``begin_budget_reserve_drain_if_needed``;
        # this fires only if the deadline is reached anyway (e.g. a single play
        # ran past the reserve window). Reads config + the monotonic loop clock
        # so it is independent of snapshot population order.
        budget_cfg = self._host.effective_budget_caps()
        if budget_cfg.time_enabled and self._host._loop_started_at > 0:
            elapsed_minutes = (time.monotonic() - self._host._loop_started_at) / 60
            if elapsed_minutes >= budget_cfg.time_total_minutes:
                return True, "time_budget"

        return False, None

    async def begin_budget_reserve_drain_if_needed(
        self, state: OrchestratorState
    ) -> OrchestratorState:
        """Start graceful drain once known spend enters the budget reserve."""
        if self._host._draining or self._host._stop_requested:
            return state
        budget = state.budget
        if budget is None:
            return state
        reason = budget.reserve_reason()
        if reason is None:
            return state
        await self._host._drain.begin_drain(reason)
        return await self._host._state_builder.build_state()

    async def pause(self, reason: str = "user_request") -> None:
        """Pause the orchestrator loop after the current play completes.

        Clears ``_pause_event`` so the loop blocks at the top of the next
        iteration until ``resume()`` is called.
        """
        self._host._pause_reason = reason
        self._host._pause_event.clear()
        # Arm the unanswered-pause backstop (#9): an automated-escalation pause
        # that nobody answers must auto-stop rather than wedge the loop forever.
        # Scoped to escalations where the awaited human may be AFK; explicit
        # user/ipc pauses are excluded — an operator who paused is present.
        timeout = self._host._cfg.feedback.unanswered_timeout_seconds
        if reason in _AUTO_STOP_PAUSE_REASONS and timeout is not None:
            self._host._pause_deadline = time.monotonic() + float(timeout)
        else:
            self._host._pause_deadline = None
        _logger.warning("session_pausing", reason=reason, session_id=self._session_id)
        await self._host._safe_call(
            self._store.update_session_state(self._session_id, "paused"),
            "update_session_state",
        )
        await self._host._safe_call(
            self._host._state_provider.on_session_paused(reason), "on_session_paused"
        )
        if self._host._last_play_id is not None:
            await self._host._safe_call(
                self._store.record_human_feedback(
                    HumanFeedbackRecord(
                        session_id=self._session_id,
                        play_id=self._host._last_play_id,
                        trigger=reason,
                        feedback_text=None,
                        action_taken="pause_requested",
                        created_at=now_iso(),
                    )
                ),
                "record_human_feedback",
            )
        if self.feedback_enabled_for_reason(reason):
            await self._host._safe_call(
                self._host._state_provider.on_feedback_requested(reason), "on_feedback_requested"
            )
        # Flush any pending PPO experience so plays at pause point are not lost
        if (
            isinstance(self._host._selector, _ppo_selector_cls())
            and len(self._host._selector.buffer) > 0
        ):
            try:
                await self._host._selector.update_policy(next_state_value=0.0)
            except (RuntimeError, ValueError) as exc:
                _logger.warning("ppo_flush_on_pause_failed", error=str(exc))

    async def resume(self, override_budget: bool = False) -> None:
        """Resume the orchestrator loop after a pause.

        ``override_budget`` is accepted for compatibility with older feedback
        flows; budget reserve drain is handled separately.
        """
        if override_budget:
            self._host._budget_override = True
        self._host._pause_reason = None
        self._host._pause_deadline = None
        await self._host._safe_call(
            self._store.update_session_state(self._session_id, "running"),
            "update_session_state",
        )
        self._host._pause_event.set()
        self._host._feedback_cadence_plays_since_ack = 0
        self._host._feedback_cadence_last_ack_monotonic = time.monotonic()
        _logger.info(
            "session_resumed", session_id=self._session_id, budget_override=override_budget
        )

    async def reload_config(self) -> None:
        """Reload configuration from disk (triggered by SIGHUP)."""
        if self._host._config_path is None:
            _logger.warning("config_reload_skipped", reason="no config path set")
            return
        try:
            new_cfg = load_config(self._host._config_path)
        except (ConfigError, OSError) as exc:
            _logger.error("config_reload_failed", error=str(exc))
            return

        old_cfg = self._host._cfg
        changed: list[str] = []
        for f in dataclasses.fields(old_cfg):
            if getattr(old_cfg, f.name) != getattr(new_cfg, f.name):
                changed.append(f.name)

        if not changed:
            _logger.info("config_reload_no_changes")
            return

        non_reloadable = {"rl"}  # rl contains policy weights, learning rate, and mode
        warned = [f for f in changed if f in non_reloadable]
        if warned:
            _logger.warning("config_non_reloadable_fields_changed", fields=warned)

        self._host._cfg = new_cfg
        # desktop-kqo5: SIGHUP-triggered reload also refreshes the cached
        # default branch in case the operator pointed origin/HEAD at a new
        # branch (e.g. main -> develop) between sessions.
        try:
            new_default, assumed = await _asyncio.to_thread(resolve_default_branch, self._repo_root)
        except Exception as exc:
            _logger.warning("default_branch_refresh_failed", error=str(exc))
        else:
            if new_default != self._main_repo.default_branch:
                _logger.info(
                    "default_branch_refreshed",
                    session_id=self._session_id,
                    previous=self._main_repo.default_branch,
                    current=new_default,
                    assumed=assumed,
                )
                self._main_repo.default_branch = new_default
        _logger.info("config_reloaded", changed_fields=changed)

    async def pause_with_reason(self, reason: str) -> None:
        """Delegate to ``pause()``; callers should ``continue`` the loop, not ``return``."""
        await self.pause(reason)

    def feedback_enabled_for_reason(self, reason: str) -> bool:
        """Return whether feedback prompts are enabled for a pause reason."""
        feedback_cfg = self._host._cfg.feedback
        if reason == "stagnation":
            return feedback_cfg.on_stagnation
        if reason in {"budget_exhausted", "budget_predictive"}:
            return feedback_cfg.on_budget_exhaustion
        if reason == "loop_detected":
            return feedback_cfg.on_loop_escalation
        if reason in {"user_request", "ipc_request"}:
            return True
        return feedback_cfg.on_ambiguous_intake

    def feedback_cadence_reason(self) -> str | None:
        """Return the cadence-checkpoint reason string if a checkpoint is due, else None."""
        cfg = self._host._cfg.feedback
        cadence_plays = cfg.cadence_plays
        if (
            cadence_plays is not None
            and cadence_plays > 0
            and self._host._feedback_cadence_plays_since_ack >= cadence_plays
        ):
            return "feedback_cadence_plays"
        cadence_minutes = cfg.cadence_minutes
        if cadence_minutes is not None and cadence_minutes > 0:
            elapsed = time.monotonic() - self._host._feedback_cadence_last_ack_monotonic
            if elapsed >= cadence_minutes * 60:
                return "feedback_cadence_minutes"
        return None

    async def pause_for_feedback_cadence_if_due(self) -> bool:
        """Pause via the feedback channel if a cadence checkpoint is due.

        Returns True if a pause was initiated. Guards against double-pausing by
        checking ``_pause_event`` before delegating to ``pause_with_reason``.
        """
        if not self._host._pause_event.is_set():
            return False
        reason = self.feedback_cadence_reason()
        if reason is None:
            return False
        await self.pause_with_reason(reason)
        return True
