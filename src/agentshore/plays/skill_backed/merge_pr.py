"""MergePRPlay — run agentshore-merge-pr to merge an approved pull request."""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from typing import TYPE_CHECKING

import aiosqlite
import structlog

from agentshore.beads import BeadStatus, bd
from agentshore.command import CommandTimeoutError, run_command
from agentshore.github.pr_links import infer_pr_issue_links
from agentshore.plays.candidates import pr_merge_ready
from agentshore.plays.scope import _PR_BODY_ISSUE_RE
from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.plays.skill_backed.gates import CapabilityGate
from agentshore.rl.mask_reason import MaskClassification, MaskReason, MaskSource
from agentshore.state import PlayOutcome, PlayType

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.plays.base import PlayExecutionContext, PlayParams
    from agentshore.state import OrchestratorState

_logger = structlog.get_logger(__name__)

# Matches any bare "#N" reference (used in the second pass over keyword lines).
_BARE_ISSUE_RE = re.compile(r"#(\d+)")


async def _fetch_pr_body(pr_number: int, project_path: Path) -> str | None:
    """Fetch a PR body via ``gh pr view``.

    Returns the body string, or None if the fetch fails (network error,
    gh not available, etc.).  Failures are non-fatal — callers fall back to
    trusting the skill result.
    """
    try:
        result = await run_command(
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--json",
            "body",
            "--jq",
            ".body",
            cwd=project_path,
            timeout_seconds=30,
        )
    except (OSError, CommandTimeoutError) as exc:
        _logger.warning("merge_pr_body_fetch_failed", pr_number=pr_number, error=str(exc))
        return None
    if result.returncode == 0:
        return result.stdout.strip()
    _logger.warning(
        "merge_pr_body_fetch_failed",
        pr_number=pr_number,
        returncode=result.returncode,
        stderr=result.stderr[:500],
    )
    return None


async def _fetch_pr_links(pr_number: int, project_path: Path) -> tuple[int, ...]:
    """Fetch all issue numbers linked to a PR using comprehensive link inference.

    Queries ``gh`` for the PR body, head branch name, and GitHub's
    ``closingIssuesReferences`` (populated even when the PR targets a
    non-default branch), then delegates to ``infer_pr_issue_links`` which
    understands closing keywords, bare #N refs on keyword lines, AgentShore
    branch prefixes, and GitHub API closing references.

    Returns an empty tuple on any fetch failure — callers fall back to the
    skill-reported ``issues_closed`` list.
    """
    try:
        result = await run_command(
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--json",
            "body,headRefName,closingIssuesReferences",
            cwd=project_path,
            timeout_seconds=30,
        )
    except (OSError, CommandTimeoutError) as exc:
        _logger.warning("merge_pr_links_fetch_failed", pr_number=pr_number, error=str(exc))
        return ()
    if result.returncode != 0:
        _logger.warning(
            "merge_pr_links_fetch_failed",
            pr_number=pr_number,
            returncode=result.returncode,
            stderr=result.stderr[:500],
        )
        return ()
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        _logger.warning("merge_pr_links_parse_failed", pr_number=pr_number, error=str(exc))
        return ()
    links = infer_pr_issue_links(
        closing_issue_references=data.get("closingIssuesReferences"),
        body=data.get("body"),
        branch=data.get("headRefName"),
    )
    return links.issue_numbers


def _validated_issue_set(
    *,
    skill_issues: list[int],
    pr_body: str | None,
    pr_number: int,
) -> list[int]:
    """Cross-check *skill_issues* against *pr_body* using ``_PR_BODY_ISSUE_RE``.

    H4: if an issue appears in skill_issues but NOT in the PR body, it is
    likely hallucinated — emit a warning and exclude it.  If an issue appears
    in the PR body but is MISSING from skill_issues, emit a warning and add it.

    When *pr_body* is None (fetch failed), return *skill_issues* unchanged —
    best-effort, don't break the merge path over a gh availability blip.
    """
    if pr_body is None:
        return list(skill_issues)

    # Two-pass extraction so that comma-separated lists like
    # "Closes #123, #456" or "Fixes #1, #2, and #3" are fully captured.
    # Pass 1: find every line that contains a closing keyword.
    # Pass 2: extract ALL #N references from those lines.
    body_issues: set[int] = set()
    for line in pr_body.splitlines():
        if _PR_BODY_ISSUE_RE.search(line):
            for m in _BARE_ISSUE_RE.finditer(line):
                body_issues.add(int(m.group(1)))
    skill_set = set(skill_issues)

    hallucinated = skill_set - body_issues
    for n in sorted(hallucinated):
        _logger.warning(
            "merge_pr_hallucinated_issue",
            pr_number=pr_number,
            issue_number=n,
            reason="issue in skill result but not in PR body — skipping",
        )

    missed = body_issues - skill_set
    for n in sorted(missed):
        _logger.warning(
            "merge_pr_missing_issue",
            pr_number=pr_number,
            issue_number=n,
            reason="issue in PR body but missing from skill result — adding",
        )

    # Final set: body_issues is the ground truth; hallucinated entries are dropped.
    return sorted(body_issues)


class MergePRPlay(SkillBackedPlay):
    """Merge an approved PR through the standard GitHub merge gates."""

    gates = (CapabilityGate("can_merge"),)

    @property
    def play_type(self) -> PlayType:
        return PlayType.MERGE_PR

    @property
    def skill_name(self) -> str:
        return "agentshore-merge-pr"

    @property
    def capability(self) -> str | None:
        return "can_merge"

    def preconditions(self, state: OrchestratorState) -> list[MaskReason]:
        reasons = super().preconditions(state)
        if reasons:
            return reasons
        # The PR must be (a) approved AND (b) confirmed mergeable by GitHub.
        # Two approval sources are accepted:
        #   - GitHub-side: review_decision='APPROVED'
        #   - AgentShore-internal: last_review_status='PASS' AND last_reviewed_sha == head_sha
        # Mergeable=None or UNKNOWN means a refresh is pending — treat as not ready.
        # The resolver uses the same filter so precondition and resolver cannot disagree.
        mergeable_prs = [
            pr
            for pr in state.pull_requests
            if pr_merge_ready(pr, target_branch=state.target_branch)
        ]
        if not mergeable_prs:
            # Distinguish "base != target" from the generic not-ready case so the
            # operator can see a wrong-base PR is being deterministically held
            # back (it will re-qualify once create-side auto-correction retargets
            # it to the configured target branch).
            wrong_base = [
                pr
                for pr in state.pull_requests
                if state.target_branch
                and isinstance(getattr(pr, "base_ref", None), str)
                and pr.base_ref
                and pr.base_ref != state.target_branch
            ]
            if wrong_base:
                nums = ", ".join(f"#{pr.pr_number}" for pr in wrong_base)
                return [
                    MaskReason(
                        text=(
                            f"PR(s) {nums} target a base other than '{state.target_branch}' "
                            "— held until base is corrected"
                        ),
                        classification=MaskClassification.HARD,
                        source=MaskSource.CANDIDATE,
                    )
                ]
            return [
                MaskReason(
                    text=(
                        "no PR with GitHub or AgentShore approval at current head_sha "
                        "and mergeable=MERGEABLE (awaiting review or CI)"
                    ),
                    classification=MaskClassification.HARD,
                    source=MaskSource.CANDIDATE,
                )
            ]
        return []

    async def execute(
        self,
        state: OrchestratorState,
        params: PlayParams,
        *,
        ctx: PlayExecutionContext,
    ) -> PlayOutcome:
        """Merge the PR, then propagate the merge to the local cache.

        On success, immediately writes ``state='MERGED'`` to the local
        cache (post-merge write-through). Without this, the resolver picks
        the just-merged PR for ``unblock_pr`` until the next
        ``_refresh_issues`` cycle, wasting agent dispatches on a PR
        GitHub already merged.

        Also closes linked beads tasks (C2) and validates the issues_closed list
        against the actual PR body (H4).
        """
        outcome = await super().execute(state, params, ctx=ctx)
        if outcome.success and params.pr_number is not None:
            await ctx.store.mark_pr_merged(params.pr_number, ctx.session_id)
            await ctx.store.complete_reviews_for_pr(ctx.session_id, params.pr_number)
            # Mark referenced issues closed in the local cache. Without this
            # they sit at state='open' forever — _refresh_issues only fetches
            # state="open" from GitHub, so closed issues are never seen by
            # the periodic refresh and the dashboard's DONE column (driven
            # by list_recently_closed_issues, which filters on state='closed')
            # stays empty.
            raw_issues_closed = (
                self._last_skill_result.issues_closed if self._last_skill_result is not None else []
            )

            # Resolve linked issues via comprehensive infer_pr_issue_links first
            # (captures branch-name refs and GitHub API closing references that
            # _validated_issue_set would miss for non-default-branch merges or
            # PRs without explicit closing keywords).
            pr_link_numbers = await _fetch_pr_links(params.pr_number, ctx.project_path)

            if pr_link_numbers:
                # Merge skill-reported and link-inferred sets; prefer the union
                # so that issues detected either way are included.
                issues_closed = sorted(set(raw_issues_closed) | set(pr_link_numbers))
                _logger.debug(
                    "merge_pr_links_resolved",
                    pr_number=params.pr_number,
                    skill_issues=sorted(raw_issues_closed),
                    link_inferred=sorted(pr_link_numbers),
                    merged=issues_closed,
                )
            else:
                # _fetch_pr_links failed; fall back to H4 body-keyword validation.
                pr_body = await _fetch_pr_body(params.pr_number, ctx.project_path)
                issues_closed = _validated_issue_set(
                    skill_issues=raw_issues_closed,
                    pr_body=pr_body,
                    pr_number=params.pr_number,
                )

            # Record the validated issue list as an artifact so MetricsEngine
            # can distinguish linked merges from doc-only/hotfix merges when
            # counting issue throughput. Empty list is meaningful — it tells
            # downstream consumers "this merge closed no issues" rather than
            # "no data". See _play_closed_issue() in src/agentshore/rl/metrics.py.
            # ``issues_closed`` key satisfies the desktop-8otp acceptance criteria
            # (downstream consumers: run_qa, groom_backlog, dashboard DONE column).
            outcome = dataclasses.replace(
                outcome,
                artifacts=[
                    *outcome.artifacts,
                    {
                        "type": "pr_merged_issue_numbers",
                        "pr": params.pr_number,
                        "issue_numbers": list(issues_closed),
                        "issues_closed": list(issues_closed),
                    },
                ],
            )

            # SQLite write-through.
            if issues_closed:
                try:
                    await ctx.store.update_issues_state_batch(
                        issues_closed, ctx.session_id, "closed"
                    )
                except (aiosqlite.Error, sqlite3.Error, RuntimeError) as exc:
                    _logger.warning(
                        "merge_pr_close_issues_failed",
                        session_id=ctx.session_id,
                        pr_number=params.pr_number,
                        issue_numbers=sorted(issues_closed),
                        error=str(exc),
                    )

            for issue_number in issues_closed:
                # C2: Close the linked beads task.
                if state.graph is not None:
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
                                    "merge_pr_bead_closed",
                                    bead_id=task.bead_id,
                                    issue_number=issue_number,
                                    pr_number=params.pr_number,
                                    session_id=ctx.session_id,
                                )
                            except Exception as exc:
                                _logger.warning(
                                    "merge_pr_bead_close_failed",
                                    bead_id=task.bead_id,
                                    issue_number=issue_number,
                                    pr_number=params.pr_number,
                                    error=str(exc),
                                    session_id=ctx.session_id,
                                )
                            break
        return outcome
