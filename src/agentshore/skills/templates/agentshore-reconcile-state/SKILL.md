---
name: agentshore-reconcile-state
description: "Action slot 11 — Reconcile State. Reads AgentShore's structured logs + worktree/plays DB + live git state to identify wedged session pathologies (dirty trunk from a killed mutator, orphan worktrees, zombie subprocesses, stuck lockfiles) and remediate locally. Never touches GitHub state."
disable-model-invocation: true
allowed-tools: Read, Grep, Glob, Bash(git status:*, git worktree list:*, git worktree remove --force:*, git worktree prune:*, git checkout:*, git merge --abort:*, git diff:*, git log:*, ps:*, lsof:*, kill:*, find:*, stat:*, jq:*, sqlite3:*, ls:*, tail:*, head:*, awk:*, xargs:*)
---

# agentshore-reconcile-state

Trunk-scoped local diagnosis from `$AGENTSHORE_PROJECT_PATH`. `$ARGUMENTS` is unused (the failure_streak ≥ 3 eligibility gate is the trigger). Read AgentShore's logs + DB + live process state, classify the wedge into a known pathology, then take **strictly local** remediation — no `git push`, no PR/issue mutations, no outbound GitHub calls. A pathology outside the known set is reported in the result block, not filed anywhere.

**Pre-flight.** Don't `git checkout`; read state without mutating yet.
1. Read `$AGENTSHORE_PROJECT_PATH/.agentshore/context.json` (session_id, repo, owner, learnings).
2. Read `.agentshore/contexts/<session_id>/play-<play_id>.json`. The orchestrator pre-writes `recent_wedge_signals` here — prefer it over re-deriving via SQL. Expected keys (best-effort): `stagnation_ticks`, `same_type_failure_streak`, `last_failed_play_type`, `last_failed_error`, `recent_timeouts[]`, `dirty_trunk_paths[]`, `orphan_worktree_paths[]`, `active_agents_in_flight[]` (each entry: `agent_id`, `agent_type`, `current_play_id`, `current_play_type`, `current_play_started_at`). **The `active_agents_in_flight` list is the authoritative cross-check for zombie classification — read it before classifying any process as a zombie.**
3. If `.agentshore/session_start_dirty.json` exists, prefer it — it's the authoritative pre-session baseline captured at bootstrap, and survives DB corruption. Use its `modified_paths[].mtime_utc` and `summary` (`pre_session`, `mtime_cluster_span_seconds`).
4. Scan structured logs **across all sessions**, newest-first (`ls -t .agentshore/logs/agentshore-*.log | head -10`). Killed-prior-play evidence often lives in an earlier session's file. `tail -n 500` each and look for `cli_dispatch_start`, `agent_dispatch_timed_out`, `play_execution_timeout`, `play_completed:success=false`, `db_recovered_via_sqlite_recover`. Most recent matching evidence wins; stop once you've attributed every dirty file's mtime range.
5. Capture ground truth: `git status --porcelain`, `git worktree list --porcelain`, `ps -ax -o pid,ppid,etime,state,command | grep -E '(claude|uv run|pytest)' | head -50`.

**Diagnose.** Classify into one or more known pathologies. Only act on a classification with unambiguous evidence; if ambiguous, leave it alone and file a follow-up in Step 5.

- **`dirty_trunk_from_killed_mutator`** — `git status` shows `M/A/D/R` entries AND a trunk-scoped play (`CLEANUP`, `MERGE_PR`, `DESIGN_AUDIT`, `GROOM_BACKLOG`, `RUN_QA`, `WRITE_IMPLEMENTATION_PLAN`, `REFINE_TASK_BREAKDOWN`, `SEED_PROJECT`, `CALIBRATE_ALIGNMENT`) failed within a window matching the dirty-file mtimes. Primary signal is **mtime clustering** — capture mtimes (`stat -f "%Sm %N" -t "%Y-%m-%dT%H:%M:%S"` on macOS, `stat -c "%y %n"` on Linux), compute span and overlap with prior killed-play windows. Decision:

  | Cluster span | Matches prior killed-play window? | Decision |
  |---|---|---|
  | < 120s, many files | yes | classify (high confidence) |
  | < 120s, many files | no log (logs rotated) | classify if pre-session AND auto-fixer signature (medium) |
  | > 1h, sparse | any | DO NOT classify — likely user WIP |
  | < 120s, few files (≤ 3) | any | DO NOT classify alone — file follow-up |

  Secondary content signals (corroborate only): auto-fixer reformat patterns / lockfile churn (cleanup); new test files / deleted scaffolding (implementation plays).

  **Untracked root artifacts are NOT this pathology.** `dirty_trunk_from_killed_mutator` covers **tracked** modifications (`M/A/D/R`) only. Untracked (`??`) files at the repo root left by trunk-scoped plays are handled deterministically by the orchestrator (per-play reclaim + a session-start sweep that quarantines them under `.agentshore/reclaimed/<play_id>/`), not by this skill — never `git clean` them. In particular, any `recent_wedge_signals.dirty_trunk_paths` entry with `owned_by_active_play: true` is **in-flight work of a still-running trunk-scoped play, not a wedge** (#162): exclude it from every pathology, never act on it, and do not let it drive `success: false`. A `??` root entry with `owned_by_active_play: false` is orphaned debris the orchestrator's sweep will reclaim — record it under `unrecognized_pathologies` for visibility and move on; it is not yours to delete or restore.

- **`conflicted_merge_in_progress`** — `$AGENTSHORE_PROJECT_PATH/.git/MERGE_HEAD` exists, or `git status --porcelain` shows unmerged entries (`UU`/`AA`/`DD`/`AU`/`UA`/`UD`/`DU`). A killed/errant `merge_pr` left the main checkout mid-merge with unresolved conflicts — this is the state that latches the trunk-dispatch pause. High confidence whenever `MERGE_HEAD` is present; no mtime analysis needed.

- **`orphan_worktrees`** — `git worktree list --porcelain` shows paths under the project's worktree root with no active session row in `sqlite3 .agentshore/agentshore.db "SELECT worktree_path FROM worktrees WHERE session_id='<current>' AND status='active'"`.

- **`zombie_subprocess`** — `ps` state `Z` AND ppid=1 (reparented) OR ppid matches a recent `cli_dispatch_start` whose play later emitted `agent_dispatch_timed_out`. Only classify if you can name the specific `play_id` and timeout event. **Critical additional constraint**: before classifying any PID as a zombie, cross-check that the associated `play_id` is NOT the current active in-flight play for that agent. An agent process is only a zombie if (a) it satisfies the `ps`/log criteria above AND (b) the orchestrator's current session does not have that play listed as actively in-flight right now. If the orchestrator DB shows `plays.completed_at IS NULL AND plays.started_at IS NOT NULL` for that `play_id` (meaning the play is still running), the process is NOT a zombie — it is a live agent backing an active play and must not be killed. Query: `sqlite3 .agentshore/agentshore.db "SELECT play_id, play_type, started_at, completed_at FROM plays WHERE play_id = <play_id>"`. If `completed_at` is NULL, the play is still active; skip this PID entirely and record it under `unrecognized_pathologies` for operator review instead.

- **`stale_worktree_lockfile`** — `lsof` shows a lockfile (`.git/index.lock`, `~/.cache/uv/.lock`, `.beads/lock`, etc.) held past the holding play's expected timeout, cross-referenced to `cli_dispatch_start` for the PID.

**Remediate.** Only act on classifications from above.
- **Conflicted merge in progress:** `git merge --abort` (the only merge operation this skill may run; it unwinds the in-progress merge and restores the pre-merge worktree — the conflicting work lives on the PR branch, not trunk). Verify `.git/MERGE_HEAD` is gone and `git status --porcelain` has no unmerged (`UU`/etc.) entries → `remediation.merge_aborted: true`. Do this BEFORE any per-path dirty-trunk restore, since unmerged paths can't be `git checkout -- `'d while the merge is live. Failure → `remediation.merge_aborted: false`, `success: false`.
- **Dirty trunk:** `git checkout -- <path>` per attributed path. Never `--force`/`--theirs`. Verify `git status --porcelain` clears the path → `remediation.trunk_paths_restored`. Failures → `trunk_paths_failed`, continue.
- **Orphan worktrees:** these are **registered** in git metadata, so a DB-only mark-stale leaves the registration behind and Verify can never pass. Remove the registration for real, preserving genuinely-uncommitted work. Per orphan path: check dirtiness with `git -C <path> status --porcelain` (best-effort).
  - **Clean** (empty porcelain, or git can't introspect the de-registered dir): `git worktree remove --force <path>` then `git worktree prune`. Verify the path is gone from `git worktree list --porcelain`, then mark the matching active DB row stale with `failure_reason='reconcile_state: orphan removed'` → `remediation.worktrees_removed`.
  - **Dirty** (porcelain shows uncommitted changes): do **not** destroy unsaved work. Leave the worktree in place, mark the DB row stale (`failure_reason='reconcile_state: orphan preserved (uncommitted changes)'`), and record the path under `remediation.worktrees_preserved_dirty`. A preserved-dirty orphan makes that category `partial`, not `success: false`.
- **Zombies:** `kill -9 <pid>` only for provably AgentShore-owned processes where the DB confirms the backing play has already completed (`completed_at IS NOT NULL`) or timed out. A process whose `play_id` still has `completed_at IS NULL` in the DB is an active agent — do not kill it. Verify gone with `ps -p`. Record PID + 80-char command excerpt + the log line proving ownership + the `completed_at` value → `remediation.processes_killed`.
- **Stale lockfile:** confirm holder is AgentShore-spawned, kill (zombie rules), wait ≤ 5s for auto-clear. Only `rm` the lockfile if it's in `.agentshore/`, `.cache/uv/`, or a named AgentShore-managed path → `remediation.lockfiles_cleared`.

**Verify.** Re-run ground-truth checks; each remediated category must come back clean (or be reported `partial`). `git status --porcelain` (after filtering AgentShore sidecars) empty; every `git worktree list` entry beyond main matches an active DB row **or** is a preserved-dirty orphan you recorded above (those leave the worktree category `partial`); no zombie children of dispatch parents; no AgentShore-owned process holding a lock past its timeout. Record each check command + exit_code + summary in `verification_evidence`. Failure here → `success: false` with explanation in `error`.

**Unrecognized pattern.** If diagnosis saw a pathology outside the known set above, **do not file anything** — record it in the result block under `unrecognized_pathologies` (a one-line summary plus the supporting log/git/ps/lsof excerpts and `session_id`) so an operator can review it. No GitHub calls.

**Forbidden mutations:**
- Never create/edit/restore/delete `.github/workflows/**`, `.github/actions/**`, `.gitlab-ci.yml`, `.circleci/**`, `azure-pipelines.yml`, `Jenkinsfile`, `bitbucket-pipelines.yml`, or tests asserting their existence. Any such remediation → `success: false`, `error: "ci-change requested but forbidden by skill policy"`, files untouched.
- Never `git push`, and never `gh pr/issue create/close/edit/comment/merge` — this skill makes no GitHub mutations at all.
- Never `git stash` (entries leak across branches/sessions).
- Never `git worktree add` — the skill never creates worktrees. `git worktree remove --force` + `git worktree prune` are permitted **only** for orphan-worktree remediation (a registered worktree with no active session row), and only after the dirty-check above; otherwise `git worktree list` is read-only.
- Never `git reset --hard`, `git clean -f`, or `git checkout` with branch switching. Working-tree restore is `git checkout -- <path>` on **specific paths attributed to a killed prior play**.
- Never `kill -9` a process you cannot prove (via the log) is a defunct child of a timed-out dispatch.

**Report — one fenced JSON block, nothing else:**

```json
{
  "success": true,
  "artifacts": [
    {
      "type": "diagnosis",
      "pathologies": ["dirty_trunk_from_killed_mutator"],
      "evidence": {
        "dirty_paths": ["src/foo.py", "tests/test_foo.py"],
        "attributed_to_play_id": 6716,
        "attributed_to_play_type": "cleanup",
        "killed_event": "agent_dispatch_timed_out at 15:21:05Z"
      }
    }
  ],
  "remediation": {
    "trunk_paths_restored": ["src/foo.py", "tests/test_foo.py"],
    "trunk_paths_failed": [],
    "worktrees_removed": [],
    "worktrees_preserved_dirty": [],
    "processes_killed": [],
    "lockfiles_cleared": []
  },
  "verification_evidence": [
    {"command": "git status --porcelain", "exit_code": 0, "summary": "clean (after filtering .agentshore/, .beads/, .agents/)"},
    {"command": "git worktree list --porcelain", "exit_code": 0, "summary": "1 main + 0 active session worktrees"}
  ],
  "unrecognized_pathologies": [],
  "error": null
}
```

`success: false` only when verification fails after remediation, or no remediation was possible (ambiguous ownership across all pathologies). A run that diagnosed nothing actionable is still `success: true` with empty `remediation` and `pathologies` — a no-op confirming the state is fine. **A checkout whose only dirty state is untracked root artifacts (`??` at the repo root) — whether `owned_by_active_play: true` in-flight work or orphaned debris the orchestrator's sweep owns — is exactly this no-op `success: true` case, never `success: false`**: those files are not this skill's to remediate, so their presence alone must not fail the run. Always emit the block.
