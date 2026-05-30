"""IssueWorkQueueScreen -- grouped issue and pull-request work queue."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from agentshore.github.pr_links import issue_numbers_for_pr

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from agentshore.state import IssueSnapshot, OrchestratorState, PullRequestSnapshot


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

    prs_by_issue: dict[int, PullRequestSnapshot] = {}
    for pr in state.pull_requests:
        for issue_number in issue_numbers_for_pr(pr):
            existing = prs_by_issue.get(issue_number)
            if existing is None or (existing.state != "open" and pr.state == "open"):
                prs_by_issue[issue_number] = pr

    pending_review_prs = {item.pr_number for item in state.pending_review_queue}
    reviewing_issues = {
        issue_number
        for pr in state.pull_requests
        if pr.state == "open" or pr.pr_number in pending_review_prs
        for issue_number in issue_numbers_for_pr(pr)
    }
    in_progress_issues = {
        agent.current_play_issue_number
        for agent in state.agents
        if agent.current_play_issue_number is not None and agent.current_play_type is not None
    }

    groups: dict[str, list[str]] = {
        "TO DO": [],
        "IN PROGRESS": [],
        "IN REVIEW": [],
        "DONE": [],
    }
    for issue in state.open_issues:
        issue_pr = prs_by_issue.get(issue.issue_number)
        if issue.state.lower() == "closed":
            group = "DONE"
        elif issue.issue_number in in_progress_issues:
            group = "IN PROGRESS"
        elif issue.issue_number in reviewing_issues:
            group = "IN REVIEW"
        else:
            group = "TO DO"
        groups[group].append(_issue_line(issue, issue_pr))

    known_issue_numbers = {issue.issue_number for issue in state.open_issues}
    for pr in state.pull_requests:
        if pr.state != "open":
            continue
        if known_issue_numbers.intersection(issue_numbers_for_pr(pr)):
            continue
        groups["IN REVIEW"].append(_pr_line(pr, queued=pr.pr_number in pending_review_prs))

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


def _issue_line(issue: IssueSnapshot, pr: PullRequestSnapshot | None) -> str:
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
    if pr is not None:
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
