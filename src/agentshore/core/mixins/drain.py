"""Graceful drain, stop/hard_stop, budget adjustment, and end-session report."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import aiosqlite

from agentshore.core.base import _OrchestratorBase
from agentshore.core.helpers import _emit_weights_dir_inventory, _logger, _ppo_selector_cls
from agentshore.errors import OrchestratorError
from agentshore.paths import project_reports_dir

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agentshore.agents.health import HealthMonitor
    from agentshore.agents.manager import AgentManager
    from agentshore.config import RuntimeConfig
    from agentshore.core.context import _DispatchContext
    from agentshore.data.store import DataStore
    from agentshore.plays.selector import PlaySelector
    from agentshore.state import (
        PlayOutcome,
        StateProvider,
    )


SHUTDOWN_GRACE_PERIOD_SECONDS = 5.0


class _DrainMixin(_OrchestratorBase):
    """Drain, stop, hard_stop, budget adjust, end-session report generation."""

    _cfg: RuntimeConfig
    _session_id: str
    _repo_root: Path
    _store: DataStore
    _manager: AgentManager
    _selector: PlaySelector | None
    _state_provider: StateProvider
    _stop_requested: bool
    _stopped: bool
    _draining: bool
    _drain_reason: str | None
    _drain_initialized: bool
    _stop_reason: str
    _extra_budget: float
    _budget_override: bool
    _pause_event: asyncio.Event
    _pause_reason: str | None
    _in_flight: dict[str, asyncio.Task[PlayOutcome]]
    _dispatch_ctx: dict[str, _DispatchContext]
    _health: HealthMonitor | None
    _stop_done: asyncio.Event
    _end_session_report_requested: bool
    _end_session_report_open_browser: bool
    _embedded_mode: bool
    _esr_ready_callback: Callable[[str, str, str | None], None] | None
    _completion_processing_idle: asyncio.Event
    _completion_processing_count: int

    # ------------------------------------------------------------------

    def request_stop(self, reason: str = "stop_requested") -> None:
        """Signal the orchestrator to stop at the next loop iteration.

        Non-blocking. Safe to call from a signal handler. The loop in
        ``run_until_idle`` exits when it next checks ``_stop_requested``;
        actual cleanup runs when ``stop()`` is awaited (typically by
        ``__aexit__``).
        """
        self._stop_reason = reason
        self._stop_requested = True
        self._pause_reason = None
        self._pause_event.set()  # wake loop if paused

    def request_drain(self, reason: str = "signal_sigterm") -> None:
        """Schedule a graceful drain from a sync context (e.g. signal handler).

        Non-blocking. Sets the drain flag and wakes the loop; ``begin_drain``
        is called on the next iteration inside ``run_until_idle``.
        """
        if self._drain_initialized:
            return
        self._draining = True
        self._drain_reason = reason
        self._pause_reason = None
        self._pause_event.set()

    def request_end_session_report(self, *, open_browser: bool = True) -> None:
        """Request a shutdown-time end-of-session report for this session."""
        self._end_session_report_requested = True
        self._end_session_report_open_browser = (
            self._end_session_report_open_browser or open_browser
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
        self._esr_ready_callback = callback

    async def begin_drain(self, reason: str) -> None:
        """Start graceful drain: PPO will only dispatch end_agent until all agents stop.

        Idempotent — safe to call multiple times (e.g., from signal handler and
        dashboard simultaneously). Does not cancel in-flight plays.
        """
        if self._drain_initialized or self._stop_requested:
            return
        self.request_end_session_report(open_browser=True)
        self._draining = True
        self._drain_reason = reason
        self._stop_reason = reason
        self._pause_reason = None
        # IMPORTANT: no await may precede this assignment without re-introducing a
        # concurrent-entry race.
        self._drain_initialized = True
        await self._safe_call(
            self._store.update_session_state(self._session_id, "draining"),
            "update_session_state",
        )
        await self._safe_call(
            self._state_provider.on_session_draining(reason), "on_session_draining"
        )
        self._pause_event.set()
        _logger.info("session_draining", reason=reason, session_id=self._session_id)

    async def hard_stop(self) -> None:
        """Immediate forced shutdown — cancels in-flight plays and kills agents."""
        await self.stop()

    def adjust_budget(self, delta_usd: float) -> bool:
        """Increase session budget; return True when a budget pause should resume."""
        if delta_usd > 0:
            self._extra_budget += delta_usd
            self._budget_override = False  # allow budget checks to run again
            _logger.info("budget_adjusted", delta_usd=delta_usd, session_id=self._session_id)
            return (
                not getattr(self, "_draining", False)
                and not getattr(self, "_stop_requested", False)
                and not self._pause_event.is_set()
                and getattr(self, "_pause_reason", None)
                in {"budget_exhausted", "budget_predictive"}
            )
        else:
            _logger.info("budget_adjust_ignored", delta_usd=delta_usd, session_id=self._session_id)
            return False

    async def stop(self, grace_period_s: float = SHUTDOWN_GRACE_PERIOD_SECONDS) -> None:
        """Gracefully shut down the orchestrator.

        Re-entrancy safe: the first caller drives the cleanup; concurrent
        callers wait for it to finish via ``_stop_done`` rather than racing
        through the cleanup body. The whole body is shielded so a cancellation
        of the awaiting caller does not interrupt the WAL checkpoint mid-flight.
        """
        if self._stopped:
            await self._stop_done.wait()
            return
        self._stopped = True
        self._stop_requested = True
        self._pause_reason = None
        if not self._drain_initialized:
            self._stop_reason = "stop_requested"
        self._pause_event.set()  # Wake loop if paused so it can check _stop_requested
        completion_idle = getattr(self, "_completion_processing_idle", None)
        if completion_idle is not None and getattr(self, "_completion_processing_count", 0) > 0:
            try:
                await asyncio.wait_for(completion_idle.wait(), timeout=grace_period_s)
            except TimeoutError:
                _logger.warning(
                    "completion_processing_shutdown_timeout",
                    session_id=self._session_id,
                    pending=getattr(self, "_completion_processing_count", 0),
                )

        await asyncio.shield(self._do_stop(grace_period_s))

    async def _do_stop(self, grace_period_s: float) -> None:
        """Actual shutdown body. Must only be called by ``stop()``."""
        try:
            await self._stop_inner(grace_period_s)
        finally:
            self._stop_done.set()

    async def _generate_end_session_report(self) -> Path:
        """Generate the static ESR while the DataStore is still open."""
        from agentshore.reports.generator import ReportGenerator

        generator = ReportGenerator(self._store)
        output_dir = project_reports_dir(self._repo_root)
        return await generator.generate_end_session_report(
            self._session_id,
            output_dir,
            open_browser=False,
        )

    async def _stop_inner(self, grace_period_s: float) -> None:
        def _ms(t0: float) -> float:
            return round((time.perf_counter() - t0) * 1000, 1)

        t_shutdown = time.perf_counter()
        end_session_report_path: Path | None = None
        _logger.info(
            "shutdown_begin",
            n_in_flight=len(self._in_flight),
            n_agents=len(self._manager.handles),
            grace_period_s=grace_period_s,
        )

        t = time.perf_counter()
        if self._health is not None:
            self._health.stop()
        _logger.info("shutdown_step", step="health_stop", elapsed_ms=_ms(t))

        # Loop-liveness watchdog (#9): cancel before the rest of teardown so it
        # cannot re-trigger a drain mid-shutdown. Safe even when this stop() was
        # initiated by the watchdog itself — cancelling a task from within its
        # own awaited call is a no-op until it next suspends, and the watchdog
        # returns immediately after awaiting stop().
        self.stop_loop_liveness_watchdog()

        t = time.perf_counter()
        if self._integrity is not None:
            self._integrity.stop()
        _logger.info("shutdown_step", step="integrity_stop", elapsed_ms=_ms(t))

        t = time.perf_counter()
        if self._power_assertion is not None:
            self._power_assertion.release()
        _logger.info("shutdown_step", step="power_assertion_release", elapsed_ms=_ms(t))

        # Drain any in-flight plays
        t = time.perf_counter()
        n_pending_after = 0
        if self._in_flight:
            tasks = list(self._in_flight.values())
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
            self._in_flight.clear()
            self._dispatch_ctx.clear()
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
        await self._safe_call(
            self._store.abandon_unfinished_plays(
                self._session_id,
                reason="unfinished play abandoned during shutdown",
            ),
            "abandon_unfinished_plays_shutdown",
        )
        await self._safe_call(
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
            state = await self._build_state()
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

        if getattr(self, "_end_session_report_requested", False):
            t = time.perf_counter()
            try:
                await self._refresh_issues()
                end_session_report_path = await self._generate_end_session_report()
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
            if isinstance(self._selector, _ppo_selector_cls()):
                if len(self._selector.buffer) > 0:
                    await self._selector.update_policy(next_state_value=0.0)
                state_for_checkpoint = await self._build_state()
                await self._selector.save_checkpoint(
                    self._store,
                    self._session_id,
                    self._weights_dir(),
                    state_for_checkpoint.total_plays,
                )
                _logger.info("shutdown_step", step="final_checkpoint", elapsed_ms=_ms(t))
        except Exception as exc:
            _logger.warning("final_checkpoint_failed", error=str(exc), session_id=self._session_id)

        t = time.perf_counter()
        _emit_weights_dir_inventory(self._weights_dir(), phase="shutdown_step")
        _logger.info("shutdown_step", step="weights_inventory", elapsed_ms=_ms(t))

        t = time.perf_counter()
        try:
            await self._store.close()
        except (aiosqlite.Error, OSError) as exc:
            _logger.warning("store_close_failed", error=str(exc))
        _logger.info("shutdown_step", step="store_close", elapsed_ms=_ms(t))

        await self._safe_call(
            self._state_provider.on_session_ended(self._stop_reason),
            "on_session_ended",
        )

        if end_session_report_path is not None and getattr(
            self, "_end_session_report_open_browser", False
        ):
            # Embedded mode (issue #561): the desktop shell renders the ESR
            # in-app at ``/session/esr``. Skip ``webbrowser.open`` — opening
            # Safari/Chrome here yanks the user out of the app at the exact
            # moment they're about to start the next session. Instead fire
            # the registered esr_ready callback so the sidecar can emit a
            # ``$/esr_ready`` JSON-RPC notification to the Tauri shell.
            esr_callback = getattr(self, "_esr_ready_callback", None)
            if getattr(self, "_embedded_mode", False):
                if esr_callback is not None:
                    try:
                        esr_callback(
                            self._session_id,
                            str(end_session_report_path.resolve()),
                            (
                                str(log_path.resolve())
                                if (log_path := getattr(self, "_log_path", None)) is not None
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
