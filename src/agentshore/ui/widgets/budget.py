"""BudgetWidget — displays current budget state with a fill bar and CSS health class."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.reactive import reactive
from textual.widget import Widget

if TYPE_CHECKING:
    from agentshore.state import BudgetSnapshot, TrajectorySnapshot


class BudgetWidget(Widget):
    """Shows spent/remaining budget with a 20-cell progress bar."""

    budget: reactive[BudgetSnapshot | None] = reactive(None, layout=True)
    trajectory: reactive[TrajectorySnapshot | None] = reactive(None, layout=True)

    def on_mount(self) -> None:
        self.border_title = "Budget"

    def render(self) -> str:
        if self.budget is None:
            return "  Budget: N/A\n  Waiting for session snapshot"
        if not self.budget.enabled:
            lines = [f"  ${self.budget.spent:.2f} spent  Budget: unlimited"]
            return "\n".join(lines + self._trajectory_lines())
        pct = self.budget.remaining / max(self.budget.total_budget, 0.01)
        pct = max(0.0, min(1.0, pct))
        filled = round(pct * 20)
        bar = "█" * filled + "░" * (20 - filled)
        lines = [
            f"  Spent ${self.budget.spent:.2f} / ${self.budget.total_budget:.2f}",
            f"  {bar}  {pct:.0%} remaining (${self.budget.remaining:.2f})",
            f"  avg/play ${self.budget.estimated_cost_per_play:.2f}",
        ]
        return "\n".join(lines + self._trajectory_lines())

    def watch_budget(self, budget: BudgetSnapshot | None) -> None:
        """React to budget changes by updating the CSS health class."""
        self.remove_class("budget--healthy", "budget--warning", "budget--exhausted")
        if budget is None:
            return
        if not budget.enabled:
            self.add_class("budget--healthy")
            return
        pct = budget.remaining / max(budget.total_budget, 0.01)
        if pct > 0.5:
            self.add_class("budget--healthy")
        elif pct > 0.2:
            self.add_class("budget--warning")
        else:
            self.add_class("budget--exhausted")

    def update_budget(
        self,
        budget: BudgetSnapshot | None,
        trajectory: TrajectorySnapshot | None = None,
    ) -> None:
        """Convenience method to set a new budget snapshot."""
        self.budget = budget
        self.trajectory = trajectory

    def _trajectory_lines(self) -> list[str]:
        if self.trajectory is None:
            return []
        return [
            "  trajectory "
            f"{self.trajectory.estimated_remaining_plays} plays / "
            f"${self.trajectory.estimated_remaining_cost:.2f} est",
            f"  projected alignment {self.trajectory.projected_alignment_at_budget_end:.0%}",
        ]
