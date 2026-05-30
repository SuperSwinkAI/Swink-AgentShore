"""AlignmentBars widget — shows beads epic closure as text progress bars."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.widget import Widget

from agentshore.ui.alignment_levels import (
    ALIGNMENT_HIGH_THRESHOLD,
    ALIGNMENT_MEDIUM_THRESHOLD,
    alignment_level,
)

if TYPE_CHECKING:
    from agentshore.beads import ProjectGraph


class AlignmentBars(Widget):
    """Renders per-epic alignment bars and reflects average level via CSS class."""

    _graph: ProjectGraph | None = None

    def on_mount(self) -> None:
        self.border_title = "Epic Closure"

    def render(self) -> str:
        if self._graph is None:
            return "  No beads graph\n  Run Seed Project to initialise"
        if not self._graph.epics:
            return "  No epics\n  Seed or groom the project graph"
        lines = [
            self._render_line("Global closure", self._graph.global_closure_ratio),
            f"  Tasks ready {self._graph.tasks_ready}/{self._graph.tasks_total}",
        ]
        ranked_epics = sorted(self._graph.epics, key=lambda epic: epic.closure_ratio, reverse=True)
        for epic in ranked_epics[:3]:
            label = epic.title[:20]
            progress = f"{epic.closed_tasks}/{epic.total_tasks}"
            lines.append(self._render_line(label, epic.closure_ratio, progress))
        if len(ranked_epics) > 3:
            lines.append(f"  +{len(ranked_epics) - 3} more epics")
        return "\n".join(lines)

    @staticmethod
    def _render_line(label: str, ratio: float, suffix: str | None = None) -> str:
        width = 12
        filled = max(0, min(width, round(ratio * width)))
        bar = "█" * filled + "░" * (width - filled)
        metric = suffix if suffix is not None else f"{ratio:.0%}"
        return f"  {label:<20} {bar} {metric:>6} [{alignment_level(ratio)}]"

    def update_clusters(self, graph: ProjectGraph | None) -> None:
        """Replace displayed graph and update the CSS level class."""
        self._graph = graph
        self.refresh()
        self.remove_class("bar--low", "bar--med", "bar--high")
        if graph is None:
            return
        ratio = graph.global_closure_ratio
        self.add_class(
            "bar--high"
            if ratio >= ALIGNMENT_HIGH_THRESHOLD
            else "bar--med"
            if ratio >= ALIGNMENT_MEDIUM_THRESHOLD
            else "bar--low"
        )
