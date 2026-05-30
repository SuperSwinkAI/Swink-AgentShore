---
name: agentshore-run-qa
description: "Action slot 7 — Run QA. Detects and runs tests, linter, type checker, and formatter for the project's language(s), audits recently-changed source for AI-slop patterns across all supported ecosystems, and files deduplicated GitHub issues for failures and findings."
argument-hint: [branch]
disable-model-invocation: true
allowed-tools: Read, Grep, Glob, Bash(*)
---

# agentshore-run-qa

You are a AgentShore skill agent invoked with parameters in `$ARGUMENTS`.

## Forbidden mutations

Never create, edit, restore, or delete: `.github/workflows/**`, `.github/actions/**`, `.gitlab-ci.yml`, `.circleci/**`, `azure-pipelines.yml`, `Jenkinsfile`, `bitbucket-pipelines.yml`, or tests that assert their existence. File issues for CI failures; never auto-fix CI configs.

## Inputs

`$ARGUMENTS` — optional branch name. Empty = default branch (merged trunk, typically `main`). Per-PR validation is `agentshore-code-review`'s responsibility.

## Step 1 — Pre-flight

Run QA in an isolated worktree pinned to the target branch as it exists right now. Never `git stash`, never `git checkout` in the main worktree, never `git fetch` or `git merge` to advance the branch — QA reports on the branch's actual current state, not a synthetically-updated one.

1. Read `.agentshore/context.json` — repo, owner, qa config, slop overrides, learnings (flakiness, past false positives, project idioms for the shape audit).
2. Read project-level config (`CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`) — conventions for the shape audit.
3. Record the main project directory: `MAIN_REPO=$(pwd)`.
4. Resolve the target branch: if `$ARGUMENTS` is set, `TARGET="$ARGUMENTS"`; otherwise `TARGET=$(git symbolic-ref refs/remotes/origin/HEAD | sed 's@refs/remotes/origin/@@')` (default `main`).
5. Compute the worktree path: `QA_WORKTREE="$MAIN_REPO/.agentshore/worktrees/qa-$TARGET"`. Remove any stale worktree from a prior run: `git worktree remove --force "$QA_WORKTREE" 2>/dev/null || rm -rf "$QA_WORKTREE"`.
6. Create the worktree pinned to the target branch's current ref and switch into it — stop with `success: false` if `$TARGET` doesn't resolve locally:
   ```
   git worktree add --detach "$QA_WORKTREE" "$TARGET"
   cd "$QA_WORKTREE"
   ```
   All subsequent steps run inside `$QA_WORKTREE`.

## Step 2 — Detect toolchain

Project docs are authoritative. Fall back to manifest inspection — always run through the project's package manager, never bypass it. Record executed commands in `tools_detected`, skipped categories in `tools_skipped`. A repo may match multiple ecosystems; run all. If a category has no declared command and no manifest signal, mark it skipped.

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

## Step 3 — Run validation sequentially

Run one tool at a time — concurrent runs may compete for lockfiles, caches, or ports.

| Step | Tool | Extract |
|---|---|---|
| 3a | formatter | pass / fail |
| 3b | linter | violations (file, line, rule, message) grouped by file |
| 3c | type checker | type errors (file, line, message) |
| 3d | test suite | run / passed / failed (names + file:line + error) / skipped |

### 3e — Mechanical slop-pattern audit

Deterministic grep pass — no LLM judgment; every hit must be a literal pattern match.

**Scope:** `git log --name-only --pretty=format: -n 50 -- . | sort -u | grep -Ev '(^$|/(tests?|__tests__|spec|fixtures|generated|vendor|node_modules|target|build|dist|\.venv)/|\.lock$|\.snap$|\.min\.(js|css)$)'`. Cap at 200 files. Categories A–C are universal. D–G activate per detected language(s); unrecognized ecosystems get A–C only.

#### A. Placeholder hallucinations (universal)

Comments: `Add logic here`, `TODO: implement`, `robust implementation`, `production-ready` (AI praise debris). Stub bodies: `raise NotImplementedError`/`pass # stub` (Py); `panic("not implemented")`/`todo!()`/`unimplemented!()` outside tests (Go/Rust); `throw new (UnsupportedOperation|NotImplemented)Exception`/`throw new Error("not implemented")` (Java/Kotlin/C#/JS/TS); language equivalents for Swift/Ruby/PHP/Elixir.

#### B. Insecure defaults (universal)

TLS bypass: `verify=False`, `rejectUnauthorized:\s*false`, `InsecureSkipVerify:\s*true`. Hardcoded credentials: `(password|api_key|secret|token)\s*[:=]\s*["'][^"']+["']` (exclude `tests?/`, `*.example`). SQL injection: string concat/interpolation with non-numeric vars in a query string. Shell injection: `os.system(`, `subprocess.*shell=True`, backtick interpolation with user vars.

#### C. Suboptimal patterns (universal)

- **Brute-Force Algos:** nested loops where the inner body is a linear membership test (`in <list>`, `.includes(`, `.contains(`, `.indexOf(.*) !== -1`). O(n²) — flag for HashMap/set refactor.
- **N+1 Trap:** DB queries, API calls, or file reads (`db.query`/`fetch`/`open`/ORM `.find`/`.get`) inside a `for`/`while`/`forEach` loop.
- **Async/Sync Friction:** `time.sleep(` inside `async def`; `Thread.sleep(` in Kotlin `suspend`/Java reactive chains; `std::thread::sleep` in `async fn`; `setTimeout` as sync barrier.

#### D. Error-handling laziness / Happy-Path Bias (language-specific)

Python: bare `except:`/`except Exception:\s*pass`. Rust: `.unwrap()`/`.expect(` outside tests. Go: `if err != nil { return nil }` (swallow without wrap); `_ = <fn>()` discarding fallible call. Java/Kotlin: empty catch; `e.printStackTrace()`. C#: `catch (Exception) { }`/`catch { }`. Ruby: empty `rescue`. PHP: `@` suppression. Swift: `try!` outside tests. JS/TS: `.catch(() => {})`/empty `catch {}`. C/C++: ignored return from `fread`/`write`/`malloc`.

#### E. Lazy Cloning / over-copy (language-specific)

Rust: `.clone()` density > 5/file or > 1/50 LOC; `.to_string()` → `.as_str()`. Python: `copy.deepcopy(` outside serialization/tests; `str(x)` on already-string-typed names. Go: `make([]T, 0)` + full-slice append. Java: `new String(s)`/`new Integer(i)`. C#: `new String(s.ToCharArray())`. JS/TS: `JSON.parse(JSON.stringify(` in hot paths. C++: large types passed by value where `const&` suffices.

#### F. Type Cowardice + Property Drilling (typed languages)

TypeScript: `as any`, `as unknown as`, `// @ts-(ignore|expect-error)` without explanation comment; **Property Drilling:** props passed through 5+ component layers without Context/Store. Java: raw generics; `@SuppressWarnings("unchecked")` without comment. Kotlin: `as Any` unchecked casts. C#: `dynamic` outside interop. Go: `interface{}`/`any` where a concrete type is available. Rust: `unsafe` block without `// SAFETY:` comment. Swift: `as!` outside tests. C/C++: C-style casts; `void*` arithmetic outside allocators.

#### G. Ghost Dependencies (universal)

`import` of a heavy library for a task covered by stdlib or native ES6+: `lodash.get`/`_.get` for optional chaining `?.`; `moment`/`dayjs` for simple `Date` formatting; `requests` when `urllib` suffices; `numpy` for a single `.sum()`; `uuid` package when `crypto.randomUUID()` is available.

Cluster hits by root cause — a single config gap producing 50 hits is one finding. Drop categories where every hit is in clearly-deprecated paths (`legacy/`, `deprecated/`, `old_*`).

### 3f — Code-shape audit

Judgment pass over the same scoped file list. Read each file in full. Only flag with clear evidence (file, line range, why).

- **Logic Bloat / Redundant comments:** comments that restate the next line rather than explaining *why*; overly-verbose patterns with equivalent terse forms. Preserve intent/invariant/constraint comments.
- **Over-abstraction:** factory/builder/generic wrapper for a single concrete usage; thin stdlib wrappers; deep hierarchies with one leaf implementer.
- **Shadow Utilities / Project-idiom drift:** reimplements a helper that already exists in `utils/`/`helpers/` or shared modules; deviates from error handling, logging, config access, or result-wrapping conventions in `CLAUDE.md`/`AGENTS.md`/`CONTRIBUTING.md`.
- **Edge-case gaps:** new/rewritten functions missing: empty-collection, `None`/`null`/`Err` inputs, integer overflow, off-by-one on slice bounds.
- **Duplication-vs-refactor:** near-identical blocks (5+ lines, same control flow) in 2+ files where extracting a shared helper would be straightforward.

Cap at 20 findings across all categories. Prioritize blocking over style.

## Step 4 — File issues

**Deduplication:** `gh issue list --search "<summary>" --label "<label>" --state all --json number,title,state`. Use `agentshore/qa` for tool failures, `agentshore/slop` for 3e/3f findings. Skip creation if an open issue covers the same root problem; re-create against a closed issue only on regression or pattern reappearance.

**Issue creation:** Group by root cause (50 mypy errors from one gap = one issue). Use `gh issue create --title ... --label ... --body "..."` with literal newlines (not `\n` escapes).

- Tool failures: title `QA: <description>`, label `agentshore/qa`. Body: `## Failure`, `## Evidence` (file:line), `## Reproduction`, `## Branch`.
- Slop findings: title `Slop: <category> in <area>`, label `agentshore/slop`. Body: `## Category`, `## Evidence`, `## Why this is slop`, `## Human fix`, `## Branch`.

## Step 5 — Cleanup

Return to the main project directory and remove the QA worktree:

```
cd "$MAIN_REPO"
git worktree remove --force "$QA_WORKTREE"
```

## Step 6 — Validate

Confirm tool runs and slop audit completed (or correctly skipped — `qa.skip_slop_audit: true` in context.json opts out). Confirm filed issues have title, body, and correct label.

## Result

```json
{
  "success": true,
  "artifacts": [
    {"type": "format", "status": "pass"},
    {"type": "lint", "status": "pass", "violations": 0},
    {"type": "typecheck", "status": "fail", "errors": 3},
    {"type": "test", "status": "pass", "passed": 42, "failed": 0, "skipped": 3},
    {"type": "slop_mechanical", "status": "fail", "violations": 12, "categories": ["n1_trap", "placeholder_comment"]},
    {"type": "slop_shape", "status": "fail", "findings": 4, "categories": ["logic_bloat", "edge_case_gap"]}
  ],
  "issues_created": [
    {"number": 101, "title": "QA: mypy errors in src/auth.py", "url": "https://github.com/owner/repo/issues/101", "label": "agentshore/qa"},
    {"number": 102, "title": "Slop: n1_trap in src/agents/manager.py", "url": "https://github.com/owner/repo/issues/102", "label": "agentshore/slop"}
  ],
  "issues_existing": [88],
  "tools_detected": ["pytest", "ruff", "mypy", "ruff-format"],
  "tools_skipped": [],
  "slop_audit": {"files_scanned": 47, "mechanical_hits": 12, "shape_findings": 4},
  "branch": "main",
  "error": null
}
```

Slop findings (3e–3f) are advisory — file issues but keep `success: true`. Set `success: false` only on catastrophic step failure. Always emit the result block.
