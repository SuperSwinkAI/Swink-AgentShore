"""RLStateBar widget — one-line summary of RL engine metrics."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.reactive import reactive
from textual.widget import Widget

from agentshore.ui.play_labels import play_label

if TYPE_CHECKING:
    from agentshore.state import OrchestratorState


class RLStateBar(Widget):
    """Compact single-row display of play count, total cost, and failure streak."""

    DEFAULT_CSS = "RLStateBar { height: 4; }"

    state: reactive[OrchestratorState | None] = reactive(None, layout=True)

    def on_mount(self) -> None:
        self.border_title = "Session"

    def render(self) -> str:
        if self.state is None:
            return "  RL: --\n  Waiting for session snapshot"
        s = self.state
        stats = s.stats
        success_rate = stats.success_rate if stats is not None else 0.0
        successful = stats.successful_plays if stats is not None else 0
        failed = stats.failed_plays if stats is not None else 0
        eligible = sum(1 for allowed in s.action_mask if allowed)
        masked = len(s.action_mask) - eligible if s.action_mask else 0
        last = s.last_play_type.value if s.last_play_type is not None else "none"
        drain = f"  drain={s.drain_reason}" if s.drain_reason else ""
        loop_level = loop_level_for_streak(s.same_type_failure_streak)
        loop_line = ""
        if loop_level == 1 and s.last_play_type is not None:
            play_name = play_label(s.last_play_type)
            loop_line = f"\n  ⚠ Loop: {play_name} failed {s.same_type_failure_streak}x"
        elif loop_level == 2 and s.last_play_type is not None:
            play_name = play_label(s.last_play_type)
            loop_line = f"\n  ⚠ Loop: {play_name} blocked ({s.same_type_failure_streak}x fail)"
        return (
            f"  state={s.session_state.value}  policy={s.policy_mode.value}  "
            f"plays={s.total_plays}  "
            f"ok={successful} fail={failed} success={success_rate:.0%}  "
            f"cost=${s.total_cost:.2f}\n"
            f"  fail_streak={s.same_type_failure_streak}  same={s.same_type_streak}  "
            f"last={last}  eligible={eligible} masked={masked}{drain}{loop_line}"
        )

    def update_state(self, state: OrchestratorState | None) -> None:
        """Replace the displayed state snapshot."""
        self.remove_class("loop--warning", "loop--force")
        if state is not None:
            loop_level = loop_level_for_streak(state.same_type_failure_streak)
            if loop_level == 1:
                self.add_class("loop--warning")
            elif loop_level == 2:
                self.add_class("loop--force")
        self.state = state


def loop_level_for_streak(streak: int) -> int:
    """Map failure streak to escalation level: 0 (none), 1 (warn), 2 (force), 3 (escalation)."""
    if streak >= 7:
        return 3
    if streak >= 5:
        return 2
    if streak >= 3:
        return 1
    return 0
