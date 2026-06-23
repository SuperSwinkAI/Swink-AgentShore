"""Health monitor — background asyncio task polling agent liveness and context pressure."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

from agentshore.logging import get_logger
from agentshore.state import CLI_AGENT_TYPES, AgentStatus, AgentType

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agentshore.agents.circuit_breaker import CircuitBreaker
    from agentshore.agents.handle import AgentHandle

_logger = get_logger(__name__)

# Context is considered "pressured" when utilisation exceeds this ratio
_CONTEXT_PRESSURE_THRESHOLD = 0.80


class HealthMonitor:
    """Polls agent handles on a fixed interval and calls back into the manager.

    Parameters
    ----------
    handles:
        Live reference to ``AgentManager._handles``.
    circuit_breakers:
        Live reference to ``AgentManager._circuit_breakers``.
    on_crash:
        Async callback invoked when a CLI agent's process has terminated
        unexpectedly.  Receives ``(agent_id, return_code)``.
    on_context_pressure:
        Async callback invoked when a handle's context utilisation exceeds 80%.
        Receives ``(agent_id, utilisation_ratio)``.
    on_recovery:
        Async callback invoked when an ERROR agent is eligible for recovery.
        Receives ``(agent_id,)``.  If not provided, error recovery is disabled.
    poll_interval:
        Seconds between polls (default 30).
    max_context_per_type:
        Map from ``AgentType`` to max_context tokens, used to compute
        utilisation.  Typically derived from ``AGENT_CAPABILITIES``.
    sleep_fn:
        Injectable sleep function for tests (defaults to ``asyncio.sleep``).
    """

    def __init__(
        self,
        handles: dict[str, AgentHandle],
        circuit_breakers: dict[str, CircuitBreaker],
        *,
        on_crash: Callable[[str, int], Awaitable[None]],
        on_context_pressure: Callable[[str, float], Awaitable[None]],
        on_recovery: Callable[[str], Awaitable[None]] | None = None,
        poll_interval: float = 30.0,
        max_context_per_type: dict[AgentType, int] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        monotonic_fn: Callable[[], float] | None = None,
    ) -> None:
        self._handles = handles
        self._circuit_breakers = circuit_breakers
        self._on_crash = on_crash
        self._on_context_pressure = on_context_pressure
        self._on_recovery = on_recovery
        self._poll_interval = poll_interval
        self._max_context_per_type: dict[AgentType, int] = max_context_per_type or {}
        self._sleep = sleep_fn or asyncio.sleep
        self._monotonic = monotonic_fn or time.monotonic
        self._task: asyncio.Task[None] | None = None

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def start(self) -> None:
        """Launch the background poll task."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="agentshore.health_monitor"
            )

    def stop(self) -> None:
        """Cancel the background poll task."""
        if self._task is not None and not self._task.done():
            self._task.cancel()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # -------------------------------------------------------------------------
    # Core poll loop
    # -------------------------------------------------------------------------

    async def _run(self) -> None:
        _logger.debug("health_monitor_started", poll_interval_s=self._poll_interval)
        try:
            while True:
                await self._sleep(self._poll_interval)
                await self._poll_all()
        except asyncio.CancelledError:
            _logger.debug("health_monitor_stopped")

    async def _poll_all(self) -> None:
        for agent_id, handle in list(self._handles.items()):
            if handle.status == AgentStatus.TERMINATED:
                continue
            await self._check_liveness(agent_id, handle)
            await self._check_busy_watchdog(agent_id, handle)
            if handle.status == AgentStatus.ERROR:
                await self._check_error_recovery(agent_id, handle)
            await self._check_context_pressure(agent_id, handle)

    async def _check_liveness(self, agent_id: str, handle: AgentHandle) -> None:
        """Detect a crashed CLI process and invoke on_crash."""
        if handle.agent_type not in CLI_AGENT_TYPES:
            return
        process = handle.process
        if process is None:
            return
        rc = process.returncode
        if rc is not None:
            # Process has exited on its own — unexpected crash
            _logger.warning(
                "agent_crash_detected",
                agent_id=agent_id,
                returncode=rc,
            )
            cb = self._circuit_breakers.get(agent_id)
            if cb is not None:
                cb.record_failure()
            handle.transition_to(AgentStatus.ERROR)
            await self._on_crash(agent_id, rc)

    async def _check_busy_watchdog(self, agent_id: str, handle: AgentHandle) -> None:
        """Reap an agent stuck BUSY past its dispatch deadline.

        Normally a dispatch's own timeouts (first-byte / stream-idle / wall-
        clock) fire and ``manager.dispatch`` transitions the agent out of BUSY.
        But if that machinery hangs — e.g. a SIGKILL that never reaps the
        process group leaves ``_kill_process`` blocked — the agent is pinned in
        BUSY forever. A single permanently-BUSY agent suppresses *every* session-
        end backstop (the selector treats it as doing real work, so the reverse-
        failsafe never arms and END_SESSION never lifts), so the session can only
        be ended by the time-budget reserve hours later (session a3202694).

        This is the deterministic catch-all: once an agent is BUSY past
        ``dispatch_deadline_monotonic`` (effective wall-clock timeout + teardown
        slack), force it to ERROR so the fleet can wind down, regardless of *why*
        the dispatch hung. ``_check_liveness`` runs first, so a cleanly-exited
        process is already handled; this only catches the hung case where the
        process is still (apparently) alive but the dispatch coroutine is wedged.
        """
        if handle.agent_type not in CLI_AGENT_TYPES:
            return
        if handle.status != AgentStatus.BUSY:
            return
        deadline = handle.dispatch_deadline_monotonic
        if deadline is None or self._monotonic() <= deadline:
            return
        process = handle.process
        pid = process.pid if process is not None else None
        _logger.error(
            "agent_busy_watchdog_reaped",
            agent_id=agent_id,
            pid=pid,
            current_play_type=(
                handle.current_play_type.value if handle.current_play_type is not None else None
            ),
        )
        # Best-effort kill of the leaked subprocess. The dispatch's own
        # _kill_process is the primary reaper (and is now bounded); this is a
        # belt-and-suspenders SIGKILL for any path where it never ran.
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                process.kill()
        cb = self._circuit_breakers.get(agent_id)
        if cb is not None:
            cb.record_failure()
        handle.dispatch_deadline_monotonic = None
        handle.transition_to(AgentStatus.ERROR)
        rc = process.returncode if process is not None and process.returncode is not None else -1
        await self._on_crash(agent_id, rc)

    async def _check_context_pressure(self, agent_id: str, handle: AgentHandle) -> None:
        """Flag agents whose context utilisation exceeds the pressure threshold."""
        max_ctx = self._max_context_per_type.get(handle.agent_type)
        if not max_ctx:
            return
        utilisation = handle.context_size / max_ctx
        if utilisation >= _CONTEXT_PRESSURE_THRESHOLD:
            _logger.info(
                "agent_context_pressure",
                agent_id=agent_id,
                utilisation=round(utilisation, 3),
            )
            await self._on_context_pressure(agent_id, utilisation)

    async def _check_error_recovery(self, agent_id: str, handle: AgentHandle) -> None:
        """Attempt to recover an ERROR agent when the circuit breaker allows it."""
        if self._on_recovery is None:
            return
        cb = self._circuit_breakers.get(agent_id)
        if cb is None:
            return
        if cb.should_attempt_recovery():
            _logger.info("agent_recovery_eligible", agent_id=agent_id)
            await self._on_recovery(agent_id)
