"""MergePRPlay — run agentshore-merge-pr to merge an approved pull request."""

from __future__ import annotations

import dataclasses
import json
import re
from typing import TYPE_CHECKING

import structlog

from agentshore.beads import bd
from agentshore.command import CommandTimeoutError, run_command
from agentshore.core.branch_sync import fast_forward_local_branch
from agentshore.github.pr_links import infer_pr_issue_links
from agentshore.plays.scope import _PR_BODY_ISSUE_RE
from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.plays.skill_backed.gates import CapabilityGate
from agentshore.state import PlayOutcome, PlayType

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.plays.base import PlayExecutionContext, PlayParams
    from agentshore.state import OrchestratorState

_logger = structlog.get_logger(__name__)

# Bare "#N" reference, for the second pass over keyword lines.
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

    # Two-pass extraction captures comma-separated lists ("Closes #1, #2, #3"):
    # pass 1 finds lines with a closing keyword, pass 2 pulls all #N from them.
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

    # body_issues is ground truth; hallucinated entries are dropped.
    return sorted(body_issues)


class MergePRPlay(SkillBackedPlay):
    """Merge an approved PR through the standard GitHub merge gates.

    Candidate validity ("is there a PR approved (GitHub or AgentShore-internal)
    at the current head_sha, mergeable, and targeting the configured base?")
    lives in ``EligibilityAuthority._VALIDITY_FNS`` for ``MERGE_PR`` and is
    appended by the base ``preconditions`` adapter. This play only declares the
    capability gate.
    """

    gates = (CapabilityGate("can_merge"),)

    # PR-scoped: self-heal the PR base before merging so it lands on the target.
    retarget_pr_base = True

    @property
    def play_type(self) -> PlayType:
        return PlayType.MERGE_PR

    @property
    def skill_name(self) -> str:
        return "agentshore-merge-pr"

    @property
    def capability(self) -> str | None:
        return "can_merge"

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
            # Shared reconciler propagates the merge (also used by unblock_pr for
            # stacked siblings). Pass this module's own helpers so tests patching
            # ``merge_pr.*`` by module path still reach the call sites. Imported
            # lazily to avoid an import cycle (``_merge_reconcile`` imports them).
            from agentshore.plays.skill_backed._merge_reconcile import reconcile_merged_pr

            raw_issues_closed = (
                self._last_skill_result.issues_closed if self._last_skill_result is not None else []
            )
            issues_closed = await reconcile_merged_pr(
                params.pr_number,
                ctx=ctx,
                state=state,
                skill_issues=raw_issues_closed,
                bd_fn=bd,
                fetch_pr_body=_fetch_pr_body,
                fetch_pr_links=_fetch_pr_links,
                fast_forward=fast_forward_local_branch,
            )

            # Artifact lets MetricsEngine distinguish linked merges from
            # doc-only/hotfix ones (see _play_closed_issue in rl/metrics.py). Empty
            # list is meaningful: "closed no issues", not "no data". The
            # ``issues_closed`` key feeds run_qa, groom_backlog, dashboard DONE.
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
        return outcome
