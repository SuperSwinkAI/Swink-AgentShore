---
name: agentshore-cleanup
description: "Action slot 13 — Cleanup. Detects the project's toolchain, runs auto-fixers (formatter, lint --fix), re-checks types and tests, opens a PR with any cleanup commits, and files deduplicated issues for unfixable failures. Language-agnostic via manifest detection."
disable-model-invocation: true
allowed-tools: Read, Grep, Glob, Bash(*)
---

# agentshore-cleanup

Trunk-scoped cleanup from `$AGENTSHORE_PROJECT_PATH`. You are on the target branch in the main checkout. `$ARGUMENTS` is an optional target branch name; empty = repo default.

**Project docs are authoritative.** Read `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md` (and any other obvious project docs) to discover the project's lint/format/typecheck/test commands and constraints; apply concrete requirements verbatim and ignore vague advice. Default when silent: detect the manifest (`pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, `Gemfile`, `*.csproj`, `Package.swift`, `composer.json`, `mix.exs`, `CMakeLists.txt`, etc.) and run that ecosystem's canonical fixers, type-checker, and test runner. A repo may match multiple ecosystems — run all that are present.

**Pre-flight:** read `$AGENTSHORE_PROJECT_PATH/.agentshore/context.json` for repo/owner/learnings and project docs for conventions. Resolve `$TARGET`: `$ARGUMENTS` > `.target_branch` from context.json > `origin/HEAD` > `main`. Do not `git stash`, `git fetch`, `git merge`, or `git checkout` until Step 5 (when you create the cleanup branch).

**Auto-fix pass.** Run fixers one at a time (lockfile/cache contention) and only if the tool exists (`command -v`). Record every command in `tools_detected` / `tools_skipped`. After all fixers run, `git diff --stat` decides whether Step 5 commits.

**Re-validate.** Run the project's type-check then test suite **sequentially** (concurrent runs trip the bash background-promotion timeout and the orchestrator kills the play, discarding work). Never pipe test/typecheck output through `tail`/`head`/buffering filters — same background-promotion failure. Use compact flags (`-q --tb=line` pytest, `--short` mypy, `--quiet` cargo test); set bash timeout ≥ 600000 ms on test commands; scope to a fast subset first if the full suite is slow. Capture pass/fail, error counts, and first 10 failure lines per tool.

**Commit and open PR (only if Step 3 produced a diff):** create `chore/cleanup-$(date +%Y%m%d-%H%M%S)`, `git add -u` (never `-A`), commit `chore: automated code-quality cleanup` with a per-tool summary, push. Re-resolve the base **in the same command** as `gh pr create` (a separate shell will have lost `$TARGET`, and `gh` would then default to the repo default branch): `BASE=$(jq -r '.target_branch // empty' "${AGENTSHORE_PROJECT_PATH:-$(pwd)}/.agentshore/context.json" 2>/dev/null); BASE=${BASE:-$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')}; BASE=${BASE:-main}; gh pr create --base "$BASE" --label "agentshore/cleanup"`. Verify with `gh pr view --json baseRefName`; if wrong, `gh pr edit <N> --base "$BASE"`. If the diff was empty, emit `"pr_created": null` and skip.

**Close stale work in touched areas.** Scoped to files this run touched (`git diff --name-only "$TARGET"...HEAD` plus paths from Step 4 failures that now pass). Cap at the 25 oldest matching open issues. For each candidate, re-read the body and re-run the relevant check against the current worktree — if the failure mode is gone, mark stale. Close via `bd close … --reason="cleanup verified fixed: <PR #N or sha>"` and `gh issue close <N> --comment …`; `bd` queries the database at the main project root so run it from `$AGENTSHORE_PROJECT_PATH`. Close children before parents. Record ids in `beads_closed_stale` / `issues_closed_stale`. Verification queries (independent reads) and dedup/issue-file calls (Step 6) may run concurrently; Steps 3→4→5 stay sequential. Snapshot `gh issue list --state open --limit 200` and `gh pr list --state open --limit 50` counts into `open_work_after`.

**File issues for unfixable failures.** For each Step 4 failure, dedup against `gh issue list --search "<summary>" --label "agentshore/cleanup" --state all` before creating. Group failures by root cause (50 mypy errors from one config gap = one issue). Create with label `agentshore/cleanup`, title `Cleanup: <description>`, body containing failure type, evidence (file:line), reproduction command, and branch.

**Forbidden mutations:**
- Never create/edit/restore/delete `.github/workflows/**`, `.github/actions/**`, `.gitlab-ci.yml`, `.circleci/**`, `azure-pipelines.yml`, `Jenkinsfile`, `bitbucket-pipelines.yml`, or tests that assert their existence. If any fixer would touch these, emit `success: false`, `error: "ci-change requested but forbidden by skill policy"`, leave files untouched.
- Never call `git worktree add/remove/prune` — AgentShore owns lifecycle; cleanup runs on trunk deliberately.
- Never `git stash`, `git add -A`, or `git push --force`.
- Never set a bash timeout < 600000 ms on test/typecheck commands.

**Report — one fenced JSON block, nothing else:**

```json
{
  "success": true,
  "artifacts": [
    {"type": "format", "status": "pass", "files_changed": 12},
    {"type": "lint_fix", "status": "pass", "fixes_applied": 47},
    {"type": "typecheck", "status": "pass", "errors": 0},
    {"type": "test", "status": "pass", "passed": 124, "failed": 0}
  ],
  "tools_detected": ["ruff", "mypy", "pytest"],
  "tools_skipped": ["typescript", "rust"],
  "pr_created": 215,
  "issues_created": [],
  "issues_existing": [],
  "issues_closed_stale": [],
  "beads_closed_stale": [],
  "open_work_after": {"issues": 0, "prs": 0},
  "branch": "main",
  "error": null
}
```

`success: false` only for catastrophic failures (target branch unresolvable, push refused after retry, forbidden-mutation breach). Type-check / test failures keep `success: true` and populate `issues_created`. Always emit the block — skipping causes `no valid result block` and discards the work.
