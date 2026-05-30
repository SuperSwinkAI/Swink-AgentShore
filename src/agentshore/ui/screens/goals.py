"""GoalsScreen -- modal showing beads graph epic closure details.

v0.10.0: uses ProjectGraph for all alignment tracking.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from agentshore.ui.alignment_levels import alignment_level

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from agentshore.beads import ProjectGraph


class GoalsScreen(ModalScreen[None]):
    """Beads graph epic-level closure details."""

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("g", "dismiss", "Close"),
    ]

    def __init__(self, graph: ProjectGraph | None) -> None:
        super().__init__()
        self._graph = graph

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="goals-container"):
            yield Static("Epic Closure", id="goals-title")
            if self._graph is None:
                yield Static("No beads graph detected.")
            else:
                ratio = self._graph.global_closure_ratio
                filled = round(ratio * 20)
                bar = "█" * filled + "░" * (20 - filled)
                yield Static(
                    f"\n-- Global closure --\n"
                    f"  Ratio: {bar} {ratio:.0%} [{alignment_level(ratio)}]\n"
                    f"  Epics: {len(self._graph.epics)}\n"
                    f"  Ready tasks: {self._graph.tasks_ready}/{self._graph.tasks_total}"
                )
                ready_by_epic: dict[str, int] = {}
                for task in self._graph.tasks:
                    if task.ready and task.epic_id is not None:
                        ready_by_epic[task.epic_id] = ready_by_epic.get(task.epic_id, 0) + 1
                for epic in self._graph.epics:
                    yield Static(
                        "\n"
                        f"-- {epic.title} --\n"
                        f"  Closure: {epic.closed_tasks}/{epic.total_tasks} "
                        f"({epic.closure_ratio:.0%})\n"
                        f"  Ready: {ready_by_epic.get(epic.bead_id, 0)}"
                    )
                if not self._graph.epics:
                    yield Static("\nNo epics detected.")
