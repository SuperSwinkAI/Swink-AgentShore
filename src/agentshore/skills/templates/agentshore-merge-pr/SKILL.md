---
name: agentshore-merge-pr
description: "Action slot 6 — Merge PR. Verifies and merges; closes linked issues."
argument-hint: [pr_number]
disable-model-invocation: true
allowed-tools: Read, Bash(gh:*, git:*)
---

# agentshore-merge-pr

Merge PR #$ARGUMENTS from `$AGENTSHORE_PROJECT_PATH`.

**Project docs are authoritative.** Read `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, and `.github/PULL_REQUEST_TEMPLATE*` for merge requirements (signing, merge mode, commit format). Default when silent: `gh pr merge $ARGUMENTS --merge --delete-branch`. If docs require signed merges or forbid API merges, use a local `git merge --no-ff` off `origin/<target>`, push, then delete the head branch explicitly: `git push origin --delete <head_ref>`.

**Pre-flight:** PR `OPEN`, approved (GitHub `reviewDecision: APPROVED` or `$AGENTSHORE_PROJECT_PATH/.agentshore/context.json` shows AgentShore PASS at current head SHA), CI green, mergeable, base matches the configured target branch. Already-`MERGED`/`CLOSED` → no-op success. Transient states (`mergeable: UNKNOWN`, CI `PENDING`): re-check once after ~60s before failing.

**Post-merge:** close any `Closes/Fixes/Resolves #N` issues GitHub didn't auto-close. After `git fetch origin`, if `git log origin/<target> --grep='Revert.*#$ARGUMENTS'` matches without a corresponding `Reapply.*#$ARGUMENTS` → don't close; create the label if missing (`gh label create agentshore/revert-reopened --color e4e669 --description 'Resolving PR was reverted' 2>/dev/null || true`), apply it, comment with the revert SHA, and add the issue to `reverted_issues`.

**Forbidden:** `git stash` / `reset --hard` / `checkout -- <path>` against dirty trunk (skip with `error: "dirty_trunk"`); `git worktree add/remove/prune` — AgentShore owns worktree lifecycle; force-push; bypassing any stated project requirement.

**Report — one fenced JSON block, nothing else:**

```json
{
  "success": true,
  "artifacts": [{"type": "merge", "pr": 42, "merge_method": "api-merge|api-squash|api-rebase|local-no-ff-signed", "sha": "abc123f"}],
  "issues_closed": [17],
  "issues_created": [],
  "reverted_issues": [],
  "branch_deleted": "<head_ref>",
  "verification_evidence": [{"command": "...", "exit_code": 0, "summary": "..."}],
  "error": null
}
```

On block/skip: `success: false` + `error` + `blocked_by`. AgentShore masks on `dirty_trunk`, `merge_conflicts`, `wrong_base_branch`; anything else is generic. Validate before reporting — don't trust prior steps.
