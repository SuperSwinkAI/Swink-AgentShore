---
name: prune
description: Free disk space by removing stale git worktrees, merged/closed branches, remote tracking refs, and build artifacts across the AgentShore workspace.
allowed-tools: Bash, Read
---

# /prune — Clean Up Stale Branches, Worktrees & Build Artifacts

Reclaim disk space by pruning stale git worktrees, branches whose PRs are merged or
closed, dead remote tracking refs, and build artifacts. Safe by default — keeps the
current branch, `main`, and branches with open PRs.

If $ARGUMENTS contains `--dry-run`, report what would be cleaned without deleting anything.

## Step 1 — Survey disk usage

Run these in parallel to understand the current state:

```bash
# Worktrees
git worktree list
# Local branch count
git branch --list | wc -l
# Build artifact sizes
du -sh .venv/ 2>/dev/null
du -sh dashboard/node_modules/ 2>/dev/null
du -sh dashboard/dist/ 2>/dev/null
du -sh dist/ 2>/dev/null
# .git size
du -sh .git/
# Agent worktrees
du -sh .claude/worktrees/ 2>/dev/null
```

Report a summary table of what's consuming space before taking action.

## Step 2 — Remove stale worktrees

1. **Prune dead references** — worktrees whose directories no longer exist:
   ```bash
   git worktree prune -v
   ```

2. **Remove `.claude/worktrees/`** — agent-spawned worktrees that accumulate over time:
   ```bash
   git worktree list | grep '\.claude/worktrees/' | awk '{print $1}' | while read wt; do
     git worktree remove --force "$wt" 2>&1
   done
   ```

3. **Remove sibling `*-wt-*` worktrees** (e.g. `AgentShore-wt-42`):
   ```bash
   git worktree list | grep -- '-wt-' | awk '{print $1}' | while read wt; do
     git worktree remove --force "$wt" 2>&1
   done
   ```

## Step 3 — Delete stale local branches

**Never delete:** `main`, the current branch (from `git branch --show-current`).

1. **Delete branches merged into main:**
   ```bash
   git branch --merged main | grep -v '^\*' | grep -v 'main' | xargs git branch -d
   ```

2. **Check PR status for remaining branches** using `gh pr list --head <branch> --state all`.
   Classify each as MERGED, CLOSED, OPEN, or no-pr.

3. **Delete branches whose PRs are MERGED or CLOSED:**
   ```bash
   git branch -D <branch>
   ```

4. **Delete orphan branches** (no PR, no worktree, not current, not main).

5. **Keep branches with OPEN PRs** — report them so the user knows they exist.

## Step 4 — Prune remote refs & git GC

```bash
git remote prune origin
git gc --prune=now
```

## Step 5 — Clean build artifacts

```bash
# Python dist artifacts (safe to delete — rebuilt by uv build)
rm -rf dist/ build/

# Dashboard dist (rebuilt by npm run build)
rm -rf dashboard/dist/

# Vite cache
rm -rf dashboard/.vite/ dashboard/.cache/
```

Do **not** delete `.venv/` or `dashboard/node_modules/` — those take minutes to rebuild
and are not stale by default. Only remove them if the user explicitly asks.

## Step 6 — Final report

Print a before/after summary table showing:

| Category | Before | After | Freed |
|---|---|---|---|
| `.claude/worktrees/` | ... | ... | ... |
| Sibling worktrees | ... | ... | ... |
| `dist/` | ... | ... | ... |
| `dashboard/dist/` | ... | ... | ... |
| `dashboard/.vite/` | ... | ... | ... |
| `.git/` | ... | ... | ... |
| **Branches** | N → M | | |
| **Worktrees** | N → M | | |

List any branches that were kept (open PRs) and any errors encountered.
