"""Agent-recovery latches shared by completion and observation paths."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.errors import ErrorClass
from agentshore.plays.override import OverrideKind

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from agentshore.core.override_queue import OverrideQueue
    from agentshore.state import AgentSnapshot, AgentStatus


# Consecutive take_break failures after which END_AGENT is unmasked.
BREAK_RECOVERY_FAILURE_LIMIT = 2

# Single source of truth for loop-produced take_break recovery: which error
# classes are recoverable, and the take_break OverrideKind each routes to. Crash,
# auth, invalid-model, and timeout classes are intentionally absent (they fall to
# the END_AGENT path, no take_break). The NO_OP class rides its own kind so the
# take_break it triggers is distinctly labelled (agent_noop_break_enqueued) and
# never confused with a real quota/rate-limit in telemetry (desktop no-op
# resilience).
#
# This map's KEY SET is asserted equal to ``state.RECOVERABLE_ERROR_CLASSES`` in
# tests/test_recovery_routing.py, so a class can never be recoverable-for-
# eligibility but unroutable here (the CODEX_ROLLOUT drift this collapse fixes).
_RECOVERY_OVERRIDE_KIND: dict[ErrorClass, OverrideKind] = {
    ErrorClass.RATE_LIMIT: OverrideKind.RATE_LIMIT_RECOVERY,
    ErrorClass.UNKNOWN: OverrideKind.UNKNOWN_ERROR_RECOVERY,
    ErrorClass.CODEX_ROLLOUT: OverrideKind.UNKNOWN_ERROR_RECOVERY,
    ErrorClass.TRANSIENT_NETWORK: OverrideKind.UNKNOWN_ERROR_RECOVERY,
    ErrorClass.NO_OP: OverrideKind.NOOP_RECOVERY,
}

# Per-kind structured log event emitted when a take_break override is enqueued.
_RECOVERY_EVENT: dict[OverrideKind, str] = {
    OverrideKind.RATE_LIMIT_RECOVERY: "rate_limit_recovery_enqueued",
    OverrideKind.UNKNOWN_ERROR_RECOVERY: "unknown_error_recovery_enqueued",
    OverrideKind.NOOP_RECOVERY: "agent_noop_break_enqueued",
}


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
    # Error-recovery enqueueing (folded from completion.py)
    # ------------------------------------------------------------------

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

        # ``ec == error_class`` membership (not ``.get``) so a bare-string
        # ``last_error_class`` resolves the same way the old frozenset ``in``
        # checks did (ErrorClass is a StrEnum).
        kind = next((k for ec, k in _RECOVERY_OVERRIDE_KIND.items() if ec == error_class), None)
        if kind is None:
            # Not a recovery-eligible class (auth, invalid_model, crash_*,
            # timeout*) — leave it for the END_AGENT path, no take_break.
            return
        # Per-kind dedup latch (route the is/mark pair off the resolved kind).
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
