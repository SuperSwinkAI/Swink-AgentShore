"""Agent-recovery latches shared by completion and observation paths."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.errors import ErrorClass
from agentshore.plays.override import OverrideKind

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from agentshore.core.override_queue import OverrideQueue
    from agentshore.state import AgentSnapshot, AgentStatus


BREAK_RECOVERY_FAILURE_LIMIT = 2

_RECOVERY_OVERRIDE_KIND: dict[ErrorClass, OverrideKind] = {
    ErrorClass.RATE_LIMIT: OverrideKind.RATE_LIMIT_RECOVERY,
    ErrorClass.AUTH: OverrideKind.RATE_LIMIT_RECOVERY,
    ErrorClass.UNKNOWN: OverrideKind.UNKNOWN_ERROR_RECOVERY,
    ErrorClass.CODEX_ROLLOUT: OverrideKind.UNKNOWN_ERROR_RECOVERY,
    ErrorClass.TRANSIENT_NETWORK: OverrideKind.UNKNOWN_ERROR_RECOVERY,
    ErrorClass.NO_OP: OverrideKind.NOOP_RECOVERY,
}

_RECOVERY_EVENT: dict[OverrideKind, str] = {
    OverrideKind.RATE_LIMIT_RECOVERY: "rate_limit_recovery_enqueued",
    OverrideKind.UNKNOWN_ERROR_RECOVERY: "unknown_error_recovery_enqueued",
    OverrideKind.NOOP_RECOVERY: "agent_noop_break_enqueued",
}


class RecoveryTracker:
    """Owns the take_break-failure and rate-limit-recovery latches."""

    def __init__(self) -> None:
        self._break_recovery_failures: dict[str, int] = {}
        self._break_recovery_retired_logged: set[str] = set()
        self._rate_limit_recovery_enqueued: set[str] = set()
        self._unknown_error_recovery_enqueued: set[str] = set()
        self._noop_recovery_enqueued: set[str] = set()

    def is_rate_limit_enqueued(self, agent_id: str) -> bool:
        return agent_id in self._rate_limit_recovery_enqueued

    def mark_rate_limit_enqueued(self, agent_id: str) -> None:
        self._rate_limit_recovery_enqueued.add(agent_id)

    def clear_rate_limit_enqueued(self, agent_id: str) -> None:
        self._rate_limit_recovery_enqueued.discard(agent_id)

    def is_unknown_error_enqueued(self, agent_id: str) -> bool:
        return agent_id in self._unknown_error_recovery_enqueued

    def mark_unknown_error_enqueued(self, agent_id: str) -> None:
        self._unknown_error_recovery_enqueued.add(agent_id)

    def clear_unknown_error_enqueued(self, agent_id: str) -> None:
        self._unknown_error_recovery_enqueued.discard(agent_id)

    def is_noop_enqueued(self, agent_id: str) -> bool:
        return agent_id in self._noop_recovery_enqueued

    def mark_noop_enqueued(self, agent_id: str) -> None:
        self._noop_recovery_enqueued.add(agent_id)

    def clear_noop_enqueued(self, agent_id: str) -> None:
        self._noop_recovery_enqueued.discard(agent_id)

    def clear_break_failures(self, agent_id: str) -> None:
        self._break_recovery_failures.pop(agent_id, None)
        self._break_recovery_retired_logged.discard(agent_id)

    def record_break_failure(self, agent_id: str) -> int:
        """Increment the agent's consecutive-failure count and return the new total."""
        failures = self._break_recovery_failures.get(agent_id, 0) + 1
        self._break_recovery_failures[agent_id] = failures
        return failures

    def break_failure_count(self, agent_id: str) -> int:
        return self._break_recovery_failures.get(agent_id, 0)

    def is_break_recovery_exhausted(self, agent_id: str) -> bool:
        """True once take_break has failed ``BREAK_RECOVERY_FAILURE_LIMIT`` times in a row.

        The exhaustion verdict is the terminal state of the break cycle: no
        further take_break is enqueued for this agent (see
        ``maybe_enqueue_error_recovery``), END_AGENT is unmasked for it
        (``recovery_exhausted_agent_ids``), and the counter stays elevated until
        the agent is actually retired or a break succeeds.
        """
        return self._break_recovery_failures.get(agent_id, 0) >= BREAK_RECOVERY_FAILURE_LIMIT

    def maybe_enqueue_error_recovery(
        self,
        agent_id: str,
        final_status: AgentStatus,
        *,
        handles: Mapping[str, object],
        overrides: OverrideQueue,
        session_id: str,
    ) -> None:
        """Enqueue a take_break override for recoverable agent errors."""
        from agentshore.core.helpers import _logger  # noqa: PLC0415
        from agentshore.plays.base import PlayParams  # noqa: PLC0415
        from agentshore.plays.override import OverrideEntry  # noqa: PLC0415
        from agentshore.state import AgentStatus as _AgentStatus  # noqa: PLC0415
        from agentshore.state import PlayType

        if final_status != _AgentStatus.ERROR:
            self.clear_rate_limit_enqueued(agent_id)
            self.clear_unknown_error_enqueued(agent_id)
            self.clear_noop_enqueued(agent_id)
            return
        handle = handles.get(agent_id)
        if handle is None:
            return
        error_class = getattr(handle, "last_error_class", None)

        kind = next((k for ec, k in _RECOVERY_OVERRIDE_KIND.items() if ec == error_class), None)
        if kind is None:
            return
        if self.is_break_recovery_exhausted(agent_id):
            if agent_id not in self._break_recovery_retired_logged:
                self._break_recovery_retired_logged.add(agent_id)
                _logger.warning(
                    "break_recovery_retired",
                    session_id=session_id,
                    agent_id=agent_id,
                    error_class=error_class,
                    consecutive_failures=self.break_failure_count(agent_id),
                    limit=BREAK_RECOVERY_FAILURE_LIMIT,
                    detail=(
                        "take_break recovery exhausted; no further break enqueued — "
                        "END_AGENT is unmasked for this agent"
                    ),
                )
            return
        latches = {
            OverrideKind.RATE_LIMIT_RECOVERY: (
                self.is_rate_limit_enqueued,
                self.mark_rate_limit_enqueued,
            ),
            OverrideKind.UNKNOWN_ERROR_RECOVERY: (
                self.is_unknown_error_enqueued,
                self.mark_unknown_error_enqueued,
            ),
            OverrideKind.NOOP_RECOVERY: (self.is_noop_enqueued, self.mark_noop_enqueued),
        }
        is_enqueued, mark = latches[kind]
        event = _RECOVERY_EVENT[kind]

        if is_enqueued(agent_id):
            return
        params = PlayParams(
            agent_id=agent_id,
            extras={
                "trigger_agent_id": agent_id,
                "trigger_error_class": error_class,
            },
        )
        overrides.put_nowait(
            OverrideEntry(
                play_type=PlayType.TAKE_BREAK,
                params=params,
                kind=kind,
            )
        )
        mark(agent_id)
        _logger.info(
            event,
            session_id=session_id,
            agent_id=agent_id,
            error_class=error_class,
        )

    def recovery_exhausted_agent_ids(self, agents: Iterable[AgentSnapshot]) -> frozenset[str]:
        """Agents whose take_break failures have reached the END_AGENT-unmask limit."""
        return frozenset(a.agent_id for a in agents if self.is_break_recovery_exhausted(a.agent_id))
