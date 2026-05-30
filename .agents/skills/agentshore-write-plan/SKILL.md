---
name: agentshore-write-plan
description: "Action slot 2 — Write Implementation Plan. Converts an issue into a concrete, task-level plan before implementation. Produces issue comments and labels that issue pickup can consume."
argument-hint: [issue_number]
disable-model-invocation: true
allowed-tools: Read, Bash(gh:*, git:*, rg:*, sed:*, ls:*, find:*)
---

# agentshore-write-plan

You are a AgentShore skill agent. AgentShore is a pure RL scheduler, not an LLM.
It invoked you with parameters in `$ARGUMENTS`.

## Forbidden mutations

Plans you write must never propose creating, editing, restoring, or deleting any of the
following:

- `.github/workflows/**` — CI workflow definitions
- `.github/actions/**` — custom actions
- Other CI configs: `.gitlab-ci.yml`, `.circleci/**`, `azure-pipelines.yml`, `Jenkinsfile`, `bitbucket-pipelines.yml`
- Tests that assert the existence, contents, or absence of any file above

If the issue calls for changes to any of the above, exclude that work from the plan, leave a
comment on the issue noting CI configuration is owned by the human maintainer and out of scope
for AgentShore skills, and only plan the non-CI portions. If the issue is entirely about CI, exit
with `"success": false`, request `agentshore/disallowed`, and set `"error": "ci-change requested but forbidden by skill policy"`.

## Inputs

- `$ARGUMENTS` - optional GitHub issue number. If empty, select the highest-value open issue
  that is not blocked, not already covered by an open PR, and not labeled `agentshore/planned`
  or `agentshore/has-plan`.

## Step 1 - Select an issue

1. Read `.agentshore/context.json` if it exists. Note beads graph context and current open issues.
2. If `$ARGUMENTS` contains an issue number, set `ISSUE_NUMBER` to it.
3. If no issue number was provided, list open issues:
   ```
   gh issue list --state open --limit 200 --json number,title,labels,body
   ```
4. List open PRs and exclude issues already covered by a PR:
   ```
   gh pr list --state open --json number,title,headRefName,body
   ```
5. Exclude issues labeled `agentshore/blocked`, `agentshore/disallowed`,
   `agentshore/needs-refinement`, `agentshore/planned`, or `agentshore/has-plan`.
6. Pick the best issue by priority label, then smallest size label, then lowest issue number.

## Step 2 - Understand the issue and codebase

1. Read project instructions (`AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, or similar).
2. Fetch the full issue:
   ```
   gh issue view $ISSUE_NUMBER --json number,title,body,labels,comments
   ```
3. Search for relevant symbols, files, tests, and existing patterns. Use `rg` first.
4. Read the files you expect implementation to touch and nearby tests.
5. Identify acceptance criteria, likely files, risks, and validation commands.

## Step 2b — Create missing issues for related work (optional)

While reading the codebase in Step 2, if you identify closely-related work that clearly needs
to happen but has no GH issue (e.g., a prerequisite, a follow-on, or a distinct sub-problem
that is separable from the primary issue), create an issue for it:

```
gh issue create --title "<concise title>" \
  --body "<description of why this work is needed and how it relates to #$ISSUE_NUMBER>" \
  --label "enhancement"
```

Only create issues for clearly-scoped, actionable work — not speculative future ideas. Record
new issue numbers in the result `issues_created` field.

## Step 3 - Write a task-level plan

The plan must be immediately actionable by `agentshore-issue-pickup`.

Required structure:

```markdown
AGENTSHORE_IMPLEMENTATION_PLAN

## Goal
<one sentence>

## Acceptance Criteria
- <specific, testable criterion>

## Likely Files
- Modify: `path/to/file.py` - <why>
- Test: `tests/path/test_file.py` - <what to cover>

## Tasks
### Task 1: <name>
- [ ] Write or update a failing test for <behavior>.
- [ ] Run `<exact command>` and confirm the expected failure.
- [ ] Implement the minimal code needed for the test.
- [ ] Run `<exact command>` and confirm it passes.
- [ ] Run broader validation if the touched area warrants it.

## Validation
- `<exact command>` - <what it proves>

## Risks
- <risk and how to check it>
```

Rules:
- No placeholders such as TBD, TODO, "add tests", or "handle edge cases".
- Include exact file paths and exact commands.
- Keep tasks small enough for one issue-pickup pass.
- Prefer TDD sequencing in every implementation task.
- Do not edit code in this play.

## Step 4 - Publish the plan

1. Add or update an issue comment containing the plan. The comment must begin with
   `AGENTSHORE_IMPLEMENTATION_PLAN` so future plays can find it.
2. Add label `agentshore/planned` to the issue:
   ```
   gh issue edit $ISSUE_NUMBER --add-label "agentshore/planned"
   ```
3. Do not open a branch or PR.

## Step 5 - Validate

1. Re-read the posted issue comment and confirm it contains:
   - `AGENTSHORE_IMPLEMENTATION_PLAN`
   - acceptance criteria
   - likely files
   - task checklist
   - validation commands
2. Confirm the issue has label `agentshore/planned`.

## Result

Output a fenced JSON block exactly like this:

```json
{
  "success": true,
  "artifacts": [
    {"type": "issue_plan", "issue": 17, "location": "GitHub issue comment"}
  ],
  "issues_created": [],
  "requested_mutations": [],
  "issue_picked_up": 17,
  "verification_evidence": [
    {"command": "gh issue view 17 --json labels,comments", "exit_code": 0, "summary": "plan comment and agentshore/planned label present"}
  ],
  "error": null
}
```

If no eligible issue exists, set `"success": false` with `"error": "no eligible issue available"`.
Do not omit the result block under any circumstances.
