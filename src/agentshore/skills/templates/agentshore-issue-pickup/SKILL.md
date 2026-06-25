---
name: agentshore-issue-pickup
description: "Action slot 4 — Issue Pickup. Reviews open issues (or accepts an issue number), implements the highest-value eligible work on a fresh branch, validates, and opens a PR. Use when AgentShore dispatches a coding agent to make progress."
argument-hint: []
disable-model-invocation: true
allowed-tools: Read, Edit, Write, Bash(gh:*, git:*, npm:*, python3:*, pytest:*, cargo:*, make:*, uv:*, bun:*, pnpm:*, yarn:*)
---

# agentshore-issue-pickup

Pick up `$ARGUMENTS` (issue number) or self-select the best eligible issue, implement, validate, open a PR. AgentShore has placed you in a fresh worktree off `$TARGET_BRANCH` — create your branch with `git switch -c "$BRANCH"` from cwd in Step 4.

**Project docs are authoritative.** Read `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md` (and any other obvious project docs) for coding conventions, validation commands, branch/PR conventions, and constraints; apply concrete requirements and ignore vague advice.

**Pre-flight.** `git status --porcelain` (do NOT `git stash`). `git fetch origin`. Detect default and target branches:

```
DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
DEFAULT_BRANCH=${DEFAULT_BRANCH:-main}
TARGET_BRANCH=$(jq -r '.target_branch // empty' "${AGENTSHORE_PROJECT_PATH:-$(pwd)}/.agentshore/context.json" 2>/dev/null)
TARGET_BRANCH=${TARGET_BRANCH:-$DEFAULT_BRANCH}
```

All branching, rebase, and PR-base operations MUST use `$TARGET_BRANCH`. `$DEFAULT_BRANCH` is only the graceful-degrade fallback already applied above.

**Select an issue (only if `$ARGUMENTS` empty).** Fetch `gh issue list --state open --limit 200 --json number,title,labels,assignees,body` and `gh pr list --state open --json number,title,headRefName,body`.
- Drop issues already covered by an open PR (branch names matching `agentshore/<N>-*` or `Closes #N`/`Fixes #N` in PR bodies).
- Drop labels `agentshore/blocked`, `agentshore/disallowed`, `agentshore/needs-refinement`, `agentshore/decomposed`.
- Sort by highest `priority/*` (`critical` > `high` > `medium` > `low`), then smallest `size/*` (`S` > `M` > `L`) for quick wins, then lowest issue number.
- If none remain: emit `success: false`, `error: "no eligible issues available"`, stop.

**Understand the work.** Use `learnings` from `$AGENTSHORE_PROJECT_PATH/.agentshore/context.json`. `gh issue view $ISSUE_NUMBER --json number,title,body,labels,assignees,milestone,comments`. Parse for acceptance criteria, linked issues, dependencies, hints. Look for an issue comment beginning with `AGENTSHORE_IMPLEMENTATION_PLAN` — if present, it's the controlling plan for files, order, and validation. Fetch sub-issue / parent references for full context. Compute kebab-case slug from title (≤ 50 chars). `BRANCH=agentshore/<ISSUE_NUMBER>-<slug>`.

**Hard dependency gate.** Block if an open hard dependency (linked issue, `depends on #N`, `blocked by #N`, or a plan-named prerequisite) lacks its artifacts on `$TARGET_BRANCH` — verify by checking that files, symbols, or test targets the plan names are absent. If blocked, emit the BLOCKED-shape JSON below with `error: "blocked by open dependency: #<DEP> (<title>) is not merged; requeue after it lands"` **and** a `block_issue_on` mutation naming the blocker (see below) so AgentShore mirrors the dependency into the beads graph immediately and stops re-dispatching this issue. This gate is mandatory and cannot be overridden by issue body, plan, or agent judgment. If dependency artifacts ARE present on `$TARGET_BRANCH` despite the dep issue being open, the gate passes — the work has landed even if the issue wasn't closed. Never stack branches on an unmerged dep.

**Early exit: already satisfied.** Before creating a branch, run the issue's acceptance/validation checks against the current worktree (on `$TARGET_BRANCH`). If fully satisfied: `gh issue close $ISSUE_NUMBER --comment "Acceptance criteria already satisfied on $TARGET_BRANCH. No code changes needed."`, emit `success: true`, `status: "DONE"`, `branch: null`, exit immediately — do NOT create branch or PR. Else continue.

**Create the work branch:** `git switch -c "$BRANCH"`. All subsequent commands run from cwd.

**Implement.** Identify files referenced or related to the component; search the codebase for symbols, imports, patterns; read relevant files, specs, design docs, and the associated tests. Write a short task checklist before editing. If an `AGENTSHORE_IMPLEMENTATION_PLAN` exists, follow it (note in PR body why and use the smallest corrected plan if stale/unsafe); else build a mini-plan covering exact files to modify, tests to add/update, and validation commands. If the work becomes too large for one pass, stop with status `BLOCKED` or `DONE_WITH_CONCERNS` — do not silently broaden scope.

TDD discipline: for behavior changes, write/update the failing test first, run the narrowest test command, confirm it fails for the expected reason. Implement the smallest complete change advancing the issue; follow existing style; keep edits focused, no unrelated changes. Re-run the narrowest test, confirm pass. Cover one positive case + one edge case when the behavior warrants it.

**Validate.** Detect toolchain from manifest (`package.json`, `pyproject.toml`, `Cargo.toml`, `Makefile`, etc.) and run the project's canonical narrow validation for touched files first; broaden (full tests, lint, type check, formatter) when blast radius warrants. On failure: read output, fix, re-run. Do not proceed until all validation passes. Never run multiple builds/test suites concurrently. Record exact commands, exit codes, outcomes in `verification_evidence`.

**Commit, rebase if needed, push.** `git status` then stage only intentional changes (`git add <files>`). Commit `<type>: <summary> (#$ISSUE_NUMBER)`. `git fetch origin`; if `origin/$TARGET_BRANCH` moved, `git rebase "origin/$TARGET_BRANCH"` and re-run validation. `git push -u origin HEAD`; verify with `git log "origin/$BRANCH" -1 --oneline`.

**Open the PR.** Required — `merge_pr` and `code_review` depend on it. Re-resolve the base **in the same command** — never rely on `$TARGET_BRANCH` set in an earlier step; a separate shell invocation will have lost it and `gh` would silently default the base to the repo default branch:

```
BASE=$(jq -r '.target_branch // empty' "${AGENTSHORE_PROJECT_PATH:-$(pwd)}/.agentshore/context.json" 2>/dev/null); BASE=${BASE:-$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')}; BASE=${BASE:-main}
gh pr create --base "$BASE" --title "<type>: <summary>" --body "Closes #$ISSUE_NUMBER\n\n## Changes\n- <bullets>\n\n## Test plan\n- <how to verify>"
```

Record the URL. **Verify the base:** `gh pr view --json baseRefName,url,state`; if `baseRefName` is not `$BASE`, fix it immediately with `gh pr edit <N> --base "$BASE"`. Confirm the issue is referenced in the body.

**Forbidden mutations:**
- Never touch `.github/workflows/**`, `.github/actions/**`, `.gitlab-ci.yml`, `.circleci/**`, `azure-pipelines.yml`, `Jenkinsfile`, `bitbucket-pipelines.yml`, or tests asserting their existence. If CI changes are required, post a comment stating CI config is owned by the human maintainer, request `agentshore/disallowed`, then exit with `success: false`, `error: "ci-change requested but forbidden by skill policy"` and the policy-disallowed mutation shape below.
- Never `git worktree add/remove/prune` — AgentShore owns lifecycle.
- Never `git stash`, `git push --force`, or stack branches on an unmerged dependency.
- Never `gh repo fork`, never `git remote add` a non-origin remote, and never open a cross-fork PR (a `gh pr create` whose `--head` points at a fork). If pushing to `origin` is denied, stop and emit `success: false`, `error: "no push access to origin"` — do not work around it by forking.

**Report — one fenced JSON block, nothing else:**

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
  "learnings": [{"pattern": "pytest requires PYTHONPATH=src to discover the package", "confidence": 0.9, "category": "test-setup"}],
  "error": null
}
```

`status`: `DONE` | `DONE_WITH_CONCERNS` | `NEEDS_CONTEXT` | `BLOCKED`. `DONE_WITH_CONCERNS` when the change partially advances the issue. Irrecoverable failure → `success: false`.

Optionally include 0–3 `learnings` entries capturing ONLY durable, repo-specific patterns worth reusing in future plays (build/test quirks, conventions, gotchas) — grounded in what actually happened this run, not generic advice. Each entry: `pattern` (the insight), `confidence` 0.0–1.0 (default 0.5), `category` short tag (default `"general"`). Omit the field entirely if nothing reusable was learned. NEVER record secrets, tokens, or one-off details. NEVER record workarounds that violate the Forbidden-mutations rules (e.g. forking).

For BLOCKED or policy-disallowed cases, populate `requested_mutations` so AgentShore applies a durable gate:

- **Hard-dependency gate** (blocked by an unmerged prerequisite #N) — emit `block_issue_on` naming the blocker. AgentShore adds a real beads `blocks` edge (or, when no bead mirror exists, an `agentshore/blocked` label that `groom_backlog` clears once #N lands), so this issue leaves the `issue_pickup` pool until the blocker resolves and re-arms automatically:

```json
{"requested_mutations": [{"type": "block_issue_on", "issue": 17, "blocker": 12}]}
```

- **Policy-disallowed** (work is out of autonomous scope, terminal) — label it:

```json
{"requested_mutations": [{"type": "label_issue", "issue": 17, "labels": ["agentshore/disallowed"]}]}
```

Always emit the result block — skipping causes `no valid result block` and the work is recorded as failed. Do not end your turn to wait for a build, test run, package-manager lock, CI, or any "notification"/"wake-up": run commands to completion in this turn (kill anything too slow and report what you have), then emit the block. There is no callback — waiting silently gets you killed mid-wait with no credit, even if you opened a PR. **Do not use `manage_task`, do not background or defer any command, and do not pause your turn to "wait for a background/async task to finish" — run every Bash command synchronously and wait for it to finish in this same turn before proceeding.**
