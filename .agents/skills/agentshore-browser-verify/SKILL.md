---
name: agentshore-browser-verify
description: "Action slot 14 — Browser Verification. Navigates to a URL via Playwright, takes a screenshot, and evaluates the page against acceptance criteria from the related issue. Use when AgentShore needs to visually verify a deployed change or UI feature."
argument-hint: [url]
disable-model-invocation: true
allowed-tools: Read, Bash(gh:*), mcp__playwright__*
---

# agentshore-browser-verify

You are a AgentShore skill agent. AgentShore is a pure RL scheduler — not an LLM.
It invoked you with parameters in `$ARGUMENTS`.

## Inputs

- `$ARGUMENTS` — the URL to verify. Required.

## Step 1 — Validate arguments

1. Read `$ARGUMENTS`. Extract the URL.
2. If no URL is provided, set `"success": false` with error `"No URL provided"`. Stop.
3. Validate the URL starts with `http://` or `https://`. If not, set `"success": false`
   with error `"Invalid URL scheme"`. Stop.

## Step 2 — Load acceptance criteria

1. Read `.agentshore/context.json`. Look for:
   - `current_issue` or `related_issue` — the GH issue number this verification is for.
   - `acceptance_criteria` — a list of expected behaviors to check.
2. If `current_issue` is available but `acceptance_criteria` is not inline, fetch it:
   ```
   gh issue view <number> --json body
   ```
   Parse the issue body for acceptance criteria (look for sections titled
   "Acceptance Criteria", "Expected Behavior", "Verification", or checkbox lists).
3. If no acceptance criteria can be found, proceed with a general health check:
   - Page loads without errors.
   - No console errors.
   - Page is not blank.

## Step 3 — Navigate to URL

1. Use the Playwright MCP tool to navigate:
   ```
   browser_navigate(url="<URL>")
   ```
2. Wait for the page to finish loading.
3. If navigation fails (timeout, DNS error, HTTP 4xx/5xx), record the failure and skip
   to the Result step with `"success": false`.

## Step 4 — Capture page state

1. Take a screenshot:
   ```
   browser_take_screenshot()
   ```
   Record the screenshot path.
2. Get a DOM snapshot for text-based evaluation:
   ```
   browser_snapshot()
   ```
3. Check browser console messages:
   ```
   browser_console_messages()
   ```
   Record any errors or warnings.

## Step 5 — Evaluate against criteria

For each acceptance criterion from Step 2:

1. Check whether the criterion is satisfied based on:
   - The screenshot (visual layout, presence of elements).
   - The DOM snapshot (text content, element structure).
   - Console messages (no unexpected errors).
2. Mark each criterion as `PASS` or `FAIL` with a brief explanation.
3. If any criterion involves interaction (click, form fill), perform the interaction
   using the appropriate Playwright tool, then re-evaluate.

## Step 6 — Determine overall result

1. If ALL criteria pass: `verdict = "PASS"`.
2. If ANY criterion fails: `verdict = "FAIL"`.
3. If no criteria were available and the page loaded without errors: `verdict = "PASS"`.
4. Compile the list of failures with descriptions of what went wrong.

## Step 7 — Clean up

1. Close the browser:
   ```
   browser_close()
   ```

## Result

Output a fenced JSON block exactly like this:

```json
{
  "success": true,
  "artifacts": [
    {"type": "screenshot", "path": "/tmp/agentshore-verify-<timestamp>.png"}
  ],
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

If navigation or verification fails, set `"success": false` and populate `"error"`.
If individual criteria fail but the page loaded, keep `"success": true` with `"verdict": "FAIL"`
so AgentShore can decide whether to retry or escalate.
Do not omit the result block under any circumstances.
