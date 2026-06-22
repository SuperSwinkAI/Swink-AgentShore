"""Unit coverage for the clock-parameterised cooldown primitive."""

from __future__ import annotations

import pytest

from agentshore.cooldown import Clock, Cooldown, CooldownSpec


def _spec(threshold: int = 3, cooldown: int = 20, clock: Clock = Clock.PLAYS) -> CooldownSpec:
    return CooldownSpec(threshold=threshold, cooldown=cooldown, clock=clock)


def test_spec_rejects_nonpositive_params() -> None:
    with pytest.raises(ValueError):
        CooldownSpec(threshold=0, cooldown=20, clock=Clock.PLAYS)
    with pytest.raises(ValueError):
        CooldownSpec(threshold=3, cooldown=0, clock=Clock.PLAYS)


def test_streak_arms_at_threshold() -> None:
    cd: Cooldown[int] = Cooldown(_spec(threshold=3, cooldown=20))
    assert cd.record_failure(7, now=0) == 1
    assert cd.record_failure(7, now=1) == 2
    assert not cd.is_armed(7, now=2)
    # Third failure trips the cooldown; the streak resets to 0.
    assert cd.record_failure(7, now=2) == 0
    assert cd.is_armed(7, now=2)
    assert cd.streak(7) == 0


def test_cooldown_expires_at_window_end() -> None:
    cd: Cooldown[int] = Cooldown(_spec(threshold=1, cooldown=20))
    cd.record_failure(7, now=100)  # threshold 1 → arms immediately, expires at 120
    assert cd.is_armed(7, now=119)
    # Window end is inclusive-expiry: now >= until releases.
    assert not cd.is_armed(7, now=120)
    assert not cd.is_armed(7, now=121)


def test_re_arms_only_after_threshold_again() -> None:
    cd: Cooldown[int] = Cooldown(_spec(threshold=2, cooldown=10))
    cd.record_failure(7, now=0)
    cd.record_failure(7, now=0)  # trips, armed until 10
    assert cd.is_armed(7, now=5)
    assert not cd.is_armed(7, now=10)  # expired
    # A single post-expiry failure is below threshold — not re-armed yet.
    assert cd.record_failure(7, now=11) == 1
    assert not cd.is_armed(7, now=11)
    assert cd.record_failure(7, now=12) == 0  # second failure re-trips
    assert cd.is_armed(7, now=12)


def test_armed_keys_prunes_expired() -> None:
    cd: Cooldown[int] = Cooldown(_spec(threshold=1, cooldown=10))
    cd.record_failure(1, now=0)  # armed until 10
    cd.record_failure(2, now=5)  # armed until 15
    assert cd.armed_keys(now=8) == {1, 2}
    assert cd.armed_keys(now=12) == {2}  # key 1 expired and pruned
    assert cd.armed_keys(now=20) == set()


def test_clear_drops_streak_and_cooldown() -> None:
    cd: Cooldown[int] = Cooldown(_spec(threshold=3, cooldown=20))
    cd.record_failure(7, now=0)
    cd.record_failure(7, now=0)
    assert cd.streak(7) == 2
    cd.clear(7)
    assert cd.streak(7) == 0
    cd.record_failure(7, now=0)
    cd.record_failure(7, now=0)
    cd.record_failure(7, now=0)  # trips
    assert cd.is_armed(7, now=0)
    cd.clear(7)
    assert not cd.is_armed(7, now=0)


def test_tracked_keys_spans_streaks_and_armed() -> None:
    cd: Cooldown[int] = Cooldown(_spec(threshold=3, cooldown=20))
    cd.record_failure(1, now=0)  # streak only
    cd.record_failure(2, now=0)
    cd.record_failure(2, now=0)
    cd.record_failure(2, now=0)  # armed
    assert cd.tracked_keys() == {1, 2}


def test_keys_are_independent() -> None:
    cd: Cooldown[int] = Cooldown(_spec(threshold=2, cooldown=10))
    cd.record_failure(1, now=0)
    cd.record_failure(2, now=0)
    cd.record_failure(2, now=0)  # only key 2 trips
    assert not cd.is_armed(1, now=0)
    assert cd.is_armed(2, now=0)


def test_spec_clock_is_carried() -> None:
    cd: Cooldown[str] = Cooldown(_spec(clock=Clock.TICKS))
    assert cd.spec.clock is Clock.TICKS
