---
name: agentshore-run-qa
description: "Action slot 7 — Run QA. Detects and runs tests, linter, type checker, and formatter for the project's language(s), audits the full target branch for project-quality risks, and files deduplicated GitHub issues for failures and findings."
argument-hint: [branch]
disable-model-invocation: true
allowed-tools: Read, Grep, Glob, Bash(*)
---

# agentshore-run-qa

Run formatter/linter/type-checker/tests on the target branch, audit the full source for project-quality risks, and file dedup'd GitHub issues for failures and findings. `$ARGUMENTS` is an optional branch; empty = the merged trunk (per-PR validation belongs to `agentshore-code-review`).

**Project docs are authoritative.** Read `$AGENTSHORE_PROJECT_PATH/.agentshore/context.json` (learnings, flakiness notes, past false positives, project idioms) and `CLAUDE.md`/`AGENTS.md`/`CONTRIBUTING.md` for conventions feeding the audits. Resolve `TARGET="$ARGUMENTS"` if set, else `TARGET=$(git symbolic-ref refs/remotes/origin/HEAD | sed 's@refs/remotes/origin/@@')`. AgentShore has placed you in the worktree pinned to that branch — work from cwd; QA reports the branch's actual state, never a synthetically advanced one.

**Detect and run.** Project docs name the canonical commands when present; otherwise detect the project manifest (`pyproject.toml`/`setup.py`, `package.json`+`tsconfig.json`, `Cargo.toml`, `go.mod`, `Gemfile`, `pom.xml`/`build.gradle*`, `*.csproj`/`*.sln`, `Package.swift`, `CMakeLists.txt`/`Makefile`, `composer.json`, `mix.exs`) and use that ecosystem's canonical lint/typecheck/test/build commands through its package manager — never bypass it. A repo may match multiple ecosystems; run all. Categories with no declared command and no manifest signal go in `tools_skipped`.

Also snapshot project shape from manifests, lockfiles, version files, CI config, Dockerfiles, and docs: languages, frameworks, runtimes, package managers, test/lint/type/format/security tooling, CI systems.

**Sequential validation** (one tool at a time — concurrent runs compete for lockfiles, caches, ports):

| Step | Tool | Extract |
|---|---|---|
| 3a | formatter | pass / fail |
| 3b | linter | violations (file, line, rule, message), grouped by root cause |
| 3c | type checker | errors (file, line, message), grouped by root cause |
| 3d | tests | run / passed / failed (name + file:line + error) / skipped |

**Audit scope (3e–3g):** the **full target branch**, not just changed files. Candidate list from `git ls-files`, excluding only `vendor/`, `node_modules/`, `target/`, `build/`, `dist/`, `.venv/`, generated dirs, lockfiles, snapshots, minified JS/CSS, images/fonts/archives, compiled binaries. If too large for one run, prioritize highest-risk categories first, record skipped count, and still cover every section. Cluster hits by root cause (one config gap producing 50 hits = one finding). Drop categories where every hit is in `legacy/`/`deprecated/`/`old_*` paths.

### 3e–3g — Slop & quality audit (your judgment, with evidence)

Cover the known categories every run, but only report concrete problems with
file/line evidence, impact, severity, and a specific remediation. Cluster by
root cause; avoid speculative style preferences.

Categories: placeholders/stubs, security, inefficient patterns, error handling,
type escape hatches, duplication/shape, module size/layering, project/dependency
health, tests, CI/CD correctness, and documentation accuracy.

Drop categories where every hit lives in `legacy/`/`deprecated/`/`old_*` paths.

File substantive issues by impact — dedup first (below) so one root cause is one issue, no fixed numeric cap. Prioritize security, correctness, CI breakage, and missing critical-path coverage over style. If the volume is large, file the highest-impact findings and report the count you held back.

**File issues.** Dedup first: `gh issue list --search "<summary>" --label "<label>" --state all --json number,title,state`. Skip if an open issue covers the same root problem. Re-create against a closed issue only on regression or pattern reappearance. Use literal newlines (not `\n` escapes) in bodies.

- Tool failures: title `QA: <description>`, label `agentshore/qa`. Body: `## Failure`, `## Evidence` (file:line), `## Reproduction`, `## Branch`.
- Audit findings (3e/3f/3g): title `Slop: <category> in <area>`, label `agentshore/slop`. Body: `## Category`, `## Severity`, `## Evidence`, `## Impact`, `## Human fix`, `## Branch`.

**Forbidden:**
- Creating, editing, restoring, or deleting `.github/workflows/**`, `.github/actions/**`, `.gitlab-ci.yml`, `.circleci/**`, `azure-pipelines.yml`, `Jenkinsfile`, `bitbucket-pipelines.yml`, or tests asserting their existence. File issues for CI failures; never auto-fix CI configs.
- `git worktree add/remove/prune` (AgentShore owns worktree lifecycle).
- `git stash`, `git checkout` to switch branches, `git fetch`/`merge` to advance the branch under audit.

**Report — one fenced JSON block, nothing after:**

```json
{
  "success": true,
  "artifacts": [
    {"type": "format", "status": "pass"},
    {"type": "lint", "status": "pass", "violations": 0},
    {"type": "typecheck", "status": "fail", "errors": 3},
    {"type": "test", "status": "pass", "passed": 42, "failed": 0, "skipped": 3},
    {"type": "project_quality_audit", "status": "fail", "findings": 5}
  ],
  "issues_created": [{"number": 101, "title": "QA: mypy errors", "url": "..."}],
  "issues_existing": [88],
  "tools_detected": ["pytest", "ruff", "mypy", "ruff-format"],
  "tools_skipped": [],
  "slop_audit": {"files_scanned": 47, "mechanical_hits": 12, "shape_findings": 4},
  "quality_audit": {
    "sections_completed": ["project_detection", "code_quality", "architecture", "error_handling", "tests", "ci", "docs", "security", "standards"],
    "sections_skipped": [],
    "findings": 5,
    "suppressed_findings": 0
  },
  "branch": "main",
  "error": null
}
```

Audit findings (3e–3g) are advisory — file issues but keep `success: true`. Set `success: false` only on catastrophic step failure. Always emit the result block.
