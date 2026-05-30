---
name: agentshore-browser-verify
description: "Action slot 14 — Browser Verification. Navigates to a URL via Playwright, takes a screenshot, and evaluates the page against acceptance criteria from the related issue. Use when AgentShore needs to visually verify a deployed change or UI feature."
argument-hint: [url]
disable-model-invocation: true
allowed-tools: Read, Bash(gh:*), mcp__playwright__*
---

# agentshore-browser-verify

Verify the URL in `$ARGUMENTS` against acceptance criteria from the related issue.

**Playwright conventions:** read `CLAUDE.md`, `AGENTS.md`, and `CONTRIBUTING.md` for browser/test conventions; default to headless Chromium with a screenshot per failure when docs are silent. Apply concrete project requirements; ignore vague advice.

**Pre-flight:** `$ARGUMENTS` must be a non-empty URL starting with `http://` or `https://` — otherwise emit `success: false` with `error: "No URL provided"` or `"Invalid URL scheme"` and stop. AgentShore has placed you in a worktree pinned to the PR's branch; work directly here.

**Load criteria.** Read `$AGENTSHORE_PROJECT_PATH/.agentshore/context.json` for `current_issue`/`related_issue` and inline `acceptance_criteria`. If only an issue number is present, run `gh issue view <n> --json body` and parse sections titled "Acceptance Criteria", "Expected Behavior", "Verification", or checkbox lists. If nothing is found, fall back to a general health check: page loads, no console errors, page not blank.

**Verify.** Navigate via `browser_navigate(url=...)`. On navigation failure (timeout, DNS, HTTP 4xx/5xx) skip to the result with `success: false`. Capture `browser_take_screenshot()`, `browser_snapshot()` (DOM), and `browser_console_messages()`. Evaluate each criterion against the screenshot, DOM, and console; perform required interactions via the matching Playwright tool and re-evaluate. Mark each criterion `PASS`/`FAIL` with a one-line reason.

**Verdict.** All criteria pass → `PASS`. Any fail → `FAIL`. No criteria available and page loaded clean → `PASS`. Close with `browser_close()`.

**Forbidden:** `git worktree add/remove/prune`, `git checkout`, `gh pr checkout`, `git pull/merge/rebase` to switch branches; creating, editing, restoring, or deleting `.github/workflows/**`, `.github/actions/**`, `.gitlab-ci.yml`, `.circleci/**`, `azure-pipelines.yml`, `Jenkinsfile`, `bitbucket-pipelines.yml`.

**Report — one fenced JSON block, nothing else:**

```json
{
  "success": true,
  "artifacts": [{"type": "screenshot", "path": "/tmp/agentshore-verify-<timestamp>.png"}],
  "issues_created": [],
  "verification": {
    "url": "https://example.com/feature",
    "verdict": "PASS",
    "criteria_results": [
      {"criterion": "Login form is visible", "result": "PASS", "detail": "Form found in DOM"},
      {"criterion": "No console errors", "result": "PASS", "detail": "0 errors logged"}
    ],
    "console_errors": [],
    "related_issue": 42
  },
  "error": null
}
```

Navigation/verification failure → `success: false` with `error` populated. Criteria-level failures on a loaded page → keep `success: true` with `verdict: "FAIL"` so AgentShore can retry or escalate. Never omit the result block.
