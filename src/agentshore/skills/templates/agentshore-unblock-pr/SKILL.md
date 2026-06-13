---
name: agentshore-unblock-pr
description: "Action slot 1 — Unblock PR. Resolves every blocker keeping an open PR from merging: merge conflicts (rebase), requested changes, failed CI checks, and AgentShore block labels. If the PR is already merge-ready (no real blockers, just a stale review or none), it merges it directly; otherwise it leaves the PR ready to merge once CI passes."
argument-hint: [pr_number]
disable-model-invocation: true
allowed-tools: Read, Edit, Write, Bash(gh:*, git:*, npm:*, python3:*, pytest:*, cargo:*, make:*, uv:*, bun:*, pnpm:*, yarn:*)
---

# agentshore-unblock-pr

Resolve every blocker on PR #$ARGUMENTS so it is ready to merge once CI passes.

**Project docs are authoritative.** Read `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, and `.github/PULL_REQUEST_TEMPLATE*` for fix conventions, CI ownership, and required validation; default to the rules below when silent. Apply concrete requirements; ignore vague advice.

**Select.** If `$ARGUMENTS` is empty, run `gh pr list --state open --limit 50 --json number,title,headRefName,labels,reviewDecision,statusCheckRollup,isDraft,mergeable` and pick the oldest non-draft PR with any blocker: `mergeable == CONFLICTING`, `reviewDecision == CHANGES_REQUESTED`, a failed/errored/timed-out/cancelled/action-required check, or labels `blocked`, `agentshore/blocked`, `needs-work`, `changes-requested`, `do-not-merge`. None → `success: false`, `error: "no blocked PR available"`, stop.

**Read feedback.** `gh pr view $PR --json number,title,body,baseRefName,headRefName,headRefOid,labels,reviewDecision,statusCheckRollup,reviews,comments,files,mergeable`, then `gh api repos/{owner}/{repo}/pulls/$PR/comments --paginate` (derive owner/repo from `gh repo view --json owner,name`). Group blockers into a checklist: source (merge_conflict / reviewer / AgentShore review / CI), file+line, exact requested change, verification evidence.

**Stale-review short-circuit.** If the newest `CHANGES_REQUESTED` review's `commit_id` is older than current `headRefOid` AND there are no other blockers: do not modify the branch. Dismiss the stale review via `gh api -X PUT repos/{owner}/{repo}/pulls/$PR/reviews/<review_id>/dismissals -f message="Stale: addressed in <headRefOid>" -f event="DISMISS"` (continue on permission/already-dismissed errors — AgentShore's PASS at the current head still lets the merge proceed). Then **re-check the target and, if it is now Merge-ready (see below), merge it directly** per the **Merge the target when ready** step; otherwise emit success with artifact `{"type": "stale_review_state", ...}` and stop — the PPO selects the follow-up play from the resulting state. Other blockers alongside a stale review → address those first; this short-circuit applies only when CHANGES_REQUESTED is the sole obstacle.

**Resolve conflicts (if `mergeable == CONFLICTING`).** `git fetch origin $BASE_BRANCH && git rebase origin/$BASE_BRANCH`. Clean → `git push --force-with-lease origin HEAD:$HEAD_BRANCH`, re-fetch PR status, continue. On conflict: `git rebase --abort`, then check linked issues — `gh pr view $PR --json closingIssuesReferences --jq '.closingIssuesReferences[].number'` and `gh issue view <n> --json state --jq '.state'`. Any CLOSED → PR is superseded: `gh pr close $PR --comment "Closing: the linked issue was already resolved by a competing PR..."`, emit `success: true` with `artifacts: [{"type": "pr_closed", "pr": $PR, "reason": "superseded"}]` and `error: "PR closed — superseded: linked issue already closed"`. Modify/delete conflicts (one side deleted what the other modifies) → close the PR with a "files were deleted on base during consolidation" comment, emit `success: false`, `blocked_by: "merge_conflicts"`. Content conflicts where intent is clear → resolve in cwd (keep both for additive, merge for compatible semantic), `git add`, `git rebase --continue`, then `git push --force-with-lease origin HEAD:$HEAD_BRANCH`. Irreconcilable → `gh pr edit $PR --add-label "needs-rebase"` (ignore errors), emit `success: false`, `blocked_by: "merge_conflicts"`, `error: "Merge conflicts require manual resolution"`.

**Blocked by another open PR (stacked / mutual).** Before treating a conflict or red CI as the target's own fault, check whether the real blocker is a *sibling* open PR. Fetch the open set once — `gh pr list --state open --json number,headRefName,baseRefName,reviewDecision,statusCheckRollup,mergeable,labels,isDraft` — and build a `headRefName → number` map. Identify blocker B (first that applies; ties → the one closest to merge-ready, else lowest number): (1) **stack base** — the target's `baseRefName` is another open PR's `headRefName`; (2) **body marker** — the target body has `Depends on #N` / `Stacked on #N` / `Blocked by #N` / `Requires #N` resolving to an open PR; (3) **conflict overlap** — `mergeable == CONFLICTING` and the target's changed files (`gh pr view $PR --json files`) overlap exactly one open sibling's changed files (`gh pr diff <B> --name-only`); (1) and (2) win over (3). Handle **one** blocker level per dispatch — deeper stacks resolve over later dispatches. Merging/unblocking a sibling you authored is allowed (only code review is identity-restricted); add no self-merge guard.

- **B is merge-ready** — GitHub `reviewDecision == APPROVED`, `mergeable == MERGEABLE`, no failed/pending required checks, `baseRefName` equals the configured target branch, not draft, and no `do-not-merge` / `needs-human-review` / `agentshore/manual-required` label — merge it in place (never `--admin`/`--force`), then re-rebase the target onto the updated base:
  ```
  gh pr merge <B> --squash --delete-branch=false      # use the repo's configured merge mode
  git fetch origin $BASE_BRANCH && git rebase origin/$BASE_BRANCH
  git push --force-with-lease origin HEAD:$HEAD_BRANCH
  ```
  Add `{"type": "pr_merged", "pr": <B>, "head_sha": "<B head sha>", "base_ref": "<target>", "reason": "stacked_blocker_merged"}` to `artifacts`, then continue resolving the target's own blockers below.
- **B is NOT merge-ready** (needs review, its own unblocking, or draft) — unblock it in place, then merge it if it becomes ready. Stay in the *current* worktree by switching branches; never run `git worktree add/remove/prune`:
  ```
  git fetch origin <B.head> && git switch -C <B.head> origin/<B.head>   # branch switch only — allowed
  # resolve B's conflicts / fix B's CI root cause using the same rules as the target, then:
  git push --force-with-lease origin HEAD:<B.head>
  # if B is now merge-ready (approved, green, MERGEABLE, correct base): gh pr merge <B> ...  (emit pr_merged for B)
  git switch $HEAD_BRANCH                                               # back to the target; git fetch + rebase onto the updated base
  ```
  The target branch is untouched at this point (you found the dependency before editing it), so the switch is clean. Emit `{"type": "pr_unblock_attempt", "number": <B>, "branch": "<B.head>", "head_sha": "<B head sha>"}` for the work pushed to B (plus `pr_merged` if you merged it).
- **B cannot be finished this dispatch** — B is itself blocked by a further PR, or by a terminal human/CI-infra cause (do not descend further) — emit `{"type": "blocked_by_pr", "target": $PR, "blocker": <B>, "reason": "needs_unblock"}` (or `"needs_review"` / `"draft"`), `success: false`, `blocked_by: ["blocked_by_pr"]`, and stop. Do **not** add `needs-rebase`/`blocked` labels to the target — it is not at fault and must not be parked for the sibling.

If you merged B but the target still has an *independent* blocker (its own conflict or red CI unrelated to B), keep the `pr_merged` artifact and report the target's **own** failure (`blocked_by: ["merge_conflicts"]` / `["ci_not_green"]`) — not `blocked_by_pr`, which is only for "still waiting on a sibling".

**Fix only the blockers.** Read files in full before editing. Smallest complete fix per blocker — no unrelated refactors, new features, or opportunistic cleanup. Unclear/wrong feedback → leave a PR comment naming the specific ambiguity and exit `success: false`. For CI failures, reproduce locally and fix the root cause. If you conclude the failure is **external** (billing block, runner outage, infrastructure), you MUST quote the literal API/annotation text — run `gh api repos/{owner}/{repo}/check-runs/{check_id}/annotations` or `gh run view <run_id> --log-failed` and copy the exact string into both the PR comment and result `error`. Never paraphrase an external cause without literal evidence.

**Validate, push, update.** Run the narrowest commands that prove each blocker is fixed (broader validation when the touched files have wider blast radius); record exact command, exit code, and outcome in `verification_evidence`. Re-check `git status --porcelain`, stage only intentional changes, `git commit -m "fix: unblock PR #$PR"`, `git push origin HEAD:$HEAD_BRANCH`. Post a PR comment summarising addressed blockers and validation evidence (quoting any literal CI annotation). Remove `blocked`, `agentshore/blocked`, `needs-work`, `changes-requested` labels when their conditions are met; never remove `do-not-merge` or `needs-human-review`. Dismiss the prior `CHANGES_REQUESTED` review via the same `dismissals` API call as the short-circuit (continue on failure). A successful unblock counts as AgentShore's code review for the PR.

**Merge the target when ready.** After clearing blockers (or in the stale-review short-circuit), re-read live status — `gh pr view $PR --json mergeable,reviewDecision,statusCheckRollup,baseRefName,isDraft,labels,headRefOid`. If the **target itself** is now Merge-ready — `mergeable == MERGEABLE`, no failed/pending required checks, `baseRefName` equals the configured target branch, not draft, and no `do-not-merge` / `needs-human-review` / `agentshore/manual-required` label (a dismissed/now-empty `reviewDecision` is fine; AgentShore's PASS at the current head is the approval) — **merge it directly**, exactly as a merge-ready sibling above (repo's configured mode, never `--admin`/`--force`):
  ```
  gh pr merge $PR --squash --delete-branch=false      # use the repo's configured merge mode
  ```
  Add `{"type": "pr_merged", "pr": $PR, "head_sha": "<headRefOid>", "base_ref": "<target>", "reason": "target_merge_ready"}` to `artifacts` and emit `success: true`. If the merge command fails (lost a race, branch protection, transient), do **not** treat it as fatal: drop the `pr_merged` artifact, emit the `{"type": "stale_review_state", ...}`/unblock artifact instead, and let the PPO pick `merge_pr` next. Only merge here when fully ready — if any real blocker remains, resolve it (above) and leave merging to a later play.

**Forbidden:** touching `.github/workflows/**`, `.github/actions/**`, `.gitlab-ci.yml`, `.circleci/**`, `azure-pipelines.yml`, `Jenkinsfile`, `bitbucket-pipelines.yml`, or tests that assert their existence — if CI changes are required, comment that CI config is owned by the human maintainer and exit `success: false`, `error: "ci-change requested but forbidden by skill policy"`. Never `git worktree add/remove/prune` (AgentShore owns lifecycle — you are already in the PR's worktree).

**Report — one fenced JSON block, nothing else:**

```json
{
  "success": true,
  "artifacts": [{"type": "pr_unblock_attempt", "number": 42, "branch": "agentshore/17-add-widget", "head_sha": "abc123"}],
  "issues_created": [],
  "requested_mutations": [],
  "blocked_by": [],
  "addressed_items": ["rebased onto main (was CONFLICTING)", "reviewer requested null handling in src/widget.py"],
  "verification_evidence": [{"command": "pytest tests/test_widget.py -v", "exit_code": 0, "summary": "12 passed"}],
  "error": null
}
```

On skip/block, `success: false` with populated `error` and `blocked_by` (e.g. `"merge_conflicts"`, `"stale_changes_requested_review"`, `"blocked_by_pr"` when the target is gated on an unmerged sibling PR). Never omit the result block.
