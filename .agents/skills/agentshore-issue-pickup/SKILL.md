---
name: agentshore-issue-pickup
description: "Action slot 4 — Issue Pickup. Reviews open issues (or accepts an issue number), implements the highest-value eligible work in an isolated git worktree, validates, and opens a PR. Use when AgentShore dispatches a coding agent to make progress."
argument-hint: []
disable-model-invocation: true
allowed-tools: Read, Edit, Write, Bash(gh:*, git:*, npm:*, python3:*, pytest:*, cargo:*, make:*, uv:*, bun:*, pnpm:*, yarn:*)
---

# agentshore-issue-pickup

You are a AgentShore skill agent. AgentShore is a pure RL scheduler — not an LLM.
It invoked you with parameters in `$ARGUMENTS`.

## Forbidden mutations

Never touch `.github/workflows/**`, `.github/actions/**`, other CI configs (`.gitlab-ci.yml`, `.circleci/**`, `azure-pipelines.yml`, `Jenkinsfile`, `bitbucket-pipelines.yml`), or tests that assert their existence. If CI changes are required, post a comment stating CI config is owned by the human maintainer, request the `agentshore/disallowed` label, then exit with `"success": false` and `"error": "ci-change requested but forbidden by skill policy"`.

## Inputs

- `$ARGUMENTS` — optional GitHub issue number. If provided, work on that issue directly.
  If empty, select the highest-value eligible issue yourself (see Step 2).

## Step 1 — Pre-flight (in the main project directory)

1. Record the main project directory: `MAIN_REPO=$(pwd)`.
2. Check the working tree state: `git status --porcelain`.
   **Do not run `git stash`.** If unrelated uncommitted changes exist in the main worktree, leave them alone — your work will happen in an isolated worktree, so they won't be touched.
3. Fetch latest remote refs: `git fetch origin`.
4. Detect the default branch (fall back to `main`):
   ```
   DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
   DEFAULT_BRANCH=${DEFAULT_BRANCH:-main}
   ```

## Step 2 — Select an issue (only if $ARGUMENTS is empty)

If an issue number was provided in `$ARGUMENTS`, set `ISSUE_NUMBER` to it and skip to Step 3.

Otherwise:

1. Read `.agentshore/context.json` if it exists. Note beads graph context, session learnings, and recent outcomes.
2. List all open issues:
   ```
   gh issue list --state open --limit 200 --json number,title,labels,assignees,body
   ```
3. List open PRs to identify issues already in progress:
   ```
   gh pr list --state open --json number,title,headRefName,body
   ```
   Extract issue numbers covered by open PRs (from branch names matching `agentshore/<N>-*` and from `Closes #N` / `Fixes #N` references in PR bodies).
4. Filter out issues that are:
   - Already covered by an open PR.
   - Labeled `agentshore/blocked`.
   - Labeled `agentshore/disallowed`.
   - Labeled `agentshore/needs-refinement` (still awaiting scope analysis).
5. From remaining candidates, select the single best issue using this priority order:
   - Highest `priority/*` label (`critical` > `high` > `medium` > `low`).
   - Smallest `size/*` label (`S` > `M` > `L`) to prefer quick wins.
   - Lowest issue number as a tiebreaker.
6. If no eligible issues remain, output the result block with `"success": false` and `"error": "no eligible issues available"` and stop.
7. Set `ISSUE_NUMBER` to the chosen issue.

## Step 3 — Understand the work

1. Use the `learnings` field from `.agentshore/context.json` for relevant patterns and past mistakes.
2. Read project-level configuration files (`CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, or similar) for coding conventions and constraints.
3. Fetch the issue:
   ```
   gh issue view $ISSUE_NUMBER --json number,title,body,labels,assignees,milestone,comments
   ```
4. Parse the issue body for acceptance criteria, linked issues, dependencies, and technical hints.
5. Look for an issue comment beginning with `AGENTSHORE_IMPLEMENTATION_PLAN`.
   If present, treat it as the controlling plan for files, task order, and validation.
6. If the issue has sub-issues or a parent reference, fetch those for full context.
7. Compute a kebab-case slug from the issue title (max 50 chars). Set `BRANCH=agentshore/<ISSUE_NUMBER>-<slug>`.

## Step 4 — Create an isolated worktree

This skill **always works in a dedicated worktree** so concurrent agents in the same repo do not fight over the working tree.

1. Compute the worktree path: `WORKTREE="$MAIN_REPO/.agentshore/worktrees/$ISSUE_NUMBER"`.
2. If `$WORKTREE` already exists from a prior failed run, remove it first:
   ```
   git worktree remove --force "$WORKTREE" 2>/dev/null || rm -rf "$WORKTREE"
   ```
3. If the branch `$BRANCH` already exists locally from a prior failed run, delete it:
   ```
   git branch -D "$BRANCH" 2>/dev/null || true
   ```
4. Create the worktree on a new branch off the latest remote default tip:
   ```
   git worktree add -b "$BRANCH" "$WORKTREE" "origin/$DEFAULT_BRANCH"
   ```
5. From this point on, run **all** subsequent commands inside the worktree:
   ```
   cd "$WORKTREE"
   ```

## Step 5 — Discover and read relevant code

1. Identify files referenced in the issue body or related to the component.
2. Search the codebase for relevant symbols, imports, and patterns.
3. Read each relevant file to understand the current implementation.
4. Read relevant specs, design docs, or architecture docs if they exist.
5. Identify the test files associated with the code you will change.

## Step 6 — Plan the implementation

1. Before editing production code, write a short task checklist for this issue.
2. If a `AGENTSHORE_IMPLEMENTATION_PLAN` exists, follow it. If it is stale or unsafe, note why
   in the PR body and use the smallest corrected plan.
3. If no plan exists, create a mini-plan covering:
   - exact files you expect to modify
   - test cases to add or update
   - validation commands and expected outcomes
4. If implementation becomes too large for one pass, stop with status `BLOCKED` or
   `DONE_WITH_CONCERNS`; do not silently broaden scope.

## Step 7 — Implement with TDD discipline

1. For behavior changes, write or update the failing test before production code.
2. Run the narrowest test command and confirm it fails for the expected reason.
3. Implement the smallest complete change that meaningfully advances the issue.
4. Follow existing code style, naming conventions, and patterns discovered in the project.
5. Keep each edited file focused — do not include unrelated changes.
6. Re-run the narrowest test and confirm it passes.
7. Include at least one positive case and one edge case when the issue behavior warrants it.

## Step 8 — Validate

1. Detect the project's toolchain from `package.json`, `pyproject.toml`, `Cargo.toml`, `Makefile`, or similar.
2. Run the narrowest appropriate validation for the files you touched first (e.g., a single test file or module).
3. If that passes, run broader validation when the blast radius warrants it (full test suite, linter, type checker, formatter check).
4. If any validation fails, read the failure output, fix the code, and re-run.
5. Do not proceed until all validation passes.
6. Do not run multiple builds or test suites concurrently.
7. Record exact commands, exit codes, and short outcomes for `verification_evidence`.

## Step 9 — Commit and push

1. Re-check `git status` and ensure only intentional changes are staged.
2. Stage only the files you changed: `git add <files>`.
3. Write a conventional commit message referencing the issue:
   ```
   git commit -m "<type>: <summary> (#$ISSUE_NUMBER)"
   ```
4. Fetch origin again before pushing: `git fetch origin`.
5. If `origin/$DEFAULT_BRANCH` moved during your work, rebase onto it and re-run validation:
   ```
   git rebase "origin/$DEFAULT_BRANCH"
   ```
6. Push the branch: `git push -u origin HEAD`.
7. Verify the push succeeded: `git log "origin/$BRANCH" -1 --oneline`.

## Step 10 — Open a pull request

1. Create the PR (the `merge_pr` and `code_review` plays depend on this — do not skip):
   ```
   gh pr create \
     --title "<type>: <summary>" \
     --body "Closes #$ISSUE_NUMBER

## Changes
- <bullet list>

## Test plan
- <how to verify>" \
     --base "$DEFAULT_BRANCH"
   ```
2. Record the PR URL.
3. Confirm the PR was created: `gh pr view --json url,state`.
4. Confirm the issue is referenced in the PR body.

## Step 11 — Cleanup

1. Return to the main project directory: `cd "$MAIN_REPO"`.
2. Remove the worktree (the branch stays on origin via the PR):
   ```
   git worktree remove "$WORKTREE" || git worktree remove --force "$WORKTREE"
   ```

## Result

Output a fenced JSON block exactly like this:
```json
{
  "success": true,
  "status": "DONE",
  "artifacts": [{"type": "pr", "url": "https://github.com/owner/repo/pull/42", "head_sha": "abc123"}],
  "issues_created": [],
  "requested_mutations": [],
  "issue_picked_up": 17,
  "branch": "agentshore/17-add-widget",
  "tests_passed": true,
  "verification_evidence": [{"command": "pytest tests/test_widget.py -v", "exit_code": 0, "summary": "12 passed"}],
  "error": null
}
```

`status`: `DONE` | `DONE_WITH_CONCERNS` | `NEEDS_CONTEXT` | `BLOCKED`. Use `DONE_WITH_CONCERNS` when the change partially advances the issue. On irrecoverable failure set `"success": false`. Always attempt Step 11 cleanup even on failure. Do not omit this block.

For policy-disallowed issues, use this mutation shape so AgentShore can apply a durable terminal gate:
```json
{
  "success": false,
  "status": "BLOCKED",
  "artifacts": [],
  "issues_created": [],
  "requested_mutations": [{"type": "label_issue", "issue": 17, "labels": ["agentshore/disallowed"]}],
  "issue_picked_up": 17,
  "branch": null,
  "tests_passed": null,
  "verification_evidence": [],
  "error": "ci-change requested but forbidden by skill policy"
}
```
