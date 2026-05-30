---
name: agentshore-systematic-debugging
description: "Action slot 8 — Systematic Debugging. Investigates explicit QA/debug failures to identify root cause before another fix attempt."
argument-hint: "[issue_number] [branch]"
disable-model-invocation: true
allowed-tools: Read, Bash(gh:*, git:*, rg:*, sed:*, ls:*, find:*, npm:*, python3:*, pytest:*, cargo:*, make:*, uv:*, bun:*, pnpm:*, yarn:*)
---

# agentshore-systematic-debugging

You are a AgentShore skill agent. AgentShore is a pure RL scheduler, not an LLM.
It invoked you with parameters in `$ARGUMENTS`.

## Inputs

- `$ARGUMENTS` - optional issue number and/or branch. If empty, discover the most relevant
  open issue labeled `agentshore/qa` or `agentshore/debug-needed`.

## Step 1 - Select the failure

1. Read `.agentshore/context.json` if present. Use its `learnings` field for compact learning context.
2. If an issue number was provided, fetch it:
   ```
   gh issue view <issue> --json number,title,body,labels,comments
   ```
   If it already has `agentshore/root-cause-found`, output `"success": false` with
   `"error": "root cause already found"` and stop.
3. If no issue was provided, list explicit diagnostic issues:
   ```
   gh issue list --state open --limit 100 --label agentshore/qa --json number,title,body,labels
   gh issue list --state open --limit 100 --label agentshore/debug-needed --json number,title,body,labels
   ```
   Do not select generic `agentshore/review`, `bug`, or `type/bug` issues unless they also carry
   `agentshore/qa` or `agentshore/debug-needed`.
4. If no issue exists, inspect recent failed play context and the target branch from `$ARGUMENTS`.
5. If there is still no concrete failure to investigate, output `"success": false` with
   `"error": "no failure target available"` and stop.

## Step 2 - Reproduce without changing code

If reproduction needs the target branch checked out, use an isolated worktree pinned to its current ref. Never `git checkout` in the main worktree, never `git stash`, never `git fetch` to advance the branch — debugging must reflect the branch's actual current state.

1. Parse the target branch from `$ARGUMENTS` (second token) or, if absent, derive it from the failing PR or issue. If no branch context applies, skip the worktree setup and reproduce against the current working tree without modifying it.
2. If a target branch was identified:
   - Record the main project directory: `MAIN_REPO=$(pwd)`.
   - Compute an isolated worktree path: `DEBUG_WORKTREE="$MAIN_REPO/.agentshore/worktrees/debug-$TARGET"`.
   - Remove any stale worktree: `git worktree remove --force "$DEBUG_WORKTREE" 2>/dev/null || rm -rf "$DEBUG_WORKTREE"`.
   - Create the worktree pinned to the target branch's current local ref and switch into it. Stop with `success: false` if `$TARGET` doesn't resolve locally:
     ```
     git worktree add --detach "$DEBUG_WORKTREE" "$TARGET"
     cd "$DEBUG_WORKTREE"
     ```
3. Run the narrowest reproduction command from the issue, CI output, or codebase conventions. Do not commit, push, or create a PR in this play.
4. Read the complete failure output. Record exact error messages, files, line numbers, and exit codes.
5. If reproduction is not possible locally, record why and gather static evidence from CI, review comments, logs, and code.
6. After reproduction (success or not), if a worktree was created in Step 2.2, return to the main project and remove it:
   ```
   cd "$MAIN_REPO"
   git worktree remove --force "$DEBUG_WORKTREE"
   ```
   Run this cleanup even if reproduction failed — leave no stale worktrees behind.

## Step 3 - Trace root cause

1. Identify the failing boundary: test expectation, application code, config, dependency,
   environment, CI-only behavior, or reviewer misunderstanding.
2. Find a nearby working example in the same codebase and compare it to the failing path.
3. Trace bad data or control flow backward to its source. Do not stop at the symptom.
4. State one root-cause hypothesis and the evidence that supports it.
5. If evidence disproves the hypothesis, form a new one. Do not stack guesses.

## Step 4 - Produce remediation guidance

Do not implement the fix in this play. Produce a concrete diagnosis that a later
`issue_pickup` or `unblock_pr` play can execute.

Required diagnosis:
- root cause
- reproduction command and observed output
- affected files and likely fix location
- smallest safe remediation
- tests that should fail before the fix and pass after
- risks or uncertainty

If the failure issue exists, add a comment beginning with `AGENTSHORE_ROOT_CAUSE_ANALYSIS`
containing the diagnosis. Add label `agentshore/root-cause-found` when root cause is clear.

If investigation uncovers **additional bugs or gaps** that are distinct from the primary failure
— i.e., things that are not the current issue but are real problems — create a separate GH issue
for each:
```
gh issue create --title "<concise bug title>" \
  --body "Discovered during debugging of #<primary_issue>.\n\n<description of the problem and evidence>" \
  --label "bug"
```
Record new issue numbers in `issues_created`. Do not create issues for hypotheticals or
speculative risks — only for clearly observed problems with evidence.

## Result

Output a fenced JSON block exactly like this:

```json
{
  "success": true,
  "artifacts": [
    {"type": "root_cause_analysis", "issue": 91, "root_cause_found": true}
  ],
  "issues_created": [],
  "requested_mutations": [],
  "root_cause": "Parser rejects empty input before applying the default config.",
  "verification_evidence": [
    {"command": "pytest tests/test_parser.py::test_empty_input -v", "exit_code": 1, "summary": "fails with ValueError before default config path"}
  ],
  "error": null
}
```

If root cause is not proven, set `"success": false` and explain what evidence is missing.
Do not omit the result block under any circumstances.
