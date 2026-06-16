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
    budget_reserve_reached,
    time_budget_reserve_reached,
)
from agentshore.config.models import BudgetConfig
from agentshore.core.helpers import _emit_weights_dir_inventory, _logger, _ppo_selector_cls
from agentshore.errors import OrchestratorError
from agentshore.paths import project_reports_dir

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from agentshore.agents.manager import AgentManager
    from agentshore.core.mixins.completion import CompletionProcessor
    from agentshore.core.mixins.state import StateBuilder
    from agentshore.core.session_runtime import SessionRuntime
    from agentshore.data.store import DataStore
    from agentshore.state import (
        OrchestratorState,
    )


SHUTDOWN_GRACE_PERIOD_SECONDS = 5.0

# How often the bounded graceful-drain watchdog (#180) re-checks the deadline.
# Mirrors ``_LOOP_LIVENESS_CHECK_INTERVAL_SECONDS`` in loop.py: a coarse poll is
# fine because the deadline is a coarse (minutes-scale) backstop, and a short
# interval keeps the escalation prompt once the deadline passes.
_GRACEFUL_DRAIN_CHECK_INTERVAL_SECONDS = 15.0


class _DrainHost(Protocol):
    """Orchestrator *behaviour* the :class:`DrainController` invokes.

    All shared session *state* now lives on :class:`SessionRuntime` (reached via
    ``self._runtime``); this Protocol is the narrow behaviour seam that remains so
    the cross-component methods and the ``_completion`` sibling reference resolve
    on the composition root without a circular import. ``_OrchestratorBase``
    structurally satisfies it.
    """

    async def _safe_call(self, coro: Awaitable[object], label: str) -> None: ...

    def effective_budget_caps(self) -> BudgetConfig:
        """Live-effective budget caps (overrides shadowing ``cfg.budget``)."""
        ...

    async def resume(self) -> None:
        """Canonical resume — updates DB, resets cadence counters, emits event."""
        ...

    _completion: CompletionProcessor

    def _weights_dir(self) -> Path: ...

    def stop_loop_liveness_watchdog(self) -> None: ...


class DrainController:
    """Drain, stop, hard_stop, budget adjust, end-session report generation.

    Stable services / collaborators are captured via the constructor; all shared
    session state (read or written) lives on the injected :class:`SessionRuntime`,
    and the cross-component behaviour methods resolve via the narrow
    :class:`_DrainHost` behaviour seam.
    """

    def __init__(
        self,
        *,
        host: _DrainHost,
        runtime: SessionRuntime,
        store: DataStore,
        manager: AgentManager,
        session_id: str,
        repo_root: Path,
        state_builder: StateBuilder,
    ) -> None:
        self._host = host
        self._runtime = runtime
        self._store = store
        self._manager = manager
        self._session_id = session_id
        self._repo_root = repo_root
        self._state_builder = state_builder
        # One-shot guard for the drain-complete defensive-visibility warning
        # (``_on_drain_complete``) so it can never double-emit within a session.
        self._drain_complete_warned = False
        # Bounded graceful-drain watchdog task (#180). Launched once when the
        # graceful drain begins; escalates to the bounded hard stop if the drain
        # has not completed within ``feedback.graceful_drain_timeout_seconds``.
        # Runs OFF the core loop (like the loop-liveness watchdog) so a drain
        # whose single in-flight play never finishes still gets reaped.
        self._graceful_drain_watchdog_task: asyncio.Task[None] | None = None

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
            _logger.info(
                "drain_complete_with_mergeable_prs",
                session_id=self._session_id,
                mergeable_pr_count=mergeable,
            )

    def request_stop(self, reason: str = "stop_requested") -> None:
        """Signal the orchestrator to stop at the next loop iteration.

        Non-blocking. Safe to call from a signal handler. The loop in
        ``run_until_idle`` exits when it next checks ``_stop_requested``;
        actual cleanup runs when ``stop()`` is awaited (typically by
        ``__aexit__``).
        """
        self._runtime.stop_reason = reason
        self._runtime.stop_requested = True
        self._runtime.pause_reason = None
        self._runtime.pause_event.set()  # wake loop if paused

    def request_drain(self, reason: str = "signal_sigterm") -> None:
        """Schedule a graceful drain from a sync context (e.g. signal handler).

        Non-blocking. Sets the drain flag and wakes the loop; ``begin_drain``
        is called on the next iteration inside ``run_until_idle``.
        """
        if self._runtime.drain_initialized:
            return
        self._runtime.draining = True
        self._runtime.drain_reason = reason
        self._runtime.pause_reason = None
        self._runtime.pause_event.set()

    def request_end_session_report(self, *, open_browser: bool = True) -> None:
        """Request a shutdown-time end-of-session report for this session."""
        self._runtime.end_session_report_requested = True
        self._runtime.end_session_report_open_browser = (
            self._runtime.end_session_report_open_browser or open_browser
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
        self._runtime.esr_ready_callback = callback

    async def begin_drain(self, reason: str) -> None:
        """Start graceful drain: PPO will only dispatch end_agent until all agents stop.

        Idempotent — safe to call multiple times (e.g., from signal handler and
        dashboard simultaneously). Does not cancel in-flight plays.
        """
        if self._runtime.drain_initialized or self._runtime.stop_requested:
            return
        self.request_end_session_report(open_browser=True)
        self._runtime.draining = True
        self._runtime.drain_reason = reason
        self._runtime.stop_reason = reason
        self._runtime.pause_reason = None
        # IMPORTANT: no await may precede this assignment without re-introducing a
        # concurrent-entry race.
        self._runtime.drain_initialized = True
        await self._host._safe_call(
            self._store.update_session_state(self._session_id, "draining"),
            "update_session_state",
        )
        await self._host._safe_call(
            self._runtime.state_provider.on_session_draining(reason), "on_session_draining"
        )
        self._runtime.pause_event.set()
        _logger.info("session_draining", reason=reason, session_id=self._session_id)
        # Arm the bounded graceful-drain deadline (#180): if the drain has not
        # completed within the configured timeout the watchdog escalates to the
        # bounded hard stop, so a stuck in-flight play can no longer hang
        # ``agentshore stop`` for hours.
        self._start_graceful_drain_watchdog()

    def graceful_drain_timeout_seconds(self) -> float | None:
        """Resolve the configured graceful-drain deadline (None disables it)."""
        return self._runtime.cfg.feedback.graceful_drain_timeout_seconds

    def _start_graceful_drain_watchdog(self) -> None:
        """Launch the bounded graceful-drain watchdog task (#180).

        Idempotent. No-op when the deadline is unset (``None`` ⇒ unbounded
        graceful drain) or a live task already exists. The task runs OFF the
        core loop so a drain whose only in-flight play never finishes — the
        wedge that previously hung ``stop`` ~1h — still gets reaped.
        """
        if self.graceful_drain_timeout_seconds() is None:
            return
        existing = self._graceful_drain_watchdog_task
        if existing is not None and not existing.done():
            return
        self._graceful_drain_watchdog_task = asyncio.get_event_loop().create_task(
            self._graceful_drain_watchdog(),
            name="agentshore.graceful_drain_watchdog",
        )

    def _stop_graceful_drain_watchdog(self) -> None:
        """Cancel the graceful-drain watchdog task if running.

        No-op when invoked from within the watchdog task itself (the escalation
        path calls ``stop()`` → here): self-cancellation would deliver a
        ``CancelledError`` at ``stop()``'s next await and abort teardown before
        ``do_stop`` runs, wedging the session half-stopped. The watchdog returns
        immediately after awaiting ``stop()``, so it needs no cancellation then;
        for any other caller (sidecar, signal handler) the watchdog is a
        different task and is cancelled as before.
        """
        task = self._graceful_drain_watchdog_task
        if task is None or task.done():
            return
        if task is asyncio.current_task():
            return
        task.cancel()

    async def _graceful_drain_watchdog(self) -> None:
        """Escalate a graceful drain to the bounded hard stop past its deadline (#180).

        Records a monotonic deadline at drain start and polls on a fixed
        interval. While the drain is still in progress with in-flight plays
        outstanding, once the deadline passes it emits
        ``graceful_drain_deadline_escalation`` (with elapsed seconds + in-flight
        count) and falls through to the existing bounded hard stop
        (``do_stop`` cancels in-flight plays under the shutdown grace period).
        Exits early when the drain completes, is reversed (``draining`` cleared
        by ``_exit_drain``), or a stop is already underway — ``stop`` is
        re-entrancy safe regardless.
        """
        timeout = self.graceful_drain_timeout_seconds()
        if timeout is None:
            return
        deadline = time.monotonic() + timeout
        interval = _GRACEFUL_DRAIN_CHECK_INTERVAL_SECONDS
        while not self._runtime.stop_requested and not self._runtime.stopped:
            await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            if self._runtime.stop_requested or self._runtime.stopped:
                return
            # Drain reversed (e.g. a raised budget cap un-drained the session) or
            # already finished — the deadline no longer applies.
            if not self._runtime.draining:
                return
            if time.monotonic() < deadline:
                continue
            in_flight = len(self._runtime.in_flight)
            elapsed = time.monotonic() - (deadline - timeout)
            _logger.warning(
                "graceful_drain_deadline_escalation",
                session_id=self._session_id,
                elapsed_seconds=round(elapsed, 1),
                timeout_seconds=timeout,
                in_flight=in_flight,
                note=(
                    "graceful drain did not complete within "
                    "feedback.graceful_drain_timeout_seconds; escalating to the "
                    "bounded hard stop (cancelling in-flight plays)"
                ),
            )
            # Drive the bounded teardown directly. ``stop`` is re-entrancy safe
            # and shielded; it bounds the in-flight wait by the shutdown grace
            # period, so this cannot itself hang.
            await self._host._safe_call(self.stop(), "graceful_drain_deadline_stop")
            return

    async def hard_stop(self) -> None:
        """Immediate forced shutdown — cancels in-flight plays and kills agents."""
        await self.stop()

    # ------------------------------------------------------------------
    # Live budget control (Feature B: #41 sidecar RPC, #42 CLI add-budget)
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
        """Absolute-set the live dollar/time caps (sidecar RPC + desktop dialog).

        Validates bounds, applies the caps via the override fields, and re-arms
        (or reverses) the drain when a raised cap moves the session back outside
        its reserve. Persists to ``agentshore.yaml`` when *persist* so caps
        survive restart.
        """
        self._validate_dollar(dollars_enabled, dollars)
        self._validate_time(time_enabled, time_minutes)
        self._runtime.budget_override_enabled = dollars_enabled
        self._runtime.budget_override_total = (
            float(dollars) if dollars_enabled and dollars is not None else 0.0
        )
        self._runtime.time_override_enabled = time_enabled
        self._runtime.time_override_minutes = (
            int(time_minutes) if time_enabled and time_minutes is not None else 0
        )
        resumed = await self._rearm_after_budget_change()
        if persist:
            self._persist_budget()
        _logger.info(
            "budget_set",
            dollars_enabled=dollars_enabled,
            dollars=self._runtime.budget_override_total,
            time_enabled=time_enabled,
            time_minutes=self._runtime.time_override_minutes,
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
            new_total = base + float(delta_usd)  # type: ignore[arg-type]
            if new_total < MIN_ENABLED_BUDGET_USD:
                raise OrchestratorError(
                    f"resulting dollar cap ${new_total:.2f} is below the "
                    f"${MIN_ENABLED_BUDGET_USD:.2f} minimum"
                )
            self._runtime.budget_override_enabled = True
            self._runtime.budget_override_total = new_total
        if has_time:
            base_min = caps.time_total_minutes if caps.time_enabled else 0
            new_minutes = int(base_min) + int(delta_minutes)  # type: ignore[arg-type]
            if not (MIN_TIME_BUDGET_MINUTES <= new_minutes <= MAX_TIME_BUDGET_MINUTES):
                raise OrchestratorError(
                    f"resulting time cap {new_minutes} min is outside "
                    f"{MIN_TIME_BUDGET_MINUTES}-{MAX_TIME_BUDGET_MINUTES} (1h-72h)"
                )
            self._runtime.time_override_enabled = True
            self._runtime.time_override_minutes = new_minutes
        resumed = await self._rearm_after_budget_change()
        if persist:
            self._persist_budget()
        _logger.info(
            "budget_added",
            delta_usd=delta_usd,
            delta_minutes=delta_minutes,
            session_id=self._session_id,
        )
        return await self._apply_and_publish(resumed=resumed)

    async def current_budget(self) -> dict[str, object]:
        """Return the live-effective caps + spend/remaining (prefill/echo)."""
        state = await self._state_builder.build_state()
        # A pure read never resumes/reverses anything.
        return self._applied_from_state(state, resumed=False)

    # --- live-budget helpers ----------------------------------------------

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
        if minutes is None or not (MIN_TIME_BUDGET_MINUTES <= minutes <= MAX_TIME_BUDGET_MINUTES):
            raise OrchestratorError(
                f"time cap must be between {MIN_TIME_BUDGET_MINUTES} and "
                f"{MAX_TIME_BUDGET_MINUTES} minutes (1h-72h) when enabled"
            )

    def _persist_budget(self) -> None:
        config_path = self._runtime.config_path
        if config_path is None:
            return
        caps = self._host.effective_budget_caps()
        persisted = BudgetConfig(
            enabled=caps.enabled,
            total=caps.total,
            warning_threshold=caps.warning_threshold,
            time_enabled=caps.time_enabled,
            time_total_minutes=caps.time_total_minutes,
        )
        from agentshore.config.budget_writer import write_budget_to_config

        write_budget_to_config(config_path, persisted)

    async def _apply_and_publish(self, *, resumed: bool = False) -> dict[str, object]:
        """Build a fresh state, push it so the dashboard repaints immediately, echo it.

        A live cap change must not wait for the next loop tick to surface — emit
        ``on_state_update`` right away so the budget bar reflects the new caps the
        instant the RPC/command returns. ``resumed`` reflects whether the cap
        change un-paused or reversed a drain (see ``_rearm_after_budget_change``).
        """
        state = await self._state_builder.build_state()
        await self._host._safe_call(
            self._runtime.state_provider.on_state_update(state), "on_state_update_budget"
        )
        return self._applied_from_state(state, resumed=resumed)

    @staticmethod
    def _applied_from_state(
        state: OrchestratorState, *, resumed: bool = False
    ) -> dict[str, object]:
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
        """Resume a budget pause or reverse a budget/time drain if now in-bounds.

        Routes resume through the single canonical :meth:`_DrainHost.resume`
        path so the DB session row, feedback-cadence counters, and the
        ``session_resumed`` event are always updated consistently. Returns
        ``True`` when the cap change actually un-paused or reversed a drain.
        """
        # Case 1: paused on budget exhaustion → resume when no longer reserve-bound.
        paused_on_budget = (
            not self._runtime.draining
            and not self._runtime.stop_requested
            and not self._runtime.pause_event.is_set()
            and self._runtime.pause_reason in {"budget_exhausted", "budget_predictive"}
        )
        if paused_on_budget and not await self._reserve_still_reached():
            await self._host.resume()
            return True
        # Case 2: draining on a budget/time reserve → reverse when back in-bounds.
        if (
            self._runtime.draining
            and not self._runtime.stop_requested
            and self._runtime.drain_reason
            in {"budget_reserve_reached", "time_budget_reserve_reached"}
            and not await self._reserve_still_reached()
        ):
            await self._exit_drain()
            await self._host.resume()
            return True
        return False

    async def _reserve_still_reached(self) -> bool:
        state = await self._state_builder.build_state()
        b = state.budget
        if b is None:
            return False
        dollar = b.enabled and budget_reserve_reached(spent=b.spent, total_budget=b.total_budget)
        timev = (
            b.time_enabled
            and b.time_total_minutes is not None
            and b.time_elapsed_minutes is not None
            and time_budget_reserve_reached(
                elapsed_minutes=b.time_elapsed_minutes, total_minutes=b.time_total_minutes
            )
        )
        return bool(dollar or timev)

    async def _exit_drain(self) -> None:
        """Reverse a budget/time reserve drain, returning the session to running."""
        # A reversed drain must not be reaped by the bounded-drain watchdog (#180).
        self._stop_graceful_drain_watchdog()
        self._runtime.draining = False
        self._runtime.drain_initialized = False
        self._runtime.drain_reason = None
        self._runtime.stop_reason = ""
        self._runtime.pause_reason = None
        # Cancel the end-session report queued by begin_drain so a resumed
        # session does not surface a premature report.
        self._runtime.end_session_report_requested = False
        self._runtime.pause_event.set()
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
        if self._runtime.stopped:
            await self._runtime.stop_done.wait()
            return
        # Cancel the bounded-drain watchdog (#180) now that a stop is underway so
        # it cannot re-enter ``stop`` after this body completes. Deliberate no-op
        # when the watchdog itself initiated this stop — self-cancellation would
        # land a ``CancelledError`` at the completion-gate await below and skip
        # teardown (see ``_stop_graceful_drain_watchdog``).
        self._stop_graceful_drain_watchdog()
        self._runtime.stopped = True
        self._runtime.stop_requested = True
        self._runtime.pause_reason = None
        if not self._runtime.drain_initialized:
            self._runtime.stop_reason = "stop_requested"
        self._runtime.pause_event.set()  # Wake loop if paused so it can check _stop_requested
        completion_idle = self._runtime.completion_processing_idle
        # Invariant: once ``stopped`` is committed above, ``do_stop`` MUST run — it
        # is the only path that cancels in-flight agents, checkpoints the WAL,
        # marks the session stopped, and sets ``stop_done`` (which any concurrent
        # second caller is blocked on). The pre-teardown completion-gate await is
        # cancellable, so drive ``do_stop`` from a ``finally`` to guarantee it runs
        # even if that await is cancelled (self-cancel or an external caller).
        try:
            if self._runtime.completion_processing_count > 0:
                try:
                    await asyncio.wait_for(completion_idle.wait(), timeout=grace_period_s)
                except TimeoutError:
                    _logger.warning(
                        "completion_processing_shutdown_timeout",
                        session_id=self._session_id,
                        pending=self._runtime.completion_processing_count,
                    )
        finally:
            await asyncio.shield(self.do_stop(grace_period_s))

    async def do_stop(self, grace_period_s: float) -> None:
        """Actual shutdown body. Must only be called by ``stop()``."""
        try:
            await self.stop_inner(grace_period_s)
        finally:
            self._runtime.stop_done.set()

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
            n_in_flight=len(self._runtime.in_flight),
            n_agents=len(self._manager.handles),
            grace_period_s=grace_period_s,
        )

        t = time.perf_counter()
        if self._runtime.health is not None:
            self._runtime.health.stop()
        _logger.info("shutdown_step", step="health_stop", elapsed_ms=_ms(t))

        # Loop-liveness watchdog (#9): cancel before the rest of teardown so it
        # cannot re-trigger a drain mid-shutdown. Safe even when this stop() was
        # initiated by the watchdog itself — cancelling a task from within its
        # own awaited call is a no-op until it next suspends, and the watchdog
        # returns immediately after awaiting stop().
        self._host.stop_loop_liveness_watchdog()

        t = time.perf_counter()
        if self._runtime.integrity is not None:
            self._runtime.integrity.stop()
        _logger.info("shutdown_step", step="integrity_stop", elapsed_ms=_ms(t))

        t = time.perf_counter()
        if self._runtime.power_assertion is not None:
            self._runtime.power_assertion.release()
        _logger.info("shutdown_step", step="power_assertion_release", elapsed_ms=_ms(t))

        # Drain any in-flight plays
        t = time.perf_counter()
        n_pending_after = 0
        if self._runtime.in_flight:
            tasks = list(self._runtime.in_flight.values())
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
            self._runtime.in_flight.clear()
            self._runtime.dispatch_ctx.clear()
        _logger.info(
            "shutdown_step",
            step="drain_in_flight",
            elapsed_ms=_ms(t),
            n_pending_after=n_pending_after,
        )

        # Clear all agent handles concurrently.  force=True because in-flight
        # asyncio tasks were cancelled above; the active-play guard in clear()
        # must not block teardown.
        t = time.perf_counter()
        agent_ids = list(self._manager.handles)
        if agent_ids:

            async def _clear_one(aid: str) -> None:
                try:
                    await self._manager.clear(aid, force=True)
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

        if self._runtime.end_session_report_requested:
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
            selector = self._runtime.selector
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
            self._runtime.state_provider.on_session_ended(self._runtime.stop_reason),
            "on_session_ended",
        )

        if end_session_report_path is not None and self._runtime.end_session_report_open_browser:
            # Embedded mode (issue #561): the desktop shell renders the ESR
            # in-app at ``/session/esr``. Skip ``webbrowser.open`` — opening
            # Safari/Chrome here yanks the user out of the app at the exact
            # moment they're about to start the next session. Instead fire
            # the registered esr_ready callback so the sidecar can emit a
            # ``$/esr_ready`` JSON-RPC notification to the Tauri shell.
            esr_callback = self._runtime.esr_ready_callback
            if self._runtime.embedded_mode:
                if esr_callback is not None:
                    try:
                        esr_callback(
                            self._session_id,
                            str(end_session_report_path.resolve()),
                            (
                                str(log_path.resolve())
                                if (log_path := self._runtime.log_path) is not None
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
