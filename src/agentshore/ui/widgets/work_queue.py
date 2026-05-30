"""WorkQueueSummary widget — compact issue and PR queue health summary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.reactive import reactive
from textual.widget import Widget

if TYPE_CHECKING:
    from agentshore.state import IssueSnapshot, OrchestratorState


class WorkQueueSummary(Widget):
    """Summarizes issue and pull-request backlog state from the latest snapshot."""

    state: reactive[OrchestratorState | None] = reactive(None, layout=True)

    def on_mount(self) -> None:
        self.border_title = "Work Queue"

    def render(self) -> str:
        if self.state is None:
            return "  Queue: waiting for state"

        issues = [issue for issue in self.state.open_issues if _is_open(issue.state)]
        ready = sum(1 for issue in issues if issue.bead_ready)
        in_progress = sum(1 for issue in issues if _is_in_progress(issue.bead_status))
        prs = [pr for pr in self.state.pull_requests if _is_open(pr.state)]
        blocked = sum(1 for pr in prs if pr.blocked)
        draft = sum(1 for pr in prs if pr.is_draft)
        pending_review = len(self.state.pending_review_queue)

        lines = [
            f"  Issues {len(issues):>2} open   {ready:>2} ready   {in_progress:>2} in progress",
            f"  PRs    {len(prs):>2} open   {blocked:>2} blocked {draft:>2} draft",
            f"  Reviews queued {pending_review}",
        ]
        next_issue = _next_issue(issues)
        if next_issue is not None:
            lines.append(f"  Next #{next_issue.issue_number}: {_truncate(next_issue.title, 42)}")
        return "\n".join(lines)

    def update_state(self, state: OrchestratorState | None) -> None:
        """Replace the state snapshot driving the queue summary."""
        self.state = state


def _is_open(state: str) -> bool:
    return state.lower() == "open"


def _is_in_progress(status: str | None) -> bool:
    if status is None:
        return False
    normalized = status.lower().replace("-", "_").replace(" ", "_")
    return normalized in {"in_progress", "doing", "started", "active"}


def _next_issue(issues: list[IssueSnapshot]) -> IssueSnapshot | None:
    if not issues:
        return None
    return min(
        issues,
        key=lambda issue: (
            issue.priority if issue.priority is not None else 999,
            issue.issue_number,
        ),
    )


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"
