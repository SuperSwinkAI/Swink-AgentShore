---
name: agentshore-unblock-pr
description: "Action slot 1 — Unblock PR. Resolves every blocker keeping an open PR from merging: merge conflicts (rebase), requested changes, failed CI checks, and AgentShore block labels. After this play succeeds the PR should be ready to merge once CI passes."
argument-hint: [pr_number]
disable-model-invocation: true
allowed-tools: Read, Edit, Write, Bash(gh:*, git:*, npm:*, python3:*, pytest:*, cargo:*, make:*, uv:*, bun:*, pnpm:*, yarn:*)
---

# agentshore-unblock-pr

Resolve every blocker on PR #$ARGUMENTS so it is ready to merge once CI passes.

**Project docs are authoritative.** Read `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, and `.github/PULL_REQUEST_TEMPLATE*` for fix conventions, CI ownership, and required validation; default to the rules below when silent. Apply concrete requirements; ignore vague advice.

**Select.** If `$ARGUMENTS` is empty, run `gh pr list --state open --limit 50 --json number,title,headRefName,labels,reviewDecision,statusCheckRollup,isDraft,mergeable` and pick the oldest non-draft PR with any blocker: `mergeable == CONFLICTING`, `reviewDecision == CHANGES_REQUESTED`, a failed/errored/timed-out/cancelled/action-required check, or labels `blocked`, `agentshore/blocked`, `needs-work`, `changes-requested`, `do-not-merge`. None → `success: false`, `error: "no blocked PR available"`, stop.

**Read feedback.** `gh pr view $PR --json number,title,body,baseRefName,headRefName,headRefOid,labels,reviewDecision,statusCheckRollup,reviews,comments,files,mergeable`, then `gh api repos/{owner}/{repo}/pulls/$PR/comments --paginate` (derive owner/repo from `gh repo view --json owner,name`). Group blockers into a checklist: source (merge_conflict / reviewer / AgentShore review / CI), file+line, exact requested change, verification evidence.

**Stale-review short-circuit.** If the newest `CHANGES_REQUESTED` review's `commit_id` is older than current `headRefOid` AND there are no other blockers: do not modify the branch. Dismiss the stale review via `gh api -X PUT repos/{owner}/{repo}/pulls/$PR/reviews/<review_id>/dismissals -f message="Stale: addressed in <headRefOid>" -f event="DISMISS"` (continue on permission/already-dismissed errors — AgentShore's PASS at the current head still lets `merge_pr` proceed). Emit success with artifact `{"type": "stale_review_state", ...}` and requested mutation `{"type": "request_play", "play": "merge_pr", "pr": $PR}`. Stop. Other blockers alongside a stale review → address those first; this short-circuit applies only when CHANGES_REQUESTED is the sole obstacle.

**Resolve conflicts (if `mergeable == CONFLICTING`).** `git fetch origin $BASE_BRANCH && git rebase origin/$BASE_BRANCH`. Clean → `git push --force-with-lease`, re-fetch PR status, continue. On conflict: `git rebase --abort`, then check linked issues — `gh pr view $PR --json closingIssuesReferences --jq '.closingIssuesReferences[].number'` and `gh issue view <n> --json state --jq '.state'`. Any CLOSED → PR is superseded: `gh pr close $PR --comment "Closing: the linked issue was already resolved by a competing PR..."`, emit `success: true` with `artifacts: [{"type": "pr_closed", "pr": $PR, "reason": "superseded"}]` and `error: "PR closed — superseded: linked issue already closed"`. Modify/delete conflicts (one side deleted what the other modifies) → close the PR with a "files were deleted on base during consolidation" comment, emit `success: false`, `blocked_by: "merge_conflicts"`. Content conflicts where intent is clear → resolve in cwd (keep both for additive, merge for compatible semantic), `git add`, `git rebase --continue`, force-push. Irreconcilable → `gh pr edit $PR --add-label "needs-rebase"` (ignore errors), emit `success: false`, `blocked_by: "merge_conflicts"`, `error: "Merge conflicts require manual resolution"`.

**Fix only the blockers.** Read files in full before editing. Smallest complete fix per blocker — no unrelated refactors, new features, or opportunistic cleanup. Unclear/wrong feedback → leave a PR comment naming the specific ambiguity and exit `success: false`. For CI failures, reproduce locally and fix the root cause. If you conclude the failure is **external** (billing block, runner outage, infrastructure), you MUST quote the literal API/annotation text — run `gh api repos/{owner}/{repo}/check-runs/{check_id}/annotations` or `gh run view <run_id> --log-failed` and copy the exact string into both the PR comment and result `error`. Never paraphrase an external cause without literal evidence.

**Validate, push, update.** Run the narrowest commands that prove each blocker is fixed (broader validation when the touched files have wider blast radius); record exact command, exit code, and outcome in `verification_evidence`. Re-check `git status --porcelain`, stage only intentional changes, `git commit -m "fix: unblock PR #$PR"`, `git push origin HEAD:$HEAD_BRANCH`. Post a PR comment summarising addressed blockers and validation evidence (quoting any literal CI annotation). Remove `blocked`, `agentshore/blocked`, `needs-work`, `changes-requested` labels when their conditions are met; never remove `do-not-merge` or `needs-human-review`. Dismiss the prior `CHANGES_REQUESTED` review via the same `dismissals` API call as the short-circuit (continue on failure). A successful unblock counts as AgentShore's code review for the PR — request `merge_pr` next.

**Forbidden:** touching `.github/workflows/**`, `.github/actions/**`, `.gitlab-ci.yml`, `.circleci/**`, `azure-pipelines.yml`, `Jenkinsfile`, `bitbucket-pipelines.yml`, or tests that assert their existence — if CI changes are required, comment that CI config is owned by the human maintainer and exit `success: false`, `error: "ci-change requested but forbidden by skill policy"`. Never `git worktree add/remove/prune` (AgentShore owns lifecycle — you are already in the PR's worktree).

**Report — one fenced JSON block, nothing else:**

```json
{
  "success": true,
  "artifacts": [{"type": "pr_unblock_attempt", "number": 42, "branch": "agentshore/17-add-widget", "head_sha": "abc123"}],
  "issues_created": [],
  "requested_mutations": [{"type": "request_play", "play": "merge_pr", "pr": 42}],
  "blocked_by": [],
  "addressed_items": ["rebased onto main (was CONFLICTING)", "reviewer requested null handling in src/widget.py"],
  "verification_evidence": [{"command": "pytest tests/test_widget.py -v", "exit_code": 0, "summary": "12 passed"}],
  "error": null
}
```

On skip/block, `success: false` with populated `error` and `blocked_by` (e.g. `"merge_conflicts"`, `"stale_changes_requested_review"`). Never omit the result block.
