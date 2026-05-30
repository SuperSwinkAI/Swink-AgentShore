---
name: agentshore-merge-pr
description: "Action slot 6 — Merge PR. Verifies approval, CI, AgentShore review status, and mergeability, then merges. Attempts clean rebase on conflicts. Closes linked issues and cleans up branches."
argument-hint: [pr_number]
disable-model-invocation: true
allowed-tools: Bash(gh:*, git:*)
---

# agentshore-merge-pr

You are a AgentShore skill agent. AgentShore is a pure RL scheduler — not an LLM.
It invoked you with parameters in `$ARGUMENTS`.

## Inputs

- `$ARGUMENTS` — the pull request number to merge (required).

## Step 1 — Pre-flight

1. Read `.agentshore/context.json` if it exists. Note merge strategy preferences (`squash`, `merge`, `rebase`) and any required status checks.
2. Read project-level configuration files (`CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, or similar) for merge conventions.
3. Detect the default branch: `git symbolic-ref refs/remotes/origin/HEAD | sed 's@^refs/remotes/origin/@@'` (fall back to `main`).
4. Fetch latest remote refs: `git fetch origin`.

## Step 2 — Fetch PR status

1. Fetch full PR details:
   ```
   gh pr view $ARGUMENTS --json number,title,body,state,baseRefName,headRefName,headRefOid,mergeable,reviewDecision,statusCheckRollup,labels,author
   ```
2. **If `state` is `MERGED` or `CLOSED`, treat as a no-op success.** The
   work has already shipped (or was abandoned outside AgentShore). Set
   `"success": true`, populate the artifact list with `{"type": "already_merged", "pr": <N>, "state": "<state>"}`,
   and stop. Do NOT return `success: false` — that punishes the policy for
   a benign race where another path (human, agent, external CI) merged the
   PR between selection and dispatch.
3. If `state` is anything else other than `OPEN`, abort with
   `"success": false` and report the state.

## Step 3 — Check AgentShore review gate

1. Labels: if `blocked`, `do-not-merge`, or `needs-human-review` is present → abort with `"blocked_by": "label_<name>"`.
2. Fetch comments via `gh pr view $ARGUMENTS --comments --json comments`; if a `AGENTSHORE_CODE_REVIEW` comment with `status: BLOCK` exists at the current head SHA → abort with `"blocked_by": "agentshore_review_block"`. Missing review at current head SHA is a warning only, not a block.

## Step 4 — Verify approval (GitHub or AgentShore-internal)

A PR is approved if **either** source reports approval. AgentShore accepts internal
approval because in single-user setups GitHub blocks self-approval and
`reviewDecision` never advances past null.

1. **Check AgentShore-internal approval first.** From `.agentshore/context.json`,
   look up this PR in `pull_requests[]`. If the entry has
   `last_review_status == "PASS"` AND `last_reviewed_sha == headRefOid` (the
   current PR head SHA from Step 2), treat the PR as approved and skip to
   Step 5. AgentShore has its own code_review/unblock_pr play history; trust it.
2. Otherwise, check GitHub `reviewDecision`:
   - `APPROVED` — proceed.
   - `CHANGES_REQUESTED` — abort. Report which reviewer requested changes.
   - `REVIEW_REQUIRED` — abort. Report that the PR lacks required approvals.
   - Empty/null — check if the repo requires reviews. If not required, proceed.
3. If aborting, skip to the Result step.

## Step 5 — Verify CI status

1. Inspect `statusCheckRollup` for all required checks.
2. For each check, evaluate the status:
   - All `SUCCESS` or `NEUTRAL` — proceed.
   - Any `PENDING` — wait up to 60 seconds, then re-check once:
     ```
     gh pr view $ARGUMENTS --json statusCheckRollup
     ```
   - Any `FAILURE` or `ERROR` — abort. List the failing checks by name and conclusion.
3. If aborting, skip to the Result step.

## Step 6 — Verify mergeability

1. Check the `mergeable` field:
   - `MERGEABLE` — proceed to Step 7.
   - `UNKNOWN` — re-fetch once after a short pause, then abort if still unknown.
   - `CONFLICTING` — attempt a clean rebase (Step 6a).

### Step 6a — Attempt clean rebase (conflicts only)

Do **not** check out the PR branch in the main worktree — concurrent agents share that working tree. Use an isolated worktree instead.

1. Record the main project directory: `MAIN_REPO=$(pwd)`.
2. Compute an isolated worktree path: `REBASE_WORKTREE="$MAIN_REPO/.agentshore/worktrees/rebase-$ARGUMENTS"`.
3. If `$REBASE_WORKTREE` already exists from a prior run, remove it: `git worktree remove --force "$REBASE_WORKTREE" 2>/dev/null || rm -rf "$REBASE_WORKTREE"`.
4. Read the PR's `headRefName` from Step 2. Delete any stale local copy of the branch from a prior failed run, then fetch and create the worktree:
   ```
   git branch -D <head_ref> 2>/dev/null || true
   git fetch origin <head_ref>:<head_ref>
   git worktree add "$REBASE_WORKTREE" <head_ref>
   ```
5. Inside the worktree, attempt the rebase:
   ```
   cd "$REBASE_WORKTREE"
   git fetch origin <base_branch>
   git rebase origin/<base_branch>
   ```
6. If the rebase succeeds cleanly (no conflicts):
   - Force-push with lease: `git push --force-with-lease`
   - Return to the main project directory: `cd "$MAIN_REPO"`
   - Remove the rebase worktree: `git worktree remove "$REBASE_WORKTREE"`
   - Wait a few seconds for GitHub to re-evaluate mergeability
   - Re-fetch PR status and continue to Step 7
7. If the rebase produces conflicts:
   - Abort the rebase: `git rebase --abort`
   - Return to the main project directory: `cd "$MAIN_REPO"`
   - Remove the rebase worktree: `git worktree remove --force "$REBASE_WORKTREE"`
   - Abort the merge with `"blocked_by": "merge_conflicts"` and `"error": "Merge conflicts require manual resolution"`
   - Add `needs-rebase` label if it exists: `gh pr edit $ARGUMENTS --add-label "needs-rebase"` (ignore errors)

## Step 7 — Merge the PR

1. Determine the merge method from `context.json` or default to `squash`.
2. Merge:
   ```
   gh pr merge $ARGUMENTS --squash --delete-branch
   ```
   Adjust `--squash` to `--merge` or `--rebase` based on project convention.
3. Never force-merge.
4. If merge fails, capture the error message and report it.

## Step 8 — Close linked issues

1. Parse the PR body for issue references: `Closes #N`, `Fixes #N`, `Resolves #N`.
2. For each linked issue, verify it was auto-closed:
   ```
   gh issue view <N> --json state
   ```
3. If the issue is still open, close it manually:
   ```
   gh issue close <N> --comment "Closed by PR #$ARGUMENTS."
   ```
4. Remove the `in-progress` label from each linked issue if present:
   ```
   gh issue edit <N> --remove-label "in-progress"
   ```
   Ignore label errors.

### Step 8b — Detect revert-reopened issues

For each linked issue:

1. Detect net reverts on `main` (after `git fetch origin`):
   ```
   git log origin/main --oneline --grep="Revert.*#$ARGUMENTS"
   git log origin/main --oneline --grep="Reapply.*#$ARGUMENTS"
   ```
2. **If a net revert exists** (revert without a subsequent re-apply):
   - Post a comment: `"PR #$ARGUMENTS was merged but its commit was subsequently reverted (see <revert-sha>). The linked work is no longer on main. This issue has been labelled agentshore/revert-reopened and will not be auto-closed until the fix is re-landed."`
   - Apply `agentshore/revert-reopened` (create if absent: `gh label create "agentshore/revert-reopened" --color "e4e669" --description "Issue reopened because its resolving PR commit was reverted"`); leave `in-progress` on; do not close.
3. **If no net revert**: proceed normally (close, remove `in-progress`).

## Step 9 — Post-merge cleanup

1. Confirm the remote head branch was deleted. If `--delete-branch` did not work:
   ```
   git push origin --delete <head_branch>
   ```
2. Remove any local worktrees that were created for this PR by `agentshore-issue-pickup` or by Step 6a above.
   - Issue-pickup worktrees live at `.agentshore/worktrees/<issue_number>/`. Parse linked issue numbers from the PR body (`Closes #N`, `Fixes #N`, `Resolves #N`) and the head branch name (pattern `agentshore/<N>-*`).
   - Rebase worktrees live at `.agentshore/worktrees/rebase-$ARGUMENTS/`.
   - For each worktree path that exists, remove it:
     ```
     git worktree remove --force <path> 2>/dev/null || rm -rf <path>
     ```
3. Prune stale worktree administrative refs:
   ```
   git worktree prune
   ```
4. Delete the local head branch if it exists (the remote was already deleted by `--delete-branch`):
   ```
   git branch -D <head_branch> 2>/dev/null || true
   ```
5. **Do not** run `git checkout` or `git pull` in the main worktree — other agents may have uncommitted work or be on a different branch. Update via `git fetch origin` only.

## Step 10 — Validate

1. Confirm PR state is `MERGED`: `gh pr view $ARGUMENTS --json state`.
2. Confirm linked issues are `CLOSED`.
3. Confirm the head branch no longer exists on the remote.
4. Record each validation command, exit code, and concise outcome in `verification_evidence`.
5. Do not report a successful merge unless this fresh validation ran in this execution.

## Result

Output a fenced JSON block exactly like this:
```json
{
  "success": true,
  "artifacts": [{"type": "merge", "pr": 42, "merge_method": "squash", "sha": "abc123f"}],
  "issues_created": [], "issues_closed": [17], "reverted_issues": [],
  "branch_deleted": "agentshore/17-add-widget",
  "verification_evidence": [{"command": "gh pr view 42 --json state", "exit_code": 0, "summary": "state is MERGED"}],
  "error": null
}
```
If blocked, set `success: false` and include `blocked_by`. Do not omit this block.
