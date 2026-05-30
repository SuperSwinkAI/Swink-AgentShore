"""Per-agent circuit breaker — CLOSED → OPEN → HALF_OPEN state machine."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Tracks failure rate for a single agent and blocks dispatch when tripped.

    ``now_fn`` is injectable so tests can control wall-clock time without sleeping.
    """

    def __init__(
        self,
        *,
        failures: int = 3,
        window_seconds: int = 300,
        cooldown_seconds: int = 60,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._threshold = failures
        self._window = window_seconds
        self._cooldown = cooldown_seconds
        self._now = now_fn or (lambda: datetime.now(UTC))
        self._state = CircuitState.CLOSED
        self._failure_times: deque[datetime] = deque()
        self._opened_at: datetime | None = None
        self._recovery_attempts: int = 0
        self._last_recovery_at: datetime | None = None

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            self._maybe_transition_to_half_open()
        return self._state

    @property
    def is_open(self) -> bool:
        return self.state == CircuitState.OPEN

    @property
    def allows_dispatch(self) -> bool:
        return self.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    # ------------------------------------------------------------------
    # Feedback from the adapter
    # ------------------------------------------------------------------

    def record_success(self) -> None:
        """A dispatch completed successfully — close the breaker if needed."""
        self._failure_times.clear()
        self._state = CircuitState.CLOSED
        self._opened_at = None
        self._recovery_attempts = 0
        self._last_recovery_at = None

    def record_failure(self) -> None:
        """A dispatch failed — may trip the breaker."""
        now = self._now()
        self._failure_times.append(now)
        self._prune_old_failures(now)
        if self._state == CircuitState.HALF_OPEN or (len(self._failure_times) >= self._threshold):
            self._state = CircuitState.OPEN
            self._opened_at = now

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def should_attempt_recovery(self) -> bool:
        """Return True when the breaker is HALF_OPEN and backoff has elapsed."""
        if self.state != CircuitState.HALF_OPEN:
            return False
        if self._last_recovery_at is None:
            return True
        elapsed = (self._now() - self._last_recovery_at).total_seconds()
        backoff = self._cooldown * (2 ** min(self._recovery_attempts, 4))
        return bool(elapsed >= backoff)

    def record_recovery_attempt(self) -> None:
        """Record that an error-recovery probe was attempted."""
        self._last_recovery_at = self._now()
        self._recovery_attempts += 1

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prune_old_failures(self, now: datetime) -> None:
        cutoff = now.timestamp() - self._window
        while self._failure_times and self._failure_times[0].timestamp() < cutoff:
            self._failure_times.popleft()

    def _maybe_transition_to_half_open(self) -> None:
        if self._opened_at is None:
            return
        elapsed = (self._now() - self._opened_at).total_seconds()
        if elapsed >= self._cooldown:
            self._state = CircuitState.HALF_OPEN
