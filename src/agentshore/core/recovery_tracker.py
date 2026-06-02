"""Agent-recovery latches: take_break failure counts + rate-limit enqueue set.

``RecoveryTracker`` owns the two short-lived per-agent recovery latches that the
completion path maintains and the state/observation path reads:

* ``_break_recovery_failures`` — consecutive ``take_break`` failures per agent.
  Once an agent reaches :data:`BREAK_RECOVERY_FAILURE_LIMIT` the count is left
  elevated so the core tick can unmask END_AGENT and let the PPO retire it
  (desktop-s1u7). Cleared on a successful break or when the agent is ended.
* ``_rate_limit_recovery_enqueued`` — agents the loop has already enqueued a
  RATE_LIMIT_RECOVERY override for; cleared once the agent recovers so the next
  rate_limit event re-arms the override.

It is a thin collaborator (mirroring :class:`agentshore.core.github_syncer.GitHubSyncer`):
constructed in ``phases.py`` and held on the orchestrator as ``_recovery``.
Hosting the :data:`BREAK_RECOVERY_FAILURE_LIMIT` constant and the
:meth:`recovery_exhausted_agent_ids` query here dissolves the former
state→completion import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agentshore.state import AgentSnapshot


# Consecutive take_break failures after which END_AGENT is unmasked for the
# wedged agent so the PPO can decide to retire it.
BREAK_RECOVERY_FAILURE_LIMIT = 2


class RecoveryTracker:
    """Owns the take_break-failure and rate-limit-recovery latches."""

    def __init__(self) -> None:
        self._break_recovery_failures: dict[str, int] = {}
        self._rate_limit_recovery_enqueued: set[str] = set()

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
