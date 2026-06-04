---
name: prune
description: Free disk space by removing stale git worktrees, merged/closed local AND remote branches, dead remote tracking refs, and build artifacts across the AgentShore workspace.
allowed-tools: Bash, Read
---

# /prune — Clean Up Stale Branches, Worktrees & Build Artifacts

Reclaim disk space by pruning stale git worktrees, local **and remote** branches whose
PRs are merged or closed, dead remote tracking refs, and build artifacts. Safe by
default — keeps the current branch, `main`, `integration`, `backup/*`, and any branch
with an open PR.

**`--dry-run` (binding on every step):** if `$ARGUMENTS` contains `--dry-run`, run only the
read-only survey/classification commands and **print** what each step *would* delete —
never run a `git worktree remove`, `git branch -d/-D`, `git push origin --delete`,
`git gc`, or `rm`. This applies to ALL steps below, not just the remote one.

## Step 0 — Pre-flight: never prune a live session

AgentShore holds worktrees and branches open while a session runs; pruning underneath it
corrupts the run. Abort (report and stop) if a session is active for any project:

```bash
pgrep -fl "agentshore.sidecar" || pgrep -fl "agentshore start" || echo "no live session"
```

If anything other than "no live session" prints, stop and tell the user to stop their
session(s) first. (The user may still pass `--dry-run` to preview safely while a session
runs.)

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
du -sh dist/ build/ 2>/dev/null
# Desktop build outputs + cargo cache (usually the biggest reclaimable items)
du -sh desktop/dist/ 2>/dev/null
du -sh desktop/src-tauri/target/ 2>/dev/null
# .git size
du -sh .git/
# Agent worktrees
du -sh .claude/worktrees/ 2>/dev/null
```

Report a summary table of what's consuming space before taking action.

## Step 2 — Remove stale worktrees

`git worktree remove --force` **discards any uncommitted changes** in the worktree. Before
force-removing each candidate, check `git -C "$wt" status --porcelain`; if it's non-empty,
**skip it and report it** (don't destroy unsaved work) rather than forcing.

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

**Never delete (same protected set as the remote step):** `main`, `integration`,
`backup/*`, the current branch (`git branch --show-current`), or any branch with an OPEN
PR. The protected filter below must be applied to *every* deletion command in this step —
`git branch --merged main` will list `integration`/`backup/*` when they're merged, and a
bare `xargs git branch -d` would delete them.

1. **Delete branches merged into main** (protected set excluded; `-d` is safe — it refuses
   unmerged branches):
   ```bash
   cur=$(git branch --show-current)
   git branch --merged main --format='%(refname:short)' \
     | grep -vE "^(main|integration|backup/.*|${cur})$" \
     | while read -r b; do git branch -d "$b"; done
   ```

2. **Check PR status for remaining branches** using `gh pr list --head <branch> --state all`.
   Classify each as MERGED, CLOSED, OPEN, or no-pr.

3. **Delete branches whose PRs are MERGED or CLOSED** — never a protected branch:
   ```bash
   git branch -D <branch>
   ```

4. **Delete orphan branches** (no PR, no worktree, not in the protected set).

5. **Keep branches with OPEN PRs** — report them so the user knows they exist.

## Step 4 — Delete stale remote branches

Remote-only branches accumulate when a PR merges on GitHub without auto-deleting its
branch (or when a local branch was pruned but its `origin/` counterpart was not). The
`git remote prune origin` in Step 5 does **not** remove these — it only drops local
tracking refs for branches *already* gone from the remote. Delete the stale remote
branches explicitly.

**Never delete on the remote:** `main`, `integration`, `backup/*`, the default branch,
or any branch with an OPEN PR. Protecting `integration` and `backup/*` is mandatory —
they are critical to the SDLC, even when fully merged.

1. **Refresh and list remote branches** (excluding HEAD and the protected set):
   ```bash
   git fetch --prune origin
   git for-each-ref --format='%(refname:short)' refs/remotes/origin \
     | sed 's#^origin/##' \
     | grep -vE '^(HEAD|main|integration|backup/.*)$'
   ```

2. **Classify each** with `gh pr list --head <branch> --state all --json number,state`.
   MERGED or CLOSED → deletable; OPEN → keep. A remote branch with **no PR** is
   ambiguous — keep it and report it rather than guessing.

3. **Delete the MERGED/CLOSED ones.** This is the one outbound, destructive step, so it
   honors `--dry-run`: under `--dry-run`, print the branches that *would* be deleted and
   do nothing.
   ```bash
   git push origin --delete <branch>
   ```

4. **Report** every remote branch deleted, kept (open PR), and kept (no PR).

## Step 5 — Prune remote refs & git GC

```bash
git remote prune origin
git gc --prune=now
```

## Step 6 — Clean build artifacts

```bash
# Python dist artifacts (safe to delete — rebuilt by uv build)
rm -rf dist/ build/

# Dashboard dist (rebuilt by npm run build)
rm -rf dashboard/dist/

# Vite cache
rm -rf dashboard/.vite/ dashboard/.cache/

# Desktop build outputs — the signed .app/.dmg/.pkg (rebuilt by scripts/build-macos.sh)
rm -rf desktop/dist/ desktop/src-tauri/target/release/bundle/
```

Do **not** delete (slow to rebuild, not stale by default — remove only if the user
explicitly asks):
- `.venv/`, `dashboard/node_modules/` (minutes to reinstall).
- `desktop/src-tauri/target/` (the cargo build cache — can be GBs but a full rebuild is
  expensive; surveyed in Step 1 so the user can decide). Only the regenerable
  `target/release/bundle/` outputs are cleaned above.

## Step 7 — Final report

Print a before/after summary table showing:

| Category | Before | After | Freed |
|---|---|---|---|
| `.claude/worktrees/` | ... | ... | ... |
| Sibling worktrees | ... | ... | ... |
| `dist/` | ... | ... | ... |
| `dashboard/dist/` | ... | ... | ... |
| `dashboard/.vite/` | ... | ... | ... |
| `desktop/dist/` + bundle | ... | ... | ... |
| `.git/` | ... | ... | ... |
| **Local branches** | N → M | | |
| **Remote branches** | N → M | | |
| **Worktrees** | N → M | | |

List any branches that were kept — local or remote — because they had open PRs (or, for
remote branches, no PR), plus any errors encountered.
