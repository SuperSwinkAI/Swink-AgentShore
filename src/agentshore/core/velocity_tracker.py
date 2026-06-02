"""Rolling-velocity + executor-skip-divergence tracker.

``VelocityTracker`` owns the orchestrator's short-window completion state that
feeds the RL observation/reward path:

* the rolling-velocity window (issues closed + PRs merged per play window),
* the recent-agent-type window (action diversity for reward shaping),
* the EligibilityAuthority confirm-repick divergence window (observation slot
  177, exposed via ``executor_skip_rate_recent_50``),
* the ``recent_executor_skip`` flag surfaced as a state diagnostic.

It is a thin collaborator (mirroring :class:`agentshore.core.github_syncer.GitHubSyncer`):
constructed in ``phases.py`` and held on the orchestrator as ``_velocity``.
The completion path writes to it, the state/observation path reads from it.
"""

from __future__ import annotations

import collections
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentshore.plays.selector import PlaySelector


class VelocityTracker:
    """Owns the rolling-velocity, agent-type, and confirm-repick windows."""

    def __init__(self, *, velocity_window_size: int) -> None:
        # (play_id, kind) for each velocity-positive completion (pr_merged /
        # issue_closed). Bounded to the configured velocity window.
        self._velocity_events: collections.deque[tuple[int, str]] = collections.deque(
            maxlen=velocity_window_size
        )
        # Watermark play_id marking the window start. Never reassigned in the
        # current implementation (always None ⇒ watermark 1); retained so the
        # velocity math is byte-for-byte the prior behaviour.
        self._velocity_window_start_play_id: int | None = None
        self._recent_agent_types: collections.deque[str] = collections.deque(
            maxlen=velocity_window_size
        )
        # Rolling 50-cycle window of EligibilityAuthority confirm-repick
        # occurrences. Each entry is True iff that selection cycle hit at least
        # one confirm-repick (the authority's live ``confirm`` rejected a
        # snapshot-eligible play → clean re-pick). Fed once per selection cycle
        # by ``record_selection_repicks``. Exposed via
        # ``MetricsEngine.executor_skip_rate_provider`` so PPO's observation
        # vector carries the same rolling divergence signal (slot 177).
        self._executor_skip_window: collections.deque[bool] = collections.deque(maxlen=50)
        # True when the most recent play returned ``skipped_outcome("masked")``
        # from the executor's preconditions safety net. Surfaced via
        # ``state.recent_executor_skip``; cleared by the next non-skipped play.
        self._recent_executor_skip = False

    # ------------------------------------------------------------------
    # Completion-path writes
    # ------------------------------------------------------------------

    def record_velocity_event(self, play_id: int, kind: str) -> None:
        """Record a velocity-positive completion (``pr_merged`` / ``issue_closed``)."""
        self._velocity_events.append((play_id, kind))

    def record_agent_type(self, agent_type: str) -> None:
        """Record the agent type that completed a play (for window diversity)."""
        self._recent_agent_types.append(agent_type)

    def set_recent_executor_skip(self, value: bool) -> None:
        """Set the masked-skip diagnostic flag (True on a masked skip, else False)."""
        self._recent_executor_skip = value

    def record_selection_repicks(self, selector: PlaySelector | None) -> None:
        """Drain the selector's confirm-repick tally into the divergence window.

        Called once per selection cycle, right after ``_select_play`` returns.
        Appends a single bool — True iff the cycle hit at least one
        confirm-repick. Non-PPO selectors have no repick notion and contribute
        nothing, so the window stays empty (rate 0.0) on the FixedPlanSelector
        path.
        """
        consume = getattr(selector, "consume_repick_count", None)
        if consume is None:
            return
        repicks = consume()
        self._executor_skip_window.append(repicks > 0)

    # ------------------------------------------------------------------
    # Observation / state-path reads
    # ------------------------------------------------------------------

    @property
    def recent_executor_skip(self) -> bool:
        return self._recent_executor_skip

    def recent_agent_type_diversity(self) -> int:
        """Distinct agent types in the recent-completion window."""
        return len(set(self._recent_agent_types))

    def compute_rolling_velocity(self, current_play_id: int) -> float:
        """Rolling velocity: (issues_closed + prs_merged) / plays_in_window."""
        if not self._velocity_events:
            return 0.0
        watermark = (
            self._velocity_window_start_play_id
            if self._velocity_window_start_play_id is not None
            else 1
        )
        denom = max(1, current_play_id - watermark + 1)
        return min(1.0, len(self._velocity_events) / denom)

    def executor_skip_rate_recent_50(self) -> float:
        """Fraction of the last 50 selection cycles that hit a confirm-repick.

        Post the eligibility refactor this is the live-drift rate: how often the
        EligibilityAuthority's one live ``confirm`` rejected a snapshot-eligible
        play, forcing a clean re-pick. Empty window returns 0.0 — a fresh session
        hasn't run the selector yet, the same observable signal as "no recent
        divergence." Feeds ``ObservationContext.executor_skip_rate_recent_50`` and
        ultimately observation slot 177 (unchanged slot and [0,1] range).
        """
        if not self._executor_skip_window:
            return 0.0
        return sum(self._executor_skip_window) / len(self._executor_skip_window)
