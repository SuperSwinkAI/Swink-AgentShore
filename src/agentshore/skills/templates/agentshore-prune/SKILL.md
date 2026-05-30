---
name: agentshore-prune
description: "Action slot 19 — Prune. Retires infrastructure debt that accumulates within a session: orphan worktrees, dead local/remote branches, and beads whose linked GitHub issue is already closed. Conservative on unlinked beads (epic-decomposition residue is never touched)."
disable-model-invocation: true
allowed-tools: Read, Grep, Glob, Bash(*)
---

# agentshore-prune

Retire session-level infrastructure debt from `$AGENTSHORE_PROJECT_PATH`: orphan worktrees, dead branches, and stale linked beads.

**Scope.** Trunk-scoped — you are in the main project checkout on the target branch and create no commits or branches. `git worktree remove` and `git worktree prune` ARE allowed here (this is the one play that owns worktree lifecycle cleanup); `git worktree add` is not.

**Project docs are authoritative.** Read `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, and any `.agentshore/` docs for protected branches, worktree-root paths, and bead-closure conventions; default to the rules below when silent. Apply concrete project requirements; ignore vague advice.

**Stale criteria (the four sweeps):**

- **Worktree:** path matches `pickup-*` or `agentshore-*` under the AgentShore worktree root AND its branch has no commits beyond merge-base with target, OR is the head of a closed/merged PR, OR has no open PR and no in-flight work claim.
- **Local branch:** matches `agentshore/*` or `pickup-*` AND its PR is closed/merged OR its tracking remote is gone.
- **Remote branch:** matches `agentshore/*` or `pickup-*` AND its PR is closed/merged AND no local worktree references it.
- **Bead (LINKED ONLY):** open AND has `external_ref: "gh-N"` AND issue #N is CLOSED. Unlinked beads are epic-decomposition residue — never auto-close them.

**Pre-flight.** Read `$AGENTSHORE_PROJECT_PATH/.agentshore/context.json` for `repo`, `owner`, `target_branch`, `session_id`. Resolve `$TARGET` from `$ARGUMENTS` (override), context, or repo default. Resolve `$WORKTREE_ROOT` from `.agentshore/worktrees-root.txt`, then `$AGENTSHORE_WORKTREES_ROOT`, else `<parent-of-project>/agentshore-worktrees/<repo-name>/`.

**Snapshot once.** Batch via `gh pr list --state all --limit 200 --json number,headRefName,state,mergedAt,closedAt`, `gh issue list --state closed --limit 500 --json number`, `gh issue list --state open --limit 500 --json number`. Build `closed_pr_branches: {headRefName: pr_number}` (state CLOSED/MERGED), `open_pr_branches`, `closed_issue_numbers`, `open_issue_numbers`.

**Worktree sweep.** From `git worktree list --porcelain`, for each worktree under `$WORKTREE_ROOT` matching `pickup-*`/`agentshore-*`: classify by branch — `closed_pr_branches` → orphan (reason `pr_closed:#N`); `open_pr_branches` → keep; else `git rev-list --count $TARGET..<branch> == 0` → orphan (reason `no_commits_beyond_target`); else keep. Remove orphans with `git worktree remove --force <path>`; finish with `git worktree prune --verbose`. Failures (locked, perms) go to `worktrees_skipped` with the error.

**Local branch sweep.** From `git branch --list 'agentshore/*' 'pickup-*' --format='%(refname:short)'`: skip current HEAD (`current_head`); skip `open_pr_branches` (`open_pr`); `closed_pr_branches` → `git branch -D` → `branches_deleted_local`; else if `git ls-remote --exit-code origin "refs/heads/<branch>"` reports gone → `git branch -D` → `branches_deleted_local`; else keep (`remote_exists_no_pr`).

**Remote branch sweep.** From `git ls-remote origin 'refs/heads/agentshore/*' 'refs/heads/pickup-*'`: skip `open_pr_branches`; skip if any local worktree still references it (re-check post-worktree-sweep); `closed_pr_branches` → `git push origin --delete <branch>` → `branches_deleted_remote`; else keep.

**Bead sweep (linked only).** From `(cd "$AGENTSHORE_PROJECT_PATH" && bd list --status open --limit 0 --json)`: for each open bead with `external_ref` starting `gh-`, parse `N`. In `closed_issue_numbers` → `bd close <bead_id> --reason "stale: gh-<N> closed"` → `beads_closed_stale_linked`. In `open_issue_numbers` → keep. In neither (untracked/deleted/transferred) → keep, record under `beads_unknown_gh_state`. Close children before parents to keep the dependency graph consistent.

**Forbidden:** deleting `main`, `master`, the resolved target branch, any branch with `protected` in its remote ref-prefix, or any branch matching a tag pattern — record under `skipped_branches` with reason; never closing unlinked beads; never `git worktree add`; no commits or branch switches (leave trunk as you found it).

**Report — one fenced JSON block, nothing else:**

```json
{
  "success": true,
  "artifacts": [
    {"type": "prune_summary", "worktrees_pruned": 3, "branches_deleted_local": 4, "branches_deleted_remote": 2, "beads_closed_stale_linked": 12}
  ],
  "worktrees_pruned": ["/Users/.../agentshore-974-foo", "..."],
  "worktrees_skipped": [],
  "branches_deleted_local": ["agentshore/974-foo", "..."],
  "branches_deleted_remote": ["pickup-965", "..."],
  "skipped_branches": [{"branch": "agentshore/971-bar", "reason": "open_pr"}],
  "beads_closed_stale_linked": ["open-stocks-mcp-1fu", "..."],
  "beads_unknown_gh_state": [{"bead_id": "...", "external_ref": "gh-9999"}],
  "open_work_after": {"issues": 9, "prs": 1, "worktrees": 4, "beads_open": 145},
  "error": null
}
```

`success: false` only for: target branch unresolvable, batch GitHub fetch refused after retry, or every worktree removal failed (suggests deeper FS problem). A run that finds nothing to prune is `success: true` with zero counts.
