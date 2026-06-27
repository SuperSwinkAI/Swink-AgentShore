"""Tests for the per-agent circuit breaker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentshore.agents.circuit_breaker import CircuitBreaker, CircuitState


def _cb(**kwargs: object) -> tuple[CircuitBreaker, list[datetime]]:
    """Create a CircuitBreaker with a fake clock that starts at t=0."""
    clock: list[datetime] = [datetime(2026, 1, 1, tzinfo=UTC)]

    def now() -> datetime:
        return clock[0]

    cb = CircuitBreaker(now_fn=now, **kwargs)  # type: ignore[arg-type]
    return cb, clock


def _advance(clock: list[datetime], seconds: float) -> None:
    clock[0] = clock[0] + timedelta(seconds=seconds)


def test_starts_closed() -> None:
    cb, _ = _cb()
    assert cb.state == CircuitState.CLOSED
    assert cb.allows_dispatch is True
    assert cb.is_open is False


def test_opens_after_threshold_failures() -> None:
    cb, _ = _cb(failures=3)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.is_open is True
    assert cb.allows_dispatch is False


def test_success_clears_failure_count() -> None:
    cb, _ = _cb(failures=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    cb.record_failure()
    # Only 2 failures since last success — still closed
    assert cb.state == CircuitState.CLOSED


def test_success_closes_open_breaker() -> None:
    cb, _ = _cb(failures=1)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    cb.record_success()
    assert cb.state == CircuitState.CLOSED


def test_transitions_to_half_open_after_cooldown() -> None:
    cb, clock = _cb(failures=1, cooldown_seconds=60)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    _advance(clock, 60)
    assert cb.state == CircuitState.HALF_OPEN
    assert cb.allows_dispatch is True


def test_stays_open_before_cooldown_expires() -> None:
    cb, clock = _cb(failures=1, cooldown_seconds=60)
    cb.record_failure()
    _advance(clock, 59)
    assert cb.state == CircuitState.OPEN


def test_half_open_success_closes() -> None:
    cb, clock = _cb(failures=1, cooldown_seconds=60)
    cb.record_failure()
    _advance(clock, 61)
    assert cb.state == CircuitState.HALF_OPEN
    cb.record_success()
    assert cb.state == CircuitState.CLOSED


def test_half_open_failure_reopens() -> None:
    cb, clock = _cb(failures=1, cooldown_seconds=60)
    cb.record_failure()
    _advance(clock, 61)
    assert cb.state == CircuitState.HALF_OPEN
    cb.record_failure()
    assert cb.state == CircuitState.OPEN


def test_old_failures_fall_outside_window() -> None:
    cb, clock = _cb(failures=3, window_seconds=100)
    cb.record_failure()
    cb.record_failure()
    _advance(clock, 101)  # slide past the window
    cb.record_failure()  # now only 1 failure in window
    assert cb.state == CircuitState.CLOSED


def test_threshold_one_trips_immediately() -> None:
    cb, _ = _cb(failures=1)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN


def test_should_attempt_recovery_when_half_open() -> None:
    """After cooldown elapses the breaker is HALF_OPEN and recovery is eligible."""
    cb, clock = _cb(failures=1, cooldown_seconds=60)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    _advance(clock, 61)
    assert cb.state == CircuitState.HALF_OPEN
    assert cb.should_attempt_recovery() is True


def test_recovery_blocked_when_open() -> None:
    """While the breaker is OPEN (cooldown hasn't elapsed), no recovery."""
    cb, clock = _cb(failures=1, cooldown_seconds=60)
    cb.record_failure()
    _advance(clock, 30)  # still within cooldown
    assert cb.state == CircuitState.OPEN
    assert cb.should_attempt_recovery() is False


def test_recovery_exponential_backoff() -> None:
    """After one recovery attempt, the next requires 2x cooldown."""
    cb, clock = _cb(failures=1, cooldown_seconds=60)
    cb.record_failure()
    _advance(clock, 61)
    assert cb.should_attempt_recovery() is True

    cb.record_recovery_attempt()
    # Now need 60 * 2^1 = 120s since last recovery attempt
    _advance(clock, 60)  # only 60s since recovery attempt
    # Stay HALF_OPEN (no failure) and check the backoff guard alone.
    assert cb.should_attempt_recovery() is False

    _advance(clock, 61)  # total 121s since recovery attempt
    assert cb.should_attempt_recovery() is True


def test_record_success_resets_recovery() -> None:
    """After a successful dispatch, recovery state should be fully reset."""
    cb, clock = _cb(failures=1, cooldown_seconds=60)
    cb.record_failure()
    _advance(clock, 61)
    cb.record_recovery_attempt()
    assert cb._recovery_attempts == 1
    assert cb._last_recovery_at is not None

    cb.record_success()
    assert cb._recovery_attempts == 0
    assert cb._last_recovery_at is None
