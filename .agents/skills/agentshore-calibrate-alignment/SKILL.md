---
name: agentshore-calibrate-alignment
description: "Action slot 18 — Calibrate Alignment. Cross-references open/merged GitHub PRs against the beads task graph, updates in-progress and closed task states, and outputs updated epic closure ratios."
argument-hint: []
disable-model-invocation: true
allowed-tools: Read, Bash(gh:*, git:*, bd:*)
---

# agentshore-calibrate-alignment

You are a AgentShore skill agent. AgentShore is a pure RL scheduler — not an LLM.
It invoked you with no parameters — this skill is self-contained.

## Inputs

None. All data is sourced from the beads project graph (`bd` CLI) and GitHub PRs.

## Forbidden mutations

Do **not** create, modify, delete, or rename any of the following:

- `.github/workflows/**` or any CI/CD configuration
- Source code files, test files, or configuration files
- `package.json`, `pyproject.toml`, or any dependency manifest
- GitHub issues (do not create, edit, or close issues)

This skill only updates **beads task states** — nothing else.

## Step 1 — Pre-flight

1. Read `.agentshore/context.json` if it exists. Extract `repo`, `owner`, and session context.
2. Confirm the beads graph is initialised by checking `.beads/` exists, then run
   `bd list --all --json --limit 0`. If this fails or returns no epic beads, emit
   `success: false` with error `"beads not initialised or no epics"` and stop.
3. Record the working directory as `REPO_ROOT=$(pwd)`.

## Step 2 — Load current beads state

1. Fetch the full beads graph, including closed tasks:
   ```
   bd list --all --json --limit 0
   ```
   Parse epics, stories, tasks, `parent`, `parent_id`, and `dependencies` entries where
   `type == "parent-child"`. Resolve every task to its owning epic through the parent chain.
   Record this as the **before** snapshot.

2. Compute closure from all task beads:
   - `total_tasks`: every bead with `type == "task"`, including closed tasks.
   - `closed_tasks`: task beads with `status == "closed"`.
   - `closure_ratio`: `closed_tasks / total_tasks`, or `0.0` when an epic has no tasks.
   For each task, record its `id`, `title`, `status`, parent chain, owning epic, and `external_ref`
   (format `gh-N` means it mirrors GitHub issue #N).

## Step 3 — Fetch GitHub state

1. Fetch open and recently closed GitHub issues:
   ```
   gh issue list --state open --limit 200 --json number,title,state
   gh issue list --state closed --limit 200 --json number,title,state,closedAt
   ```
   Build `closed_issue_numbers` from the closed issue list. This is the authoritative source
   for closing mirrored beads.

2. Fetch all open PRs:
   ```
   gh pr list --state open --limit 200 --json number,title,body,headRefName,author
   ```

3. Fetch recently merged PRs for context and reporting:
   ```
   gh pr list --state merged --limit 100 --json number,title,body,headRefName,mergedAt
   ```

4. For each PR (open and merged), extract referenced issue numbers from the body.
   Scan for these patterns (case-insensitive):
   - `Closes #N`
   - `Fixes #N`
   - `Resolves #N`
   - `Close #N`
   - `Fix #N`
   - `Resolve #N`

   Build a map: `issue_number → list of (pr_number, pr_state)` where `pr_state` is
   `"open"` or `"merged"`.

## Step 4 — Update task states

For each open bead task with an `external_ref` of the form `gh-N`:

1. Extract `N` (the GitHub issue number).
2. Compare GitHub issue and PR state:
   - If issue `N` is in `closed_issue_numbers`:
     - The issue is done. Mark the bead task `closed`:
       ```
       bd close <bead_id> --reason "GitHub issue #N is closed"
       ```
     - Record this as a `closed` update.
   - Else if any open PR references this issue:
     - Work is in progress. Mark the bead task `in_progress` (only if it is currently `open`
       or `blocked`; do not downgrade a task already `in_progress` or `closed`):
       ```
       bd update <bead_id> --status in_progress
       ```
     - Record this as an `in_progress` update.
   - Else (no PR references this issue):
     - Leave the task state unchanged. Record as `unchanged`.

For tasks **without** an `external_ref` (not mirrored from GitHub): skip — beads-native
tasks are managed directly and must not be auto-closed by this skill.

## Step 5 — Recompute epic closure ratios

After updating task states, re-fetch the full graph:

```
bd list --all --json --limit 0
```

Parse it using the same all-task calculation from Step 2. Record this as the **after** snapshot.

Compute the delta for each epic:
```
ratio_delta = after.closure_ratio - before.closure_ratio
```

## Step 6 — Validate

1. Confirm that only tasks with `external_ref` matching `gh-N` were modified.
2. Confirm no task was moved to a state lower than its current state
   (e.g., `closed` → `in_progress` must not happen).
3. Confirm the `after` epic closure ratios are >= the `before` ratios (calibration
   can only increase or maintain closure — it never decrements closed counts).

If validation fails for any reason, set `success: false` and describe the violation in `error`.

## Result

Output a fenced JSON block exactly like this:

```json
{
  "success": true,
  "artifacts": [
    {
      "epic_id": "bd-001",
      "epic_title": "Core capture pipeline",
      "closure_ratio_before": 0.40,
      "closure_ratio_after": 0.60,
      "ratio_delta": 0.20
    }
  ],
  "tasks_updated": [
    {
      "bead_id": "bd-042",
      "external_ref": "gh-17",
      "old_status": "open",
      "new_status": "in_progress",
      "pr_number": 34,
      "pr_state": "open"
    },
    {
      "bead_id": "bd-019",
      "external_ref": "gh-9",
      "old_status": "in_progress",
      "new_status": "closed",
      "pr_number": 28,
      "pr_state": "merged"
    }
  ],
  "tasks_unchanged": 12,
  "error": null
}
```

Fields:
- `artifacts`: one entry per epic with non-zero delta. Epics with `ratio_delta == 0` may be omitted.
- `tasks_updated`: every task whose state changed, with old/new status and the triggering PR.
- `tasks_unchanged`: count of tasks skipped (no PR reference or state already correct).

If any step fails, set `"success": false` and populate `"error"`.
Do not omit the result block under any circumstances.
