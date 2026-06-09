"""Graceful drain, stop/hard_stop, budget adjustment, and end-session report."""

from __future__ import annotations

import asyncio
import math
import time
from typing import TYPE_CHECKING, Protocol

import aiosqlite

from agentshore.budget import (
    MAX_TIME_BUDGET_MINUTES,
    MIN_ENABLED_BUDGET_USD,
    MIN_TIME_BUDGET_MINUTES,
)
from agentshore.config.models import BudgetConfig
from agentshore.core.helpers import _emit_weights_dir_inventory, _logger, _ppo_selector_cls
from agentshore.errors import OrchestratorError
from agentshore.paths import project_reports_dir

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from agentshore.agents.health import HealthMonitor
    from agentshore.agents.manager import AgentManager
    from agentshore.core.context import _DispatchContext
    from agentshore.core.mixins.completion import CompletionProcessor
    from agentshore.core.mixins.state import StateBuilder
    from agentshore.data.integrity import IntegrityMonitor
    from agentshore.data.store import DataStore
    from agentshore.plays.selector import PlaySelector
    from agentshore.power import PowerAssertion
    from agentshore.state import (
        OrchestratorState,
        PlayOutcome,
        StateProvider,
    )


SHUTDOWN_GRACE_PERIOD_SECONDS = 5.0


class _DrainHost(Protocol):
    """Orchestrator runtime/control state read OR written live by :class:`DrainController`.

    These members are accessed fresh via ``self._host.<attr>`` on every call so
    per-tick mutation (stop/drain latches, budget override, ESR request flags,
    in-flight maps) is always current — never captured at construction. Fields
    the controller *writes* (the stop/drain latches, ``_stop_reason``,
    ``_extra_budget``, ``_budget_override``, the ESR request flags, the ESR-ready
    callback) are declared as plain annotated attributes (not read-only
    ``@property``) so the assignments type-check. ``_OrchestratorBase``
    structurally satisfies this Protocol; the cross-component methods
    (``_safe_call``, ``_weights_dir``, ``stop_loop_liveness_watchdog``) and the
    ``_completion`` component (for the shutdown-time GitHub refresh) are resolved
    live on the composition root.
    """

    # --- written by the controller -----------------------------------------
    _stopped: bool
    _stop_requested: bool
    _stop_reason: str
    _pause_reason: str | None
    _draining: bool
    _drain_reason: str | None
    _drain_initialized: bool
    _extra_budget: float
    _budget_override: bool
    # Live mid-session cap overrides (Feature B, #41/#42).
    _budget_override_enabled: bool | None
    _budget_override_total: float | None
    _time_override_enabled: bool | None
    _time_override_minutes: int | None
    _end_session_report_requested: bool
    _end_session_report_open_browser: bool
    _esr_ready_callback: Callable[[str, str, str | None], None] | None
    # --- read by the controller --------------------------------------------
    _loop_started_at: float
    _config_path: Path | None
    _pause_event: asyncio.Event
    _stop_done: asyncio.Event
    _completion_processing_idle: asyncio.Event
    _completion_processing_count: int
    _health: HealthMonitor | None
    _integrity: IntegrityMonitor | None
    _power_assertion: PowerAssertion | None
    _in_flight: dict[str, asyncio.Task[PlayOutcome]]
    _dispatch_ctx: dict[str, _DispatchContext]
    _selector: PlaySelector | None
    _state_provider: StateProvider
    _embedded_mode: bool
    _log_path: Path | None

    async def _safe_call(self, coro: Awaitable[object], label: str) -> None: ...

    def effective_budget_caps(self) -> BudgetConfig:
        """Live-effective budget caps (overrides shadowing ``_cfg.budget``)."""
        ...

    _completion: CompletionProcessor

    def _weights_dir(self) -> Path: ...

    def stop_loop_liveness_watchdog(self) -> None: ...


class BudgetControl:
    """Live-cap policy: validate, apply, persist, re-arm.

    Owns the mid-session dollar/time cap override state and the pause/drain
    re-arming logic. Extracted from :class:`DrainController` so that class
    keeps one job: graceful drain, stop, hard_stop, and end-session report.

    :class:`DrainController` wires drain-reversal callbacks through the public
    ``set_budget`` / ``add_budget`` / ``current_budget`` methods.
    """

    def __init__(
        self,
        *,
        host: _DrainHost,
        store: DataStore,
        session_id: str,
        state_builder: StateBuilder,
        exit_drain: Callable[[], Awaitable[None]],
    ) -> None:
        self._host = host
        self._store = store
        self._session_id = session_id
        self._state_builder = state_builder
        self._exit_drain = exit_drain

    async def set_budget(
        self,
        *,
        dollars_enabled: bool,
        dollars: float | None,
        time_enabled: bool,
        time_minutes: int | None,
        persist: bool = True,
    ) -> dict[str, object]:
        """Absolute-set the live dollar/time caps (sidecar RPC + desktop dialog).

        Validates bounds, applies the caps via the override fields, clears the
        additive ``_extra_budget`` accumulator, and re-arms (or reverses) the
        drain when a raised cap moves the session back outside its reserve.
        Persists to ``agentshore.yaml`` when *persist* so caps survive restart.
        """
        self._validate_dollar(dollars_enabled, dollars)
        self._validate_time(time_enabled, time_minutes)
        self._host._budget_override_enabled = dollars_enabled
        self._host._budget_override_total = (
            float(dollars) if dollars_enabled and dollars is not None else 0.0
        )
        self._host._time_override_enabled = time_enabled
        self._host._time_override_minutes = (
            int(time_minutes) if time_enabled and time_minutes is not None else 0
        )
        self._host._extra_budget = 0.0
        resumed = await self._rearm_after_budget_change()
        if persist:
            await asyncio.to_thread(self._persist_budget_sync)
        _logger.info(
            "budget_set",
            dollars_enabled=dollars_enabled,
            dollars=self._host._budget_override_total,
            time_enabled=time_enabled,
            time_minutes=self._host._time_override_minutes,
            resumed=resumed,
            session_id=self._session_id,
        )
        return await self._apply_and_publish(resumed=resumed)

    async def add_budget(
        self,
        *,
        delta_usd: float | None = None,
        delta_minutes: int | None = None,
        persist: bool = True,
    ) -> dict[str, object]:
        """Additively top up the dollar cap and/or extend the time cap (CLI)."""
        has_dollar = delta_usd is not None and delta_usd > 0
        has_time = delta_minutes is not None and delta_minutes > 0
        if not has_dollar and not has_time:
            raise OrchestratorError("add_budget requires a positive --budget and/or --time delta")
        caps = self._host.effective_budget_caps()
        if has_dollar:
            base = caps.total if caps.enabled else 0.0
            new_total = base + self._host._extra_budget + float(delta_usd)  # type: ignore[arg-type]
            if new_total < MIN_ENABLED_BUDGET_USD:
                raise OrchestratorError(
                    f"resulting dollar cap ${new_total:.2f} is below the "
                    f"${MIN_ENABLED_BUDGET_USD:.2f} minimum"
                )
            self._host._budget_override_enabled = True
            self._host._budget_override_total = new_total
            self._host._extra_budget = 0.0
        if has_time:
            base_min = caps.time_total_minutes if caps.time_enabled else 0
            new_minutes = int(base_min) + int(delta_minutes)  # type: ignore[arg-type]
            if not (MIN_TIME_BUDGET_MINUTES <= new_minutes <= MAX_TIME_BUDGET_MINUTES):
                raise OrchestratorError(
                    f"resulting time cap {new_minutes} min is outside "
                    f"{MIN_TIME_BUDGET_MINUTES}-{MAX_TIME_BUDGET_MINUTES} (1h-72h)"
                )
            self._host._time_override_enabled = True
            self._host._time_override_minutes = new_minutes
        resumed = await self._rearm_after_budget_change()
        if persist:
            await asyncio.to_thread(self._persist_budget_sync)
        _logger.info(
            "budget_added",
            delta_usd=delta_usd,
            delta_minutes=delta_minutes,
            resumed=resumed,
            session_id=self._session_id,
        )
        return await self._apply_and_publish(resumed=resumed)

    async def current_budget(self) -> dict[str, object]:
        """Return the live-effective caps + spend/remaining (prefill/echo)."""
        state = await self._state_builder.build_state()
        return self._applied_from_state(state, resumed=False)

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _validate_dollar(enabled: bool, dollars: float | None) -> None:
        if not enabled:
            return
        if dollars is None or not math.isfinite(dollars) or dollars < MIN_ENABLED_BUDGET_USD:
            raise OrchestratorError(
                f"dollar cap must be at least ${MIN_ENABLED_BUDGET_USD:.2f} when enabled"
            )

    @staticmethod
    def _validate_time(enabled: bool, minutes: int | None) -> None:
        if not enabled:
            return
        if not (
            isinstance(minutes, int)
            and MIN_TIME_BUDGET_MINUTES <= minutes <= MAX_TIME_BUDGET_MINUTES
        ):
            raise OrchestratorError(
                f"time cap must be between {MIN_TIME_BUDGET_MINUTES} and "
                f"{MAX_TIME_BUDGET_MINUTES} minutes (1h-72h) when enabled"
            )

    def _persist_budget_sync(self) -> None:
        """Synchronous budget persist — always called via ``asyncio.to_thread``."""
        config_path = self._host._config_path
        if config_path is None:
            return
        caps = self._host.effective_budget_caps()
        persisted = BudgetConfig(
            enabled=caps.enabled,
            total=caps.total + self._host._extra_budget,
            warning_threshold=caps.warning_threshold,
            time_enabled=caps.time_enabled,
            time_total_minutes=caps.time_total_minutes,
        )
        from agentshore.config.budget_writer import write_budget_to_config

        write_budget_to_config(config_path, persisted)

    async def _apply_and_publish(self, *, resumed: bool) -> dict[str, object]:
        """Build a fresh state, push it so the dashboard repaints immediately, echo it."""
        state = await self._state_builder.build_state()
        await self._host._safe_call(
            self._host._state_provider.on_state_update(state), "on_state_update_budget"
        )
        return self._applied_from_state(state, resumed=resumed)

    @staticmethod
    def _applied_from_state(state: OrchestratorState, *, resumed: bool) -> dict[str, object]:
        b = state.budget
        if b is None:
            return {"resumed": resumed}
        remaining = (
            b.remaining if (b.remaining is not None and math.isfinite(b.remaining)) else None
        )
        return {
            "enabled": b.enabled,
            "total": b.total_budget,
            "spent": b.spent,
            "remaining": remaining,
            "time_enabled": b.time_enabled,
            "time_total_minutes": b.time_total_minutes,
            "time_elapsed_minutes": b.time_elapsed_minutes,
            "time_remaining_minutes": b.time_remaining_minutes,
            "resumed": resumed,
        }

    async def _rearm_after_budget_change(self) -> bool:
        """Resume a budget pause or reverse a budget/time drain if now in-bounds."""
        self._host._budget_override = False  # allow budget checks to run again
        # Case 1: paused on budget exhaustion → resume when no longer reserve-bound.
        paused_on_budget = (
            not self._host._draining
            and not self._host._stop_requested
            and not self._host._pause_event.is_set()
            and self._host._pause_reason in {"budget_exhausted", "budget_predictive"}
        )
        if paused_on_budget and not await self._reserve_still_reached():
            self._host._pause_reason = None
            self._host._pause_event.set()
            return True
        # Case 2: draining on a budget/time reserve → reverse when back in-bounds.
        if (
            self._host._draining
            and not self._host._stop_requested
            and self._host._drain_reason
            in {"budget_reserve_reached", "time_budget_reserve_reached"}
            and not await self._reserve_still_reached()
        ):
            await self._exit_drain()
            return True
        return False

    async def _reserve_still_reached(self) -> bool:
        state = await self._state_builder.build_state()
        b = state.budget
        if b is None:
            return False
        return b.reserve_reason() is not None


class DrainController:
    """Drain, stop, hard_stop, budget adjust, and end-session report generation.

    Stable services / collaborators are captured via the constructor; all
    orchestrator runtime/control state (read or written) flows through the
    :class:`_DrainHost` Protocol so per-tick mutation never goes stale.
    Live budget policy (validation, persist, re-arm) is owned by the
    :class:`BudgetControl` collaborator and delegated here.
    """

    def __init__(
        self,
        *,
        host: _DrainHost,
        store: DataStore,
        manager: AgentManager,
        session_id: str,
        repo_root: Path,
        state_builder: StateBuilder,
    ) -> None:
        self._host = host
        self._store = store
        self._manager = manager
        self._session_id = session_id
        self._repo_root = repo_root
        self._state_builder = state_builder
        # One-shot guard for the drain-complete defensive-visibility warning
        # (``_on_drain_complete``) so it can never double-emit within a session.
        self._drain_complete_warned = False
        self._budget = BudgetControl(
            host=host,
            store=store,
            session_id=session_id,
            state_builder=state_builder,
            exit_drain=self._exit_drain,
        )

    # ------------------------------------------------------------------

    def _on_drain_complete(self, state: OrchestratorState) -> None:
        """Emit the drain-complete defensive-visibility warning at most once.

        Drain should not have been entered with merge-ready PRs outstanding
        (the auto-stop guard in loop.py defers that). If we still reach
        drain completion with mergeable PRs, surface it loudly — finished work
        is being abandoned and the entry guard did not hold. Idempotent via
        ``_drain_complete_warned`` so it cannot double-emit within a session.
        """
        if self._drain_complete_warned:
            return
        self._drain_complete_warned = True
        from agentshore.plays.candidates import build_candidate_plan

        mergeable = build_candidate_plan(state).work_availability.mergeable_pr_count
        if mergeable > 0:
            _logger.error(
                "drain_complete_with_mergeable_prs",
                session_id=self._session_id,
                mergeable_pr_count=mergeable,
                note=(
                    "session draining to completion while merge-ready PRs remain "
                    "unmerged — finished work abandoned; the auto-stop entry guard "
                    "should have prevented this drain"
                ),
            )

    def request_stop(self, reason: str = "stop_requested") -> None:
        """Signal the orchestrator to stop at the next loop iteration.

        Non-blocking. Safe to call from a signal handler. The loop in
        ``run_until_idle`` exits when it next checks ``_stop_requested``;
        actual cleanup runs when ``stop()`` is awaited (typically by
        ``__aexit__``).
        """
        self._host._stop_reason = reason
        self._host._stop_requested = True
        self._host._pause_reason = None
        self._host._pause_event.set()  # wake loop if paused

    def request_drain(self, reason: str = "signal_sigterm") -> None:
        """Schedule a graceful drain from a sync context (e.g. signal handler).

        Non-blocking. Sets the drain flag and wakes the loop; ``begin_drain``
        is called on the next iteration inside ``run_until_idle``.
        """
        if self._host._drain_initialized:
            return
        self._host._draining = True
        self._host._drain_reason = reason
        self._host._pause_reason = None
        self._host._pause_event.set()

    def request_end_session_report(self, *, open_browser: bool = True) -> None:
        """Request a shutdown-time end-of-session report for this session."""
        self._host._end_session_report_requested = True
        self._host._end_session_report_open_browser = (
            self._host._end_session_report_open_browser or open_browser
        )

    def register_esr_ready_callback(
        self, callback: Callable[[str, str, str | None], None] | None
    ) -> None:
        """Wire a callback fired when the in-shutdown ESR file becomes available.

        Receives ``(session_id, report_path, log_path)``. Set by the sidecar's
        ``run_session_start`` (issue #561) so the desktop shell can navigate
        to ``/session/esr`` from drain without the engine touching
        ``webbrowser.open``. The callback runs synchronously inside the
        shutdown loop; exceptions are caught and logged but never raised.
        """
        self._host._esr_ready_callback = callback

    async def begin_drain(self, reason: str) -> None:
        """Start graceful drain: PPO will only dispatch end_agent until all agents stop.

        Idempotent — safe to call multiple times (e.g., from signal handler and
        dashboard simultaneously). Does not cancel in-flight plays.
        """
        if self._host._drain_initialized or self._host._stop_requested:
            return
        self.request_end_session_report(open_browser=True)
        self._host._draining = True
        self._host._drain_reason = reason
        self._host._stop_reason = reason
        self._host._pause_reason = None
        # IMPORTANT: no await may precede this assignment without re-introducing a
        # concurrent-entry race.
        self._host._drain_initialized = True
        await self._host._safe_call(
            self._store.update_session_state(self._session_id, "draining"),
            "update_session_state",
        )
        await self._host._safe_call(
            self._host._state_provider.on_session_draining(reason), "on_session_draining"
        )
        self._host._pause_event.set()
        _logger.info("session_draining", reason=reason, session_id=self._session_id)

    async def hard_stop(self) -> None:
        """Immediate forced shutdown — cancels in-flight plays and kills agents."""
        await self.stop()

    def adjust_budget(self, delta_usd: float) -> bool:
        """Increase session budget; return True when a budget pause should resume."""
        if delta_usd > 0:
            self._host._extra_budget += delta_usd
            self._host._budget_override = False  # allow budget checks to run again
            _logger.info("budget_adjusted", delta_usd=delta_usd, session_id=self._session_id)
            return (
                not getattr(self._host, "_draining", False)
                and not getattr(self._host, "_stop_requested", False)
                and not self._host._pause_event.is_set()
                and getattr(self._host, "_pause_reason", None)
                in {"budget_exhausted", "budget_predictive"}
            )
        else:
            _logger.info("budget_adjust_ignored", delta_usd=delta_usd, session_id=self._session_id)
            return False

    # ------------------------------------------------------------------
    # Live budget control — delegates to BudgetControl collaborator
    # ------------------------------------------------------------------

    async def set_budget(
        self,
        *,
        dollars_enabled: bool,
        dollars: float | None,
        time_enabled: bool,
        time_minutes: int | None,
        persist: bool = True,
    ) -> dict[str, object]:
        """Absolute-set the live dollar/time caps. Delegates to BudgetControl."""
        return await self._budget.set_budget(
            dollars_enabled=dollars_enabled,
            dollars=dollars,
            time_enabled=time_enabled,
            time_minutes=time_minutes,
            persist=persist,
        )

    async def add_budget(
        self,
        *,
        delta_usd: float | None = None,
        delta_minutes: int | None = None,
        persist: bool = True,
    ) -> dict[str, object]:
        """Additively top up the dollar cap and/or time cap. Delegates to BudgetControl."""
        return await self._budget.add_budget(
            delta_usd=delta_usd,
            delta_minutes=delta_minutes,
            persist=persist,
        )

    async def current_budget(self) -> dict[str, object]:
        """Return the live-effective caps + spend/remaining. Delegates to BudgetControl."""
        return await self._budget.current_budget()

    async def _exit_drain(self) -> None:
        """Reverse a budget/time reserve drain, returning the session to running."""
        self._host._draining = False
        self._host._drain_initialized = False
        self._host._drain_reason = None
        self._host._stop_reason = ""
        self._host._pause_reason = None
        # Cancel the end-session report queued by begin_drain so a resumed
        # session does not surface a premature report.
        self._host._end_session_report_requested = False
        self._host._pause_event.set()
        await self._host._safe_call(
            self._store.update_session_state(self._session_id, "running"),
            "update_session_state",
        )
        _logger.info("budget_drain_reversed", session_id=self._session_id)

    async def stop(self, grace_period_s: float = SHUTDOWN_GRACE_PERIOD_SECONDS) -> None:
        """Gracefully shut down the orchestrator.

        Re-entrancy safe: the first caller drives the cleanup; concurrent
        callers wait for it to finish via ``_stop_done`` rather than racing
        through the cleanup body. The whole body is shielded so a cancellation
        of the awaiting caller does not interrupt the WAL checkpoint mid-flight.
        """
        if self._host._stopped:
            await self._host._stop_done.wait()
            return
        self._host._stopped = True
        self._host._stop_requested = True
        self._host._pause_reason = None
        if not self._host._drain_initialized:
            self._host._stop_reason = "stop_requested"
        self._host._pause_event.set()  # Wake loop if paused so it can check _stop_requested
        completion_idle = getattr(self._host, "_completion_processing_idle", None)
        if (
            completion_idle is not None
            and getattr(self._host, "_completion_processing_count", 0) > 0
        ):
            try:
                await asyncio.wait_for(completion_idle.wait(), timeout=grace_period_s)
            except TimeoutError:
                _logger.warning(
                    "completion_processing_shutdown_timeout",
                    session_id=self._session_id,
                    pending=getattr(self._host, "_completion_processing_count", 0),
                )

        await asyncio.shield(self.do_stop(grace_period_s))

    async def do_stop(self, grace_period_s: float) -> None:
        """Actual shutdown body. Must only be called by ``stop()``."""
        try:
            await self.stop_inner(grace_period_s)
        finally:
            self._host._stop_done.set()

    async def generate_end_session_report(self) -> Path:
        """Generate the static ESR while the DataStore is still open."""
        from agentshore.reports.generator import ReportGenerator

        generator = ReportGenerator(self._store)
        output_dir = project_reports_dir(self._repo_root)
        return await generator.generate_end_session_report(
            self._session_id,
            output_dir,
            open_browser=False,
        )

    async def stop_inner(self, grace_period_s: float) -> None:
        def _ms(t0: float) -> float:
            return round((time.perf_counter() - t0) * 1000, 1)

        t_shutdown = time.perf_counter()
        end_session_report_path: Path | None = None
        _logger.info(
            "shutdown_begin",
            n_in_flight=len(self._host._in_flight),
            n_agents=len(self._manager.handles),
            grace_period_s=grace_period_s,
        )

        t = time.perf_counter()
        if self._host._health is not None:
            self._host._health.stop()
        _logger.info("shutdown_step", step="health_stop", elapsed_ms=_ms(t))

        # Loop-liveness watchdog (#9): cancel before the rest of teardown so it
        # cannot re-trigger a drain mid-shutdown. Safe even when this stop() was
        # initiated by the watchdog itself — cancelling a task from within its
        # own awaited call is a no-op until it next suspends, and the watchdog
        # returns immediately after awaiting stop().
        self._host.stop_loop_liveness_watchdog()

        t = time.perf_counter()
        if self._host._integrity is not None:
            self._host._integrity.stop()
        _logger.info("shutdown_step", step="integrity_stop", elapsed_ms=_ms(t))

        t = time.perf_counter()
        if self._host._power_assertion is not None:
            self._host._power_assertion.release()
        _logger.info("shutdown_step", step="power_assertion_release", elapsed_ms=_ms(t))

        # Drain any in-flight plays
        t = time.perf_counter()
        n_pending_after = 0
        if self._host._in_flight:
            tasks = list(self._host._in_flight.values())
            try:
                done, pending = await asyncio.wait(tasks, timeout=grace_period_s)
                n_pending_after = len(pending)
                for task in pending:
                    task.cancel()
            except (TimeoutError, ValueError) as exc:
                _logger.warning("in_flight_shutdown_failed", error=str(exc))
                for task in tasks:
                    if not task.done():
                        task.cancel()
            self._host._in_flight.clear()
            self._host._dispatch_ctx.clear()
        _logger.info(
            "shutdown_step",
            step="drain_in_flight",
            elapsed_ms=_ms(t),
            n_pending_after=n_pending_after,
        )

        # Clear all agent handles concurrently
        t = time.perf_counter()
        agent_ids = list(self._manager.handles)
        if agent_ids:

            async def _clear_one(aid: str) -> None:
                try:
                    await self._manager.clear(aid)
                except (OrchestratorError, OSError, KeyError, aiosqlite.Error) as exc:
                    _logger.warning("agent_clear_failed", agent_id=aid, error=str(exc))

            await asyncio.gather(*[_clear_one(aid) for aid in agent_ids])
        _logger.info(
            "shutdown_step", step="clear_agents", elapsed_ms=_ms(t), n_agents=len(agent_ids)
        )

        t = time.perf_counter()
        await self._host._safe_call(
            self._store.abandon_unfinished_plays(
                self._session_id,
                reason="unfinished play abandoned during shutdown",
            ),
            "abandon_unfinished_plays_shutdown",
        )
        await self._host._safe_call(
            self._store.abandon_active_work_claims(self._session_id),
            "abandon_active_work_claims_shutdown",
        )
        _logger.info("shutdown_step", step="abandon_orphaned_work", elapsed_ms=_ms(t))

        t = time.perf_counter()
        # Tests patch ``agentshore.core.phases._clear_session_scoped_bead_progress``
        # (its binding home) to intercept this.
        from agentshore.core import phases

        reset_count = await phases._clear_session_scoped_bead_progress(
            repo_root=self._repo_root,
            sid=self._session_id,
            phase="session_shutdown",
        )
        _logger.info(
            "shutdown_step",
            step="clear_beads_in_progress",
            elapsed_ms=_ms(t),
            count=reset_count,
        )

        # Compute final alignment from beads graph global closure ratio
        t = time.perf_counter()
        final_alignment = 0.0
        try:
            state = await self._state_builder.build_state()
            if state.graph is not None:
                final_alignment = state.graph.global_closure_ratio
        except Exception as exc:
            _logger.warning("final_alignment_error", error=str(exc))
        _logger.info(
            "shutdown_step",
            step="final_alignment",
            elapsed_ms=_ms(t),
            alignment=round(final_alignment, 4),
        )

        t = time.perf_counter()
        try:
            await self._store.complete_session(self._session_id, final_alignment)
        except aiosqlite.Error as exc:
            _logger.warning("complete_session_failed", error=str(exc))
        _logger.info("shutdown_step", step="complete_session", elapsed_ms=_ms(t))

        if getattr(self._host, "_end_session_report_requested", False):
            t = time.perf_counter()
            try:
                await self._host._completion.refresh_issues()
                end_session_report_path = await self.generate_end_session_report()
                _logger.info(
                    "shutdown_step",
                    step="end_session_report_generate",
                    elapsed_ms=_ms(t),
                    path=str(end_session_report_path),
                )
            except Exception as exc:
                _logger.error(
                    "end_session_report_failed",
                    error=str(exc),
                    session_id=self._session_id,
                    exc_info=True,
                )

        # Persist a final policy checkpoint so short sessions (below
        # checkpoint_interval) still warm-start the next run. The end_session
        # play path already saves when PPO selects shutdown; this is the
        # safety net for user-initiated stops and natural exits.
        t = time.perf_counter()
        try:
            selector = self._host._selector
            if isinstance(selector, _ppo_selector_cls()):
                if len(selector.buffer) > 0:
                    await selector.update_policy(next_state_value=0.0)
                state_for_checkpoint = await self._state_builder.build_state()
                await selector.save_checkpoint(
                    self._store,
                    self._session_id,
                    self._host._weights_dir(),
                    state_for_checkpoint.total_plays,
                )
                _logger.info("shutdown_step", step="final_checkpoint", elapsed_ms=_ms(t))
        except Exception as exc:
            _logger.warning("final_checkpoint_failed", error=str(exc), session_id=self._session_id)

        t = time.perf_counter()
        _emit_weights_dir_inventory(self._host._weights_dir(), phase="shutdown_step")
        _logger.info("shutdown_step", step="weights_inventory", elapsed_ms=_ms(t))

        t = time.perf_counter()
        try:
            await self._store.close()
        except (aiosqlite.Error, OSError) as exc:
            _logger.warning("store_close_failed", error=str(exc))
        _logger.info("shutdown_step", step="store_close", elapsed_ms=_ms(t))

        await self._host._safe_call(
            self._host._state_provider.on_session_ended(self._host._stop_reason),
            "on_session_ended",
        )

        if end_session_report_path is not None and getattr(
            self._host, "_end_session_report_open_browser", False
        ):
            # Embedded mode (issue #561): the desktop shell renders the ESR
            # in-app at ``/session/esr``. Skip ``webbrowser.open`` — opening
            # Safari/Chrome here yanks the user out of the app at the exact
            # moment they're about to start the next session. Instead fire
            # the registered esr_ready callback so the sidecar can emit a
            # ``$/esr_ready`` JSON-RPC notification to the Tauri shell.
            esr_callback = getattr(self._host, "_esr_ready_callback", None)
            if getattr(self._host, "_embedded_mode", False):
                if esr_callback is not None:
                    try:
                        esr_callback(
                            self._session_id,
                            str(end_session_report_path.resolve()),
                            (
                                str(log_path.resolve())
                                if (log_path := getattr(self._host, "_log_path", None)) is not None
                                else None
                            ),
                        )
                        _logger.info(
                            "shutdown_step",
                            step="end_session_report_emit",
                            path=str(end_session_report_path),
                        )
                    except Exception as exc:
                        _logger.error(
                            "end_session_report_emit_failed",
                            error=str(exc),
                            path=str(end_session_report_path),
                            exc_info=True,
                        )
                else:
                    _logger.info(
                        "shutdown_step",
                        step="end_session_report_skip_browser",
                        path=str(end_session_report_path),
                    )
            else:
                import webbrowser

                t = time.perf_counter()
                try:
                    await asyncio.to_thread(
                        webbrowser.open, end_session_report_path.resolve().as_uri()
                    )
                    _logger.info(
                        "shutdown_step",
                        step="end_session_report_open",
                        elapsed_ms=_ms(t),
                        path=str(end_session_report_path),
                    )
                except Exception as exc:
                    _logger.error(
                        "end_session_report_open_failed",
                        error=str(exc),
                        path=str(end_session_report_path),
                        exc_info=True,
                    )
        _logger.info("shutdown_complete", total_elapsed_ms=_ms(t_shutdown))
