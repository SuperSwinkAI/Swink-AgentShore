"""IssueWorkQueueScreen -- grouped issue and pull-request work queue."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from agentshore.state import (
        IssueSnapshot,
        OrchestratorState,
        PullRequestSnapshot,
        WorkQueueItem,
    )


class IssueWorkQueueScreen(ModalScreen[None]):
    """Shows issues and PRs grouped into todo, active, review, and done phases."""

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("i", "dismiss", "Close"),
    ]

    def __init__(self, state: OrchestratorState | None) -> None:
        super().__init__()
        self._state = state

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="issues-container"):
            yield Static(_render_work_queue(self._state), id="issues-work-queue")


def _render_work_queue(state: OrchestratorState | None) -> str:
    if state is None:
        return "Work Queue\n" + "-" * 60 + "\nNo session state available yet."

    view = state.work_queue()
    groups: dict[str, list[str]] = {
        "TO DO": [_issue_line(item) for item in view.todo],
        "IN PROGRESS": [_issue_line(item) for item in view.in_progress],
        "IN REVIEW": [_issue_line(item) for item in view.in_review],
        "DONE": [_issue_line(item) for item in view.done],
    }
    groups["IN REVIEW"].extend(_pr_line(pr, queued=queued) for pr, queued in view.orphan_review_prs)

    lines = [
        "Work Queue",
        "-" * 60,
        f"session={state.session_id} state={state.session_state.value}",
        f"issues={len(state.open_issues)} prs={len(state.pull_requests)} "
        f"pending_reviews={len(state.pending_review_queue)}",
        "",
    ]
    for group_name, items in groups.items():
        lines.append(f"{group_name} ({len(items)})")
        if items:
            lines.extend(items)
        else:
            lines.append("  -")
        lines.append("")
    return "\n".join(lines).rstrip()


def _issue_line(item: WorkQueueItem) -> str:
    issue: IssueSnapshot = item.issue
    bits = [
        f"#{issue.issue_number}",
        issue.title,
        f"[{issue.state}]",
        f"priority={issue.priority if issue.priority is not None else 'none'}",
    ]
    if issue.bead_epic_title:
        bits.append(f"epic={issue.bead_epic_title}")
    if issue.bead_status:
        bits.append(f"bead={issue.bead_status}")
    if issue.bead_ready:
        bits.append("ready")
    if issue.labels:
        bits.append("labels=" + ",".join(issue.labels[:4]))
    if issue.url:
        bits.append(f"url={issue.url}")
    if issue.created_at:
        bits.append(f"created={issue.created_at}")
    if issue.closed_at:
        bits.append(f"closed={issue.closed_at}")
    if item.pr is not None:
        pr = item.pr
        bits.append(f"PR #{pr.pr_number} {pr.status_check_summary or 'checks?'}")
        if pr.blocked:
            bits.append("blocked=" + ",".join(pr.blocked_reasons))
    return "  " + "  ".join(bits)


def _pr_line(pr: PullRequestSnapshot, *, queued: bool) -> str:
    bits = [f"PR #{pr.pr_number}", pr.title, f"[{pr.state}]"]
    if pr.branch:
        bits.append(f"branch={pr.branch}")
    if pr.review_decision:
        bits.append(f"review={pr.review_decision}")
    if pr.status_check_summary:
        bits.append(f"checks={pr.status_check_summary}")
    if pr.blocked:
        bits.append("blocked=" + ",".join(pr.blocked_reasons))
    if queued:
        bits.append("queued_review")
    if pr.url:
        bits.append(f"url={pr.url}")
    return "  " + "  ".join(bits)
