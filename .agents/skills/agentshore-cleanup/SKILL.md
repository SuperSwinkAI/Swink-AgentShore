---
name: agentshore-cleanup
description: "Action slot 13 — Cleanup. Detects the project's toolchain, runs auto-fixers (formatter, lint --fix), re-checks types and tests, opens a PR with any cleanup commits, and files deduplicated issues for unfixable failures. Language-agnostic via manifest detection."
disable-model-invocation: true
allowed-tools: Read, Grep, Glob, Bash(*)
---

# agentshore-cleanup

You are a AgentShore skill agent invoked with parameters in `$ARGUMENTS`.

## Forbidden mutations

Never create, edit, restore, or delete: `.github/workflows/**`, `.github/actions/**`, `.gitlab-ci.yml`, `.circleci/**`, `azure-pipelines.yml`, `Jenkinsfile`, `bitbucket-pipelines.yml`, or tests that assert their existence. If any auto-fixer would touch a forbidden path, exit with `"success": false` and `"error": "ci-change requested but forbidden by skill policy"` and leave all files untouched.

## Inputs

`$ARGUMENTS` — optional target branch name. Empty = default branch (typically `main`).

## Step 1 — Pre-flight

Run cleanup in an isolated worktree pinned to the target branch. Never `git stash`, never `git checkout` in the main worktree, never `git fetch` or `git merge` to advance the branch.

1. Read `.agentshore/context.json` — repo, owner, learnings.
2. Read project-level config (`CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`) — conventions.
3. Record the main project directory: `MAIN_REPO=$(pwd)`.
4. Resolve the target branch: if `$ARGUMENTS` is set, `TARGET="$ARGUMENTS"`; otherwise `TARGET=$(git symbolic-ref refs/remotes/origin/HEAD | sed 's@refs/remotes/origin/@@')` (default `main`).
5. Compute the worktree path: `CLEANUP_WORKTREE="$MAIN_REPO/.agentshore/worktrees/cleanup-$TARGET"`. Remove any stale worktree: `git worktree remove --force "$CLEANUP_WORKTREE" 2>/dev/null || rm -rf "$CLEANUP_WORKTREE"`.
6. Create the worktree and switch into it:
   ```
   git worktree add --detach "$CLEANUP_WORKTREE" "$TARGET"
   cd "$CLEANUP_WORKTREE"
   ```
   Stop with `success: false` if `$TARGET` does not resolve locally.

## Step 2 — Detect toolchain

Project docs are authoritative. Fall back to manifest inspection. Record `tools_detected` and `tools_skipped`. A repo may match multiple ecosystems; run all that are present.

| Ecosystem | Manifest | Manager |
|---|---|---|
| Python | `pyproject.toml`, `setup.py` | `uv`, `pip` |
| JS/TS | `package.json` (+`tsconfig.json`) | `npm`/`npx` |
| Rust | `Cargo.toml` | `cargo` |
| Go | `go.mod` | `go` |
| Ruby | `Gemfile` | `bundle` |
| Java/Kotlin | `pom.xml`, `build.gradle*` | `./mvnw`, `./gradlew` |
| C# | `*.csproj`, `*.sln` | `dotnet` |
| Swift | `Package.swift` | `swift` |
| C/C++ | `CMakeLists.txt`, `Makefile` | `cmake`/`make` |
| PHP | `composer.json` | `vendor/bin/...` |
| Elixir | `mix.exs` | `mix` |

## Step 3 — Auto-fix pass

Run one tool at a time to avoid lockfile / cache contention. Only invoke tools that exist (`command -v <tool>` or `which <tool>`). Record every command executed.

**Python:**
- `command -v ruff` → `uv run ruff check --fix . && uv run ruff format .` (or `ruff` directly if uv absent)
- If ruff absent: `command -v black` → `black .`; `command -v isort` → `isort .`

**JS/TS:**
- If `package.json` has a `lint` script → `npm run lint -- --fix` (attempt; skip if it exits non-zero due to missing `--fix` support)
- `command -v prettier` or `prettier` in devDependencies → `npx prettier --write .`
- `command -v eslint` → `npx eslint --fix .`

**Rust:**
- `cargo fmt`
- `command -v cargo-clippy` → `cargo clippy --fix --allow-dirty --allow-staged 2>&1`

**Go:**
- `gofmt -w .`
- `command -v golangci-lint` → `golangci-lint run --fix 2>&1`

**Ruby:**
- `bundle exec rubocop -A 2>&1` (only if Gemfile contains `rubocop`)

**Other ecosystems:** record `auto_fix: skipped` — no language-agnostic auto-fixer available.

After all fixers run, check `git diff --stat` inside the worktree to determine whether any files were modified.

## Step 4 — Re-validate (report-only, no auto-fix)

Run the type-checker and test suite for each detected ecosystem. These gates determine `success` but never mutate files.

| Ecosystem | Type check | Test suite |
|---|---|---|
| Python | `uv run mypy src/` (or `mypy src/`) | `uv run pytest` (or `pytest`) |
| JS/TS | `npm run typecheck` or `npx tsc --noEmit` | `npm test` |
| Rust | `cargo check` | `cargo test` |
| Go | `go vet ./...` | `go test ./...` |
| Ruby | *(none)* | `bundle exec rspec` or `bundle exec rake test` |
| Other | *(skip)* | *(skip)* |

Capture pass/fail, error counts, and first 10 lines of failure output for each tool.

## Step 5 — Commit and open PR

If Step 3 produced a non-empty diff:

1. Create branch `chore/cleanup-$(date +%Y%m%d-%H%M%S)` from the target branch ref:
   ```
   git checkout -b chore/cleanup-$(date +%Y%m%d-%H%M%S)
   ```
2. Stage modified files only (never `git add -A` — do not stage untracked files):
   ```
   git add -u
   ```
3. Commit:
   ```
   git commit -m "chore: automated code-quality cleanup

   Tools applied: <comma-separated list>
   Files changed: <N>
   Fixes applied: <N lint/format fixes>"
   ```
4. Push and open a PR:
   ```
   gh pr create \
     --title "chore: cleanup (<tools>)" \
     --label "agentshore/cleanup" \
     --body "<per-tool diff summary with file counts>"
   ```
   Capture the PR number from the output.

If `git diff --stat` was empty after Step 3, skip this step entirely. Emit `"pr_created": null`.

## Step 6 — File issues for unfixable failures

For each type-check or test failure from Step 4, check for an existing open issue before creating:
```
gh issue list --search "<summary>" --label "agentshore/cleanup" --state all --json number,title,state
```

Skip creation if an open issue already covers the same root problem. Group failures by root cause (e.g., 50 mypy errors from one config gap = one issue). Create using:
```
gh issue create \
  --title "Cleanup: <description>" \
  --label "agentshore/cleanup" \
  --body "<Failure type, evidence (file:line), reproduction command, branch>"
```

## Step 7 — Remove worktree

```
cd "$MAIN_REPO"
git worktree remove --force "$CLEANUP_WORKTREE"
```

## Result

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
  "branch": "main",
  "error": null
}
```

Set `"success": false` only for catastrophic failures: worktree creation failure, push refused after retry, or forbidden-mutation breach. Type-check / test failures keep `"success": true` but populate `"issues_created"`. Always emit the result block.
