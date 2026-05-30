---
name: agentshore-calibrate-alignment
description: "Action slot 18 — Calibrate Alignment. Cross-references open/merged GitHub PRs against the beads task graph, updates in-progress and closed task states, and outputs updated epic closure ratios."
argument-hint: []
disable-model-invocation: true
allowed-tools: Read, Bash(gh:*, git:*, bd:*)
---

# agentshore-calibrate-alignment

Reconcile beads task states against GitHub PR/issue state from `$AGENTSHORE_PROJECT_PATH`. Self-contained — no inputs.

**Project docs are authoritative.** Read `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, and any `docs/` for closure conventions (what counts as "done", how PRs map to issues); default to the rules below when silent. Apply concrete requirements; ignore vague advice.

**Pre-flight.** `cd "$AGENTSHORE_PROJECT_PATH"` (bd db lives in the main repo; trunk-scoped). Read `$AGENTSHORE_PROJECT_PATH/.agentshore/context.json` for `repo`, `owner`, session context. Confirm `.beads/` exists and `bd list --all --json --limit 0` succeeds with at least one epic — else `success: false`, `error: "beads not initialised or no epics"`, stop.

**Load before-snapshot.** From `bd list --all --json --limit 0`, parse epics, stories, tasks, `parent`, `parent_id`, and `dependencies` where `type == "parent-child"`. Resolve every task to its owning epic. For each task record `id`, `title`, `status`, parent chain, owning epic, `external_ref` (format `gh-N` mirrors GitHub issue #N). Per-epic closure: `total_tasks` = all task beads (including closed), `closed_tasks` = those with `status == "closed"`, `closure_ratio = closed_tasks / total_tasks` (or `0.0` when an epic has no tasks).

**Fetch GitHub state.** Run `gh issue list --state open --limit 200 --json number,title,state`, `gh issue list --state closed --limit 200 --json number,title,state,closedAt`, `gh pr list --state open --limit 200 --json number,title,body,headRefName,author`, `gh pr list --state merged --limit 100 --json number,title,body,headRefName,mergedAt`. `closed_issue_numbers` from the closed-issue list is authoritative for closing mirrored beads. For each PR (open and merged), case-insensitively extract `Closes #N`, `Fixes #N`, `Resolves #N`, `Close #N`, `Fix #N`, `Resolve #N` from the body and build `issue_number → list of (pr_number, pr_state)` where state is `"open"` or `"merged"`.

**Update task states.** For each task with `external_ref = gh-N`, in order: (1) issue `N` in `closed_issue_numbers` → `bd close <bead_id> --reason "GitHub issue #N is closed"`; (2) else if the task is `open` or `blocked` AND any **open** PR references `N` → `bd update <bead_id> --status in_progress`; (3) else if the task is `in_progress` AND issue `N` is **not** closed AND **no open** PR references `N` → the in_progress state is orphaned (its PR was closed/abandoned without merging, leaving the bead stuck `in_progress` and blocking re-dispatch of plan/pickup/refine/debug for that issue) → `bd update <bead_id> --status open`, recorded with `pr_number: null, pr_state: null`; (4) else unchanged. Never reopen a `closed` task; the **only** permitted downgrade is the orphan reset in (3). Tasks without `external_ref` are beads-native — skip; never auto-close them.

**After-snapshot and validation.** Re-run `bd list --all --json --limit 0`, recompute closure with the same all-task formula, and compute `ratio_delta = after.closure_ratio - before.closure_ratio` per epic. Validate: only `gh-N` tasks were modified; no `open`/`blocked` task moved to a lower state and no `closed` task was reopened — the **sole** permitted downgrade is the orphan reset (`in_progress → open` for a task whose issue is not closed and which no open PR references); every `after` epic ratio is `>= before`. (Closure ratio counts only `closed` tasks, so an `in_progress → open` orphan reset never lowers it — calibration still only increases or maintains.) On violation → `success: false` with the violation in `error`.

**Forbidden:** creating, modifying, deleting, or renaming `.github/workflows/**` or any CI/CD config; source/test/config files; `package.json`, `pyproject.toml`, or any dependency manifest; GitHub issues (no create/edit/close); `git worktree add/remove/prune`. This skill updates beads task states only — nothing else.

**Report — one fenced JSON block, nothing else:**

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
    },
    {
      "bead_id": "bd-006",
      "external_ref": "gh-6",
      "old_status": "in_progress",
      "new_status": "open",
      "pr_number": null,
      "pr_state": null
    }
  ],
  "tasks_unchanged": 12,
  "error": null
}
```

`artifacts` includes one entry per epic with non-zero delta (zero-delta epics may be omitted). `tasks_updated` lists every state change with old/new status and triggering PR. `tasks_unchanged` counts tasks skipped because no PR referenced them or state was already correct. On any failure, `success: false` with `error` populated. Never omit the result block.
