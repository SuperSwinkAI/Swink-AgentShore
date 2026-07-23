"""Pause/resume, config reload, feedback-cadence, and budget-drain initiation."""

from __future__ import annotations

import asyncio as _asyncio
import dataclasses
import time
from typing import TYPE_CHECKING, Protocol

from agentshore.config import load_config
from agentshore.core.git_safety import resolve_default_branch
from agentshore.core.helpers import _logger, _ppo_selector_cls, _SafeCallHost
from agentshore.data.store import HumanFeedbackRecord
from agentshore.errors import ConfigError
from agentshore.state import AgentStatus, SessionState
from agentshore.utils import now_iso

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.config.models import BudgetConfig
    from agentshore.core.main_repo_guard import MainRepoGuard
    from agentshore.core.mixins.drain import DrainController
    from agentshore.core.mixins.state import StateBuilder
    from agentshore.core.session_runtime import SessionRuntime
    from agentshore.data.store import DataStore
    from agentshore.state import (
        OrchestratorState,
    )


# Automated-escalation pauses where the awaited human may be AFK: these auto-stop
# after ``feedback.unanswered_timeout_seconds`` (#9) rather than wedging forever.
# Explicit user/ipc pauses are excluded — an operator who paused is present.
_AUTO_STOP_PAUSE_REASONS: frozenset[str] = frozenset(
    {"loop_detected", "stagnation", "budget_exhausted", "budget_predictive"}
)


class _LifecycleHost(_SafeCallHost, Protocol):
    """Orchestrator *behaviour* the :class:`LifecycleController` invokes.

    All shared session *state* now lives on :class:`SessionRuntime` (reached via
    ``self._runtime``); this Protocol is the narrow behaviour seam that remains so
    the cross-component method (``effective_budget_caps``) and the
    sibling-component references (``_state_builder``, ``_drain``) resolve on the
    composition root without a circular import. ``_OrchestratorBase`` structurally
    satisfies it. Extends :class:`_SafeCallHost` for the ``_safe_call`` method
    shared by every per-component Host Protocol.
    """

    _state_builder: StateBuilder
    _drain: DrainController

    def effective_budget_caps(self) -> BudgetConfig:
        """Live-effective budget caps (overrides shadowing ``cfg.budget``)."""
        ...


class LifecycleController:
    """Pause, resume, config reload, budget-drain initiation, and feedback cadence.

    Stable services / collaborators are captured via the constructor; all shared
    session state (read or written) lives on the injected :class:`SessionRuntime`,
    and the cross-component behaviour/sibling references resolve via the narrow
    :class:`_LifecycleHost` behaviour seam.
    """

    def __init__(
        self,
        *,
        host: _LifecycleHost,
        runtime: SessionRuntime,
        store: DataStore,
        session_id: str,
        repo_root: Path,
        main_repo: MainRepoGuard,
    ) -> None:
        self._host = host
        self._runtime = runtime
        self._store = store
        self._session_id = session_id
        self._repo_root = repo_root
        self._main_repo = main_repo

    # ------------------------------------------------------------------
    # Implementations
    # ------------------------------------------------------------------

    def should_terminate(self, state: OrchestratorState) -> tuple[bool, str | None]:
        """Return (should_stop, reason) based on termination conditions."""
        if self._runtime.stop_requested:
            return True, "stop_requested"
        if state.session_state == SessionState.DRAINING:
            no_live_agents = not state.agents or all(
                a.status == AgentStatus.TERMINATED for a in state.agents
            )
            if no_live_agents:
                # Pure predicate: the "drain completed with mergeable PRs" visibility
                # check fires from ``DrainController._on_drain_complete`` instead, keeping
                # this method side-effect-free.
                return True, "drain_complete"
            return False, None  # continue; mask allows only end_agent
        if state.session_state == SessionState.SHUTTING_DOWN:
            return True, "shutting_down"

        max_plays = self._runtime.cfg.session.max_plays
        if max_plays is not None and state.total_plays >= max_plays:
            return True, "max_plays"

        # Wall-clock hard-stop backstop: the primary path is the graceful drain in
        # ``begin_budget_reserve_drain_if_needed``; this fires only if the deadline is
        # hit anyway (e.g. a play ran past the reserve window). Uses the monotonic loop
        # clock so it's independent of snapshot population order.
        budget_cfg = self._host.effective_budget_caps()
        if budget_cfg.time_enabled and self._runtime.loop_started_at > 0:
            elapsed_minutes = (time.monotonic() - self._runtime.loop_started_at) / 60
            if elapsed_minutes >= budget_cfg.time_total_minutes:
                return True, "time_budget"

        return False, None

    async def begin_budget_reserve_drain_if_needed(
        self, state: OrchestratorState
    ) -> OrchestratorState:
        """Start graceful drain once known spend enters the budget reserve."""
        if self._runtime.draining or self._runtime.stop_requested:
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
        self._runtime.pause_reason = reason
        self._runtime.pause_event.clear()
        # Arm the unanswered-pause backstop (#9): an unanswered automated escalation
        # must auto-stop rather than wedge forever (see _AUTO_STOP_PAUSE_REASONS).
        timeout = self._runtime.cfg.feedback.unanswered_timeout_seconds
        if reason in _AUTO_STOP_PAUSE_REASONS and timeout is not None:
            self._runtime.pause_deadline = time.monotonic() + float(timeout)
        else:
            self._runtime.pause_deadline = None
        _logger.warning("session_pausing", reason=reason, session_id=self._session_id)
        await self._host._safe_call(
            self._store.update_session_state(self._session_id, "paused"),
            "update_session_state",
        )
        await self._host._safe_call(
            self._runtime.state_provider.on_session_paused(reason), "on_session_paused"
        )
        if self._runtime.last_play_id is not None:
            await self._host._safe_call(
                self._store.record_human_feedback(
                    HumanFeedbackRecord(
                        session_id=self._session_id,
                        play_id=self._runtime.last_play_id,
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
                self._runtime.state_provider.on_feedback_requested(reason), "on_feedback_requested"
            )
        # Flush any pending PPO experience so plays at pause point are not lost
        if (
            isinstance(self._runtime.selector, _ppo_selector_cls())
            and len(self._runtime.selector.buffer) > 0
        ):
            try:
                await self._runtime.selector.update_policy(next_state_value=0.0)
            except (RuntimeError, ValueError) as exc:
                _logger.warning("ppo_flush_on_pause_failed", error=str(exc))

    async def resume(self) -> None:
        """Resume the orchestrator loop after a pause."""
        self._runtime.pause_reason = None
        self._runtime.pause_deadline = None
        await self._host._safe_call(
            self._store.update_session_state(self._session_id, "running"),
            "update_session_state",
        )
        self._runtime.pause_event.set()
        self._runtime.feedback_cadence_plays_since_ack = 0
        self._runtime.feedback_cadence_last_ack_monotonic = time.monotonic()
        _logger.info("session_resumed", session_id=self._session_id)

    async def reload_config(self) -> None:
        """Reload configuration from disk (triggered by SIGHUP)."""
        if self._runtime.config_path is None:
            _logger.warning("config_reload_skipped", reason="no config path set")
            return
        try:
            new_cfg = load_config(self._runtime.config_path)
        except (ConfigError, OSError) as exc:
            _logger.error("config_reload_failed", error=str(exc))
            return

        old_cfg = self._runtime.cfg
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

        self._runtime.cfg = new_cfg
        # The PPO selector builds its action mask from the config captured at
        # construction (incl. user-disabled-play hard-mask) and isn't re-created on
        # reload; refresh it here or a play disabled mid-session stays selectable.
        if isinstance(self._runtime.selector, _ppo_selector_cls()):
            self._runtime.selector.update_orchestrator_cfg(new_cfg)
        # Reload also refreshes the cached default branch in case origin/HEAD was
        # repointed (e.g. main -> develop) between sessions.
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
        feedback_cfg = self._runtime.cfg.feedback
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
        cfg = self._runtime.cfg.feedback
        cadence_plays = cfg.cadence_plays
        if (
            cadence_plays is not None
            and cadence_plays > 0
            and self._runtime.feedback_cadence_plays_since_ack >= cadence_plays
        ):
            return "feedback_cadence_plays"
        cadence_minutes = cfg.cadence_minutes
        if cadence_minutes is not None and cadence_minutes > 0:
            elapsed = time.monotonic() - self._runtime.feedback_cadence_last_ack_monotonic
            if elapsed >= cadence_minutes * 60:
                return "feedback_cadence_minutes"
        return None

    async def pause_for_feedback_cadence_if_due(self) -> bool:
        """Pause via the feedback channel if a cadence checkpoint is due.

        Returns True if a pause was initiated. Guards against double-pausing by
        checking ``_pause_event`` before delegating to ``pause_with_reason``.
        """
        if not self._runtime.pause_event.is_set():
            return False
        reason = self.feedback_cadence_reason()
        if reason is None:
            return False
        await self.pause_with_reason(reason)
        return True
