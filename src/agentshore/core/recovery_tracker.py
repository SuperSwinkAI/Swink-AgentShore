"""Agent-recovery latches shared by completion and observation paths."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agentshore.state import AgentSnapshot


# Consecutive take_break failures after which END_AGENT is unmasked.
BREAK_RECOVERY_FAILURE_LIMIT = 2


class RecoveryTracker:
    """Owns the take_break-failure and rate-limit-recovery latches."""

    def __init__(self) -> None:
        self._break_recovery_failures: dict[str, int] = {}
        self._rate_limit_recovery_enqueued: set[str] = set()
        self._unknown_error_recovery_enqueued: set[str] = set()
        self._noop_recovery_enqueued: set[str] = set()

    # ------------------------------------------------------------------
    # Rate-limit-recovery latch
    # ------------------------------------------------------------------

    def is_rate_limit_enqueued(self, agent_id: str) -> bool:
        return agent_id in self._rate_limit_recovery_enqueued

    def mark_rate_limit_enqueued(self, agent_id: str) -> None:
        self._rate_limit_recovery_enqueued.add(agent_id)

    def clear_rate_limit_enqueued(self, agent_id: str) -> None:
        self._rate_limit_recovery_enqueued.discard(agent_id)

    # ------------------------------------------------------------------
    # Unknown-error-recovery latch (distinct path from rate-limit, #23/#24)
    # ------------------------------------------------------------------

    def is_unknown_error_enqueued(self, agent_id: str) -> bool:
        return agent_id in self._unknown_error_recovery_enqueued

    def mark_unknown_error_enqueued(self, agent_id: str) -> None:
        self._unknown_error_recovery_enqueued.add(agent_id)

    def clear_unknown_error_enqueued(self, agent_id: str) -> None:
        self._unknown_error_recovery_enqueued.discard(agent_id)

    # ------------------------------------------------------------------
    # No-op-recovery latch (clean-exit empty no-op → standard take_break)
    # ------------------------------------------------------------------

    def is_noop_enqueued(self, agent_id: str) -> bool:
        return agent_id in self._noop_recovery_enqueued

    def mark_noop_enqueued(self, agent_id: str) -> None:
        self._noop_recovery_enqueued.add(agent_id)

    def clear_noop_enqueued(self, agent_id: str) -> None:
        self._noop_recovery_enqueued.discard(agent_id)

    # ------------------------------------------------------------------
    # take_break consecutive-failure counter
    # ------------------------------------------------------------------

    def clear_break_failures(self, agent_id: str) -> None:
        self._break_recovery_failures.pop(agent_id, None)

    def record_break_failure(self, agent_id: str) -> int:
        """Increment the agent's consecutive-failure count and return the new total."""
        failures = self._break_recovery_failures.get(agent_id, 0) + 1
        self._break_recovery_failures[agent_id] = failures
        return failures

    def break_failure_count(self, agent_id: str) -> int:
        return self._break_recovery_failures.get(agent_id, 0)

    # ------------------------------------------------------------------
    # State / observation read
    # ------------------------------------------------------------------

    def recovery_exhausted_agent_ids(self, agents: Iterable[AgentSnapshot]) -> frozenset[str]:
        """Agents whose take_break failures have reached the END_AGENT-unmask limit."""
        return frozenset(
            a.agent_id
            for a in agents
            if self._break_recovery_failures.get(a.agent_id, 0) >= BREAK_RECOVERY_FAILURE_LIMIT
        )
