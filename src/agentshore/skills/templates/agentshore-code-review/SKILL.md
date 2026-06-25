---
name: agentshore-code-review
description: "Action slot 5 — Code Review. Reviews a PR for correctness, safety, test coverage, and code quality, then submits a structured GH review. Creates follow-up issues for non-blocking findings."
argument-hint: [pr_number]
disable-model-invocation: true
allowed-tools: Read, Bash(gh:*, git:*)
---

# agentshore-code-review

Review PR #$ARGUMENTS. AgentShore has placed you in a worktree pinned to the PR's branch — work directly here.

**Project docs are authoritative.** Read `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, and `$AGENTSHORE_PROJECT_PATH/.agentshore/context.json` to discover coding conventions, review structure, and recorded learnings. Default to a structured GitHub review with file:line + finding + suggested-fix when project docs are silent. Apply concrete project requirements; ignore vague advice.

**Anti-confirmation rule (hard):** Reviewer GitHub identity (`gh api user --jq .login`) must differ from the PR author. If they match, exit with `success: false`, `error: "reviewer matches author"`.

**Pre-flight:** `gh pr view $ARGUMENTS --json number,title,body,baseRefName,headRefName,headRefOid,author,labels,isDraft,reviewDecision,statusCheckRollup,additions,deletions,changedFiles`. Skip immediately (`verdict: SKIP`, `success: true`) on: draft PR; label `do-not-merge`/`needs-human-review`; an open changes-requested review; `changedFiles == 0`; or an existing `AGENTSHORE_CODE_REVIEW` comment at the current `headRefOid` (parse its `status:` and `blocking_findings:` into `prior_verdict`/`prior_findings_count`, omit if unparseable). Record the head SHA. Note the linked issue (`Closes #N`, `Fixes #N`).

**Hard safety checks (auto-BLOCK on any hit):** diff > 2500 lines; credentials/keys/`.env`/`BEGIN PRIVATE KEY`; dangerous patterns (`unsafe` Rust; `eval`/`exec`/`__import__` Python; `dangerouslySetInnerHTML`/`eval` JS/TS; SQL string concat; `--no-verify`, `verify=False`); unfamiliar/unvetted new dependencies.

**Diff intake:** `gh pr diff $ARGUMENTS` and `gh pr diff $ARGUMENTS --name-only`. Read every changed source file in full context — callers, surrounding patterns, test coverage. Files with >200 changed lines get extra scrutiny. For new deps: pins, vulnerabilities, license.

**Spec compliance (gate):** Verify the diff against the linked issue, not the PR description. Missing requirements, scope drift, or misread spec → blocking.

**Architectural drift:** If `docs/design/HLD.md` or `docs/design/**` exists, flag user-visible contradictions only. Format: `contradicts docs/design/HLD.md §X — but worth reopening because <evidence>`. Stylistic drift is not blocking. Skip the step if no design dir.

**Code quality findings** (file + line range + reason):
- **Logic / Spec:** code matches the linked issue's required behavior.
- **Safety:** happy-path bias (missing null/empty/error paths), edge cases, async/sync friction (blocking calls in `async`).
- **Tests:** new behavior has tests; tests assert behavior, not mocks; edge cases covered.
- **Architecture:** shadow utilities (reimplements `utils/`/`helpers/`), scope creep beyond the linked issue, property drilling (5+ layers), spaghetti growth (one-off booleans/nullable modes bolted onto unrelated flows where a small abstraction would isolate the case).
- **Performance:** N+1 (DB/API/file in loop), O(n²) where a set/map suffices, lazy cloning, ghost dependencies (heavy lib for stdlib-covered task).
- **Types:** `Any` (Py), `any`/`as` casts (TS), `unsafe` without `// SAFETY:`.
- **Naming:** clear and consistent with codebase conventions.
- **Bloat:** redundant code, meta-comments restating the next line.
- **File size (non-blocking):** if the diff pushes a source file across ~1500 lines, note it as a suggestion and file a follow-up issue proposing a decomposition — never block the PR on size alone.

**Security:** injection, hardcoded secrets, unsafe deserialization, path traversal, access control, unvalidated input, untrusted deps.

**Classify:** Blocking — bugs, regressions, security, hard-check fails, spec fails, missing tests for critical paths. Non-blocking — style, naming, optional perf, doc gaps, follow-up refactors.

**Submit:** verdict is `APPROVE` (zero blocking), `REQUEST_CHANGES` (any blocking), `COMMENT` (non-blocking only, sparingly). `gh pr review $ARGUMENTS --<approve|request-changes|comment> --body "<body>"`. Body must begin with this header (executor parses it):

```
AGENTSHORE_CODE_REVIEW
head_sha: <sha>
status: PASS|BLOCK
spec_compliance: PASS|BLOCK
blocking_findings: <int>
non_blocking_findings: <int>

## Summary
<one line>

## Blocking
<numbered list, or "None">

## Suggestions
<numbered list, or "None">
```

**Labels (best-effort, never block):** PASS → add `agentshore/approved`, remove `blocked`. BLOCK → add `blocked`, remove `agentshore/approved`. `gh pr edit ... 2>/dev/null || true`.

**Follow-up issues (substantive only — no style nits):** Dedup against open issues first: `gh issue list --state open --search "<2-3 keywords>" --json number,title --limit 5`. If a match exists, reference it in the review comment, don't create. Otherwise `gh issue create --title "Follow-up: <desc>" --label "agentshore/review"`. File substantive follow-ups by impact (no fixed numeric cap); if the volume is large, file the highest-impact ones and mention the rest in the review.

**Forbidden:**
- `git worktree add/remove/prune`, `git checkout`/`gh pr checkout` to switch branches, `git pull`/`merge`/`rebase` to advance state (your cwd is already the PR branch; use `gh pr diff`, files on disk, or `git show HEAD:<file>`).
- Creating, editing, restoring, or deleting `.github/workflows/**`, `.github/actions/**`, `.gitlab-ci.yml`, `.circleci/**`, `azure-pipelines.yml`, `Jenkinsfile`, `bitbucket-pipelines.yml`, or tests asserting their existence. If the PR modifies any, mark blocking and exit with `success: false`, `error: "ci-change requested but forbidden by skill policy"`.
- Reviewing a PR you authored (anti-confirmation).

**Report — one fenced JSON block, nothing after it:**

```json
{
  "success": true,
  "artifacts": [{"type": "review", "pr": 42, "verdict": "APPROVE", "head_sha": "abc123"}],
  "issues_created": [],
  "issues_existing": [],
  "findings_count": {"blocking": 0, "non_blocking": 2},
  "spec_compliance": "PASS",
  "learnings": [{"pattern": "all new async functions must use the project's custom @retry_on_rate_limit decorator, not tenacity directly", "confidence": 0.8, "category": "conventions"}],
  "error": null
}
```

Optionally include 0–3 `learnings` entries capturing ONLY durable, repo-specific patterns worth reusing in future reviews or plays (recurring anti-patterns, project conventions discovered from the diff, test-coverage norms) — grounded in what was found this review, not generic advice. Each entry: `pattern` (the insight), `confidence` 0.0–1.0 (default 0.5), `category` short tag (default `"general"`). Omit the field entirely if nothing reusable was found. NEVER record secrets, tokens, or PII from the diff. NEVER record workarounds that violate the Forbidden rules (e.g. forking).

Valid `verdict`: `APPROVE`, `REQUEST_CHANGES`, `COMMENT`, `SKIP`. On skip include `verdict: SKIP` and `error: <reason>`; for zero-diff also include `head_sha`. On irrecoverable failure: `success: false`, `error: <description>`. Never omit the result block.
