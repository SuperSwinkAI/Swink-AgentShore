"""Health monitor — background asyncio task polling agent liveness and context pressure."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from agentshore.logging import get_logger
from agentshore.state import AgentStatus, AgentType

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agentshore.agents.circuit_breaker import CircuitBreaker
    from agentshore.agents.handle import AgentHandle

_logger = get_logger(__name__)

_CLI_AGENT_TYPES: frozenset[AgentType] = frozenset(
    {AgentType.CLAUDE_CODE, AgentType.CODEX, AgentType.GEMINI, AgentType.GROK}
)

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
    ) -> None:
        self._handles = handles
        self._circuit_breakers = circuit_breakers
        self._on_crash = on_crash
        self._on_context_pressure = on_context_pressure
        self._on_recovery = on_recovery
        self._poll_interval = poll_interval
        self._max_context_per_type: dict[AgentType, int] = max_context_per_type or {}
        self._sleep = sleep_fn or asyncio.sleep
        self._task: asyncio.Task[None] | None = None

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def start(self) -> None:
        """Launch the background poll task."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_event_loop().create_task(
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
            if handle.status == AgentStatus.ERROR:
                await self._check_error_recovery(agent_id, handle)
            await self._check_context_pressure(agent_id, handle)

    async def _check_liveness(self, agent_id: str, handle: AgentHandle) -> None:
        """Detect a crashed CLI process and invoke on_crash."""
        if handle.agent_type not in _CLI_AGENT_TYPES:
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
