"""Shared post-merge reconciliation for plays that land a PR.

When a play merges a PR — ``merge_pr`` directly, or ``unblock_pr`` merging a
stacked/blocking sibling in place — the local SQLite cache and beads graph must
be propagated forward: mark the PR ``MERGED``, complete its review-queue rows,
fast-forward the target branch, and close the PR's linked issues and their beads
tasks.

``merge_pr.execute()`` keeps its own inline copy of this block because its tests
patch ``merge_pr._fetch_pr_body`` / ``merge_pr.bd`` by module path; moving those
call sites would break them. This module is the reusable entry point for other
plays and intentionally reuses ``merge_pr``'s gh-reading helpers. Keep the logic
here in sync with ``merge_pr.execute()`` (post-merge block, ~lines 209-325).
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import aiosqlite
import structlog

from agentshore.beads import BeadStatus, bd
from agentshore.core.branch_sync import fast_forward_local_branch
from agentshore.plays.skill_backed.merge_pr import (
    _fetch_pr_body,
    _fetch_pr_links,
    _validated_issue_set,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentshore.plays.base import PlayExecutionContext
    from agentshore.state import OrchestratorState

_logger = structlog.get_logger(__name__)


async def reconcile_merged_pr(
    pr_number: int,
    *,
    ctx: PlayExecutionContext,
    state: OrchestratorState,
    skill_issues: Sequence[int] = (),
) -> list[int]:
    """Propagate a just-merged PR into the local cache and beads graph.

    Mirrors the post-merge block of ``merge_pr.execute()``: marks the PR merged,
    completes its reviews, fast-forwards the configured target branch, resolves
    the PR's linked issues (comprehensive link inference first, body-keyword
    validation as fallback), closes them in the cache, and closes the linked
    beads tasks. Returns the resolved ``issues_closed`` list so callers can emit
    it as an artifact if they want one (``unblock_pr`` deliberately does not, to
    avoid double-counting issue throughput against ``merge_pr``).

    Issue/bead close-out is best-effort (guarded); ``mark_pr_merged`` and
    ``complete_reviews_for_pr`` surface their errors as in ``merge_pr``.
    """
    await ctx.store.mark_pr_merged(pr_number, ctx.session_id)
    await ctx.store.complete_reviews_for_pr(ctx.session_id, pr_number)

    target_branch = ctx.cfg.project.target_branch
    if target_branch:
        await fast_forward_local_branch(ctx.project_path, target_branch)

    pr_link_numbers = await _fetch_pr_links(pr_number, ctx.project_path)
    if pr_link_numbers:
        issues_closed = sorted(set(skill_issues) | set(pr_link_numbers))
    else:
        pr_body = await _fetch_pr_body(pr_number, ctx.project_path)
        issues_closed = _validated_issue_set(
            skill_issues=list(skill_issues),
            pr_body=pr_body,
            pr_number=pr_number,
        )

    if issues_closed:
        try:
            await ctx.store.update_issues_state_batch(issues_closed, ctx.session_id, "closed")
        except (aiosqlite.Error, sqlite3.Error, RuntimeError) as exc:
            _logger.warning(
                "reconcile_merged_pr_close_issues_failed",
                session_id=ctx.session_id,
                pr_number=pr_number,
                issue_numbers=sorted(issues_closed),
                error=str(exc),
            )

    for issue_number in issues_closed:
        if state.graph is None:
            break
        for task in state.graph.tasks:
            if task.issue_number == issue_number and task.status != BeadStatus.CLOSED:
                try:
                    await bd(
                        "update",
                        task.bead_id,
                        "--status",
                        "closed",
                        "--dolt-auto-commit=on",
                        cwd=ctx.project_path,
                    )
                    _logger.info(
                        "reconcile_merged_pr_bead_closed",
                        bead_id=task.bead_id,
                        issue_number=issue_number,
                        pr_number=pr_number,
                        session_id=ctx.session_id,
                    )
                except Exception as exc:
                    _logger.warning(
                        "reconcile_merged_pr_bead_close_failed",
                        bead_id=task.bead_id,
                        issue_number=issue_number,
                        pr_number=pr_number,
                        error=str(exc),
                        session_id=ctx.session_id,
                    )
                break

    return issues_closed
