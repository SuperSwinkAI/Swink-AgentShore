"""A clock-parameterised failure→cooldown primitive.

Several "N failures → cool down for M" circuits exist across the codebase
(``tmp/TNQA.md`` Critical group 1). Their cooldown windows are measured against
*different* clocks — wall-clock seconds, ``state.total_plays``, the per-play
``last_play_id`` tick — and two of them happen to use the literal ``20`` against
different clocks, a latent "merge by value" trap.

This primitive makes the clock **explicit at the construction site** via
:class:`Clock`, so a windowed cooldown can never silently be compared against
the wrong clock. The primitive never reads a global clock itself — the caller
passes the current reading of the declared clock on every query, keeping it
pure and trivially testable.

Phase 2 of ``tmp/PLAN-error-cooldown-unification.md``. The per-issue
issue-pickup skip circuit is migrated onto it; the other circuits stay in place
(they are clockless terminal counters or have shared/distributed state for which
this windowed abstraction is not a clean fit — see the plan).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Clock(StrEnum):
    """Which clock a :class:`CooldownSpec` window is measured against.

    The value a caller must pass as ``now`` to :class:`Cooldown` queries:

    * ``SECONDS`` — wall-clock epoch seconds (``datetime.now().timestamp()``).
    * ``PLAYS``   — ``state.total_plays``.
    * ``TICKS``   — the monotonic per-play ``last_play_id``.
    """

    SECONDS = "seconds"
    PLAYS = "plays"
    TICKS = "ticks"


@dataclass(frozen=True)
class CooldownSpec:
    """Configuration for a :class:`Cooldown`.

    ``threshold`` consecutive failures arm a cooldown that lasts ``cooldown``
    units of ``clock``, after which the key becomes eligible again and must
    re-accumulate ``threshold`` failures to re-arm.
    """

    threshold: int
    cooldown: int
    clock: Clock

    def __post_init__(self) -> None:
        if self.threshold < 1:
            raise ValueError("threshold must be >= 1")
        if self.cooldown < 1:
            raise ValueError("cooldown must be >= 1")


class Cooldown[K]:
    """Per-key failure counter that arms a clock-windowed cooldown at a threshold.

    The primitive holds no clock of its own: callers pass the current reading of
    the spec's :class:`Clock` as ``now`` on every mutation/query. Expired
    cooldowns are pruned lazily on read.
    """

    def __init__(self, spec: CooldownSpec) -> None:
        self._spec = spec
        self._streaks: dict[K, int] = {}
        self._armed_until: dict[K, int] = {}

    @property
    def spec(self) -> CooldownSpec:
        return self._spec

    def record_failure(self, key: K, *, now: int) -> int:
        """Increment *key*'s failure streak; arm a cooldown when it reaches the
        threshold (resetting the streak). Returns the new streak — ``0`` on the
        call that trips the cooldown.
        """
        streak = self._streaks.get(key, 0) + 1
        if streak >= self._spec.threshold:
            self._armed_until[key] = now + self._spec.cooldown
            self._streaks.pop(key, None)
            return 0
        self._streaks[key] = streak
        return streak

    def is_armed(self, key: K, *, now: int) -> bool:
        """True if *key* is currently inside its cooldown window (pruning if expired)."""
        until = self._armed_until.get(key)
        if until is None:
            return False
        if now >= until:
            del self._armed_until[key]
            return False
        return True

    def armed_keys(self, *, now: int) -> set[K]:
        """Keys still inside their cooldown window, dropping any that have expired."""
        expired = [key for key, until in self._armed_until.items() if now >= until]
        for key in expired:
            del self._armed_until[key]
        return set(self._armed_until)

    def streak(self, key: K) -> int:
        """The current (pre-trip) failure streak for *key*."""
        return self._streaks.get(key, 0)

    def armed_until(self, key: K) -> int | None:
        """The clock value at which *key*'s cooldown expires, or ``None`` if it
        is not armed. Does not prune — a returned value <= a later ``now`` means
        the window has elapsed (see :meth:`is_armed`)."""
        return self._armed_until.get(key)

    def clear(self, key: K) -> None:
        """Forget *key* entirely — drop its streak and any armed cooldown.

        Used both on a success (the streak should not carry) and on an external
        re-arm signal that retires the cooldown early.
        """
        self._streaks.pop(key, None)
        self._armed_until.pop(key, None)

    def tracked_keys(self) -> set[K]:
        """Every key with a live streak or an armed cooldown (for bulk purges)."""
        return set(self._streaks) | set(self._armed_until)
