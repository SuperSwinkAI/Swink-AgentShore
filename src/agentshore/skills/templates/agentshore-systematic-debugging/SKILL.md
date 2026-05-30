---
name: agentshore-systematic-debugging
description: "Action slot 8 — Systematic Debugging. Investigates explicit QA/debug failures to identify root cause before another fix attempt."
argument-hint: "[issue_number] [branch]"
disable-model-invocation: true
allowed-tools: Read, Bash(gh:*, git:*, rg:*, sed:*, ls:*, find:*, npm:*, python3:*, pytest:*, cargo:*, make:*, uv:*, bun:*, pnpm:*, yarn:*)
---

# agentshore-systematic-debugging

Investigate the QA/debug failure in `$ARGUMENTS` (issue number and/or branch) and produce remediation guidance — **the feedback loop is the product**: without a fast, deterministic, runnable pass/fail signal, no amount of static analysis finds the bug, so build the loop first, iterate on it, then reproduce against it.

**Project docs are authoritative.** Read `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, and any `docs/` for reproduction commands, test runners, and debug conventions; default to ecosystem norms when silent. Apply concrete requirements; ignore vague advice.

**Construction menu (try in roughly this order):** (1) failing test at the seam that reaches the bug; (2) CLI invocation with fixture input, diff against known-good; (3) curl/HTTP script against a running dev server; (4) headless browser driving the UI; (5) replay a captured trace; (6) throwaway harness with mocked deps. Iterate on the loop itself — **faster** (cache setup, narrow scope), **sharper** (assert on the specific symptom, not "didn't crash"), **more deterministic** (pin time, seed RNG, isolate filesystem). For non-deterministic bugs, goal is **higher reproduction rate** — loop 100×, parallelise, narrow timing windows; a 50% flake is debuggable, 1% is not. If you cannot build a loop, record what you tried in `error`, set `success: false`, stop — do not hypothesis-stack without a loop.

**Select the failure.** Read `$AGENTSHORE_PROJECT_PATH/.agentshore/context.json` if present (use `learnings` for context). If an issue was provided, `gh issue view <n> --json number,title,body,labels,comments` — if it already has `agentshore/root-cause-found`, emit `success: false` with `error: "root cause already found"` and stop. Otherwise list explicit diagnostics: `gh issue list --state open --limit 100 --label agentshore/qa` and the same with `--label agentshore/debug-needed`. Do NOT select generic `agentshore/review`, `bug`, or `type/bug` issues unless they also carry one of those two labels. If none, inspect recent failed play context and the branch in `$ARGUMENTS`. Still no concrete failure → `success: false`, `error: "no failure target available"`, stop.

**Run the loop.** AgentShore has placed you in the appropriate worktree pinned to the target branch's current ref. Work from cwd. Run the narrowest reproduction from the issue, CI output, or codebase conventions — no commits, no push, no PR. Read the complete failure output; record exact errors, files, line numbers, exit codes. If local repro is impossible, record why and gather static evidence from CI, review comments, logs, and code.

**Trace root cause.** Identify the failing boundary (test expectation, application code, config, dependency, environment, CI-only behavior, or reviewer misunderstanding). Find a nearby working example in the same codebase and diff it against the failing path. Trace bad data or control flow backward to its source — do not stop at the symptom. State one hypothesis and the evidence; if evidence disproves it, form a new one. Do not stack guesses.

**Produce guidance — do not implement the fix.** Required diagnosis: root cause; reproduction command and observed output; affected files and likely fix location; smallest safe remediation; tests that should fail before and pass after; risks/uncertainty. If the failure issue exists, post a comment beginning with `AGENTSHORE_ROOT_CAUSE_ANALYSIS` containing the diagnosis; add label `agentshore/root-cause-found` when root cause is clear. If investigation uncovers **distinct additional bugs** (not the current issue, real problems with evidence), open one GH issue per bug via `gh issue create --title ... --body "Discovered during debugging of #<primary>. ..." --label "bug"` and record numbers in `issues_created`. No speculative or hypothetical issues.

**Forbidden:** `git worktree add/remove/prune` (AgentShore owns lifecycle); `git checkout` to switch branches; `git stash`; `git fetch` to advance the branch (debugging must reflect the branch's current state); commits, pushes, or PR creation in this play.

**Report — one fenced JSON block, nothing else:**

```json
{
  "success": true,
  "artifacts": [{"type": "root_cause_analysis", "issue": 91, "root_cause_found": true}],
  "issues_created": [],
  "requested_mutations": [],
  "root_cause": "Parser rejects empty input before applying the default config.",
  "verification_evidence": [
    {"command": "pytest tests/test_parser.py::test_empty_input -v", "exit_code": 1, "summary": "fails with ValueError before default config path"}
  ],
  "error": null
}
```

If root cause is not proven, `success: false` and explain what evidence is missing. Never omit the result block.
