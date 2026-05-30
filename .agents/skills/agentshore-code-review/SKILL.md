---
name: agentshore-code-review
description: "Action slot 5 — Code Review. Reviews a PR for correctness, safety, test coverage, and code quality, then submits a structured GH review. Creates follow-up issues for non-blocking findings."
argument-hint: [pr_number]
disable-model-invocation: true
allowed-tools: Read, Bash(gh:*, git:*)
---

# agentshore-code-review

You are a AgentShore skill agent invoked with parameters in `$ARGUMENTS`.

## Inputs

`$ARGUMENTS` — PR number to review (required).

## Step 1 — Pre-flight

1. Read `.agentshore/context.json` — coding standards, review criteria, learnings, known pitfalls.
2. Read project-level config (`CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`) for coding conventions.
3. `gh pr view $ARGUMENTS --json number,title,body,baseRefName,headRefName,headRefOid,author,labels,isDraft,reviewDecision,statusCheckRollup,additions,deletions,changedFiles`
4. **Skip** immediately if: draft PR; label `do-not-merge`/`needs-human-review`; open review requesting changes; or `changedFiles == 0`. For zero-diff, fetch `headRefOid` first, then return:

   ```json
   {"success": true, "verdict": "SKIP", "error": "zero-diff PR; no review needed", "head_sha": "<sha>",
    "artifacts": [{"type": "review", "number": $ARGUMENTS, "head_sha": "<sha>", "verdict": "SKIP"}],
    "findings_count": {"blocking": 0, "non_blocking": 0}, "issues_created": [], "issues_existing": []}
   ```

5. Note linked issue (`Closes #N`, `Fixes #N`). Record head SHA. If a prior
   `AGENTSHORE_CODE_REVIEW` comment exists for this SHA, parse its `status:` and
   `blocking_findings:` lines, then return:

   ```json
   {"success": true, "verdict": "SKIP", "error": "already reviewed at <sha>", "head_sha": "<sha>",
    "artifacts": [{"type": "review", "number": $ARGUMENTS, "head_sha": "<sha>", "verdict": "SKIP"}],
    "prior_verdict": "PASS", "prior_findings_count": {"blocking": 0}, "issues_created": [], "issues_existing": []}
   ```

   Omit `prior_verdict`/`prior_findings_count` if the comment is unparseable.

## Step 2 — Hard safety checks (auto-BLOCK on any failure)

1. **Diff size** > 2500 lines.
2. **Credentials** — API keys, tokens, passwords, `.env` additions, `BEGIN PRIVATE KEY`.
3. **Dangerous patterns** — `unsafe` (Rust); `eval()`/`exec()`/`__import__` (Python); `dangerouslySetInnerHTML`/`eval` (JS/TS); SQL string concatenation; `--no-verify`/`verify=False`.
4. **New dependencies** — unfamiliar or unvetted packages.

## Step 3 — Fetch and analyze the diff

`gh pr diff $ARGUMENTS` and `gh pr diff $ARGUMENTS --name-only`. Categorize changes (source/tests/config/docs/deps). Flag files with >200 changed lines for extra scrutiny.

## Step 4 — Read changed files in full context

For each changed source file, read it in full — understand callers, surrounding patterns, test coverage. For deps: check version pins, vulnerabilities, license.

## Step 5 — Spec compliance (before code quality)

Verify against the actual diff, not the PR description. Flag: missing requirements, extra unrelated work, misinterpretation of the issue. Any spec failure is blocking.

## Step 6 — Code quality review

Flag each finding with file + line range + reason:

1. **Logic** — does the code do what the linked issue requires?
2. **Happy-Path Bias** — missing null-checks, unhandled errors, ignored edge cases, empty-collection boundary conditions.
3. **Naming** — clear and consistent with the codebase.
4. **Shadow Utilities** — reimplements an existing helper; check `utils/`, `helpers/`, and shared modules first.
5. **N+1 Trap** — DB queries, API calls, or file reads inside loops.
6. **Brute-Force Algos** — O(n²) nested loops where a HashMap/set lookup suffices.
7. **Scope** — PR stays within the linked issue's bounds; no unrelated refactors or features.
8. **Logic Bloat** — redundant code, overly-verbose patterns, meta-comments restating the next line.
9. **Type Cowardice** — `Any` (Python) or `any`/`as` casts (TypeScript) bypassing the type system.
10. **Async/Sync Friction** — blocking calls (`time.sleep`, sync I/O) inside `async` functions.
11. **Lazy Cloning** — excessive `.clone()`/`.copy()` calls to avoid thinking about ownership.
12. **Ghost Dependencies** — importing a heavy library for a task covered by stdlib or native ES6+.
13. **Property Drilling** — props passed through 5+ component layers without Context/State.

## Step 7 — Test coverage

New behavior must have tests. Edge cases from Steps 5–6 must be covered. Tests must not be tautological (testing mocks, not behavior).

## Step 8 — Security

Check for: injection, hardcoded secrets, unsafe deserialization, path traversal, improper access control, unvalidated user input, untrusted new dependencies.

## Step 9 — Classify findings

**Blocking** (must fix before merge): bugs, regressions, security, credential exposure, hard-check failures, spec failures, missing tests for critical paths.

**Non-blocking** (should not delay merge): style, naming, optional perf, doc gaps, follow-up refactors.

## Step 10 — Submit the review

Verdict: **APPROVE** (zero blocking), **REQUEST_CHANGES** (any blocking), **COMMENT** (non-blocking only; use sparingly).

```
gh pr review $ARGUMENTS --<approve|request-changes|comment> --body "<review body>"
```

Review body must follow this structure:

```
AGENTSHORE_CODE_REVIEW
head_sha: <sha>
status: PASS|BLOCK
spec_compliance: PASS|BLOCK
blocking_findings: <integer>
non_blocking_findings: <integer>

## Summary
<one-line verdict>

## Blocking
<numbered list, or "None">

## Suggestions
<numbered list, or "None">

For each finding: file, line range, description, suggested fix.
```

## Step 11 — Apply labels (best-effort; never block on failure)

`PASS`: add `agentshore/approved`, remove `blocked`. `BLOCK`: add `blocked`, remove `agentshore/approved`.

```
gh pr edit $ARGUMENTS --add-label agentshore/approved 2>/dev/null || true
```

## Step 12 — Follow-up issues

For each non-blocking improvement: `gh issue create --title "Follow-up: <desc>" --body "Identified during review of PR #$ARGUMENTS.\n\n<details>" --label "agentshore/review"`. Reference in review comment.

## Step 13 — Validate

`gh pr view $ARGUMENTS --json reviews` — confirm review submitted. Confirm follow-up issues created.

## Result

```json
{
  "success": true,
  "artifacts": [{"type": "review", "pr": 42, "verdict": "APPROVE", "head_sha": "abc123"}],
  "issues_created": [{"number": 55, "title": "Follow-up: add input validation", "url": "https://github.com/owner/repo/issues/55"}],
  "findings_count": {"blocking": 0, "non_blocking": 2},
  "spec_compliance": "PASS",
  "hard_checks": {"diff_size": "pass", "credentials": "pass", "dangerous_patterns": "pass", "dependencies": "pass"},
  "error": null
}
```

Valid `verdict` values: `"APPROVE"`, `"REQUEST_CHANGES"`, `"COMMENT"`, `"SKIP"`. On skip: `"success": true`, `"error"` = reason. On irrecoverable failure: `"success": false`, `"error"` = description. Never omit the result block.
