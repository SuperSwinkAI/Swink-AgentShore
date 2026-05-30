---
name: agentshore-refine-tasks
description: "Action slot 12 — Refine Task Breakdown. Scans open GitHub issues, estimates scope, decomposes oversized issues into linked sub-issues, and re-prioritizes based on dependencies. Use when unrefined issues exist in the backlog."
argument-hint: []
disable-model-invocation: true
allowed-tools: Read, Bash(gh:*, git:*)
---

# agentshore-refine-tasks

You are a AgentShore skill agent. AgentShore is a pure RL scheduler — not an LLM.
It invoked you with no parameters — this skill is self-contained.

## Inputs

None. All data is sourced from GitHub issues and the repository.

## Step 1 — Pre-flight

1. Read `.agentshore/context.json` if it exists. Extract `repo`, `owner`, and any `priority_scheme`.
2. Use the `learnings` field from `.agentshore/context.json` for past decomposition patterns and sizing mistakes.
3. Read project-level configuration files (`CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, or similar) for project structure and conventions.
4. Identify the repository from context or by running `git remote get-url origin`.
5. Fetch latest remote refs: `git fetch origin`.

## Step 2 — Fetch open issues

1. Run:
   ```
   gh issue list --state open --limit 200 --json number,title,labels,body,assignees
   ```
2. Parse the JSON output into a working list.
3. Process only issues that still carry the `agentshore/needs-refinement` gate
   label. Issues without it have already been sized in a previous run.
4. If no issues need refinement, set `"success": true` with `"error": "all issues already refined"` and empty artifacts. Stop.

## Step 3 — Estimate scope for each unrefined issue

For each open issue not yet refined:

1. Read the issue body to understand the task.
2. Search the codebase to estimate files likely touched:
   - Use `git grep` or file listing to find relevant files mentioned in or implied by the issue.
3. Estimate complexity:
   - **Files touched**: count of files that would need changes.
   - **Estimated time**: S (<15 min, 1-2 files), M (15-30 min, 2-3 files), L (30-60 min, 3-5 files), XL (>60 min, >5 files).
4. Record the estimate for each issue.

## Step 4 — Decompose oversized issues

For every issue estimated as L or XL (>3 files or >30 min):

### Duplicate check (before creating)

1. Search for existing sub-issues:
   ```
   gh issue list --search "Parent: #<parent_number>" --json number,title,state
   ```
2. If sub-issues already exist for this parent, skip decomposition and ensure the
   gate label is removed (Step 4 "Mark refinement complete" below).

### Decomposition

1. Break the issue into 2-5 sub-tasks, each sized S or M.
2. Each sub-task should be independently implementable and testable.
3. Each sub-issue body must be task-ready. Include:
   - `Parent: #<parent_number>`
   - acceptance criteria as checkboxes
   - likely files or areas to inspect
   - likely tests to add or update
   - exact validation commands when they can be inferred
   - dependencies or blockers
4. Create each sub-issue. Sub-issues are already scoped, so they do **not**
   carry `agentshore/needs-refinement`:
   ```
   gh issue create \
     --title "<parent title>: <sub-task description>" \
     --body "Parent: #<parent_number>

## Acceptance Criteria
- [ ] <criterion>

## Likely Files
- <path or area>

## Tests
- <test path or scenario>

## Validation
- <command>" \
     --label "agentshore/intake"
   ```
5. Add a tracking checklist to the parent issue body listing all sub-issues. Fetch the existing body first so the edit appends rather than clobbering:
   ```
   ORIGINAL=$(gh issue view <parent_number> --json body --jq .body)
   gh issue edit <parent_number> --body "$ORIGINAL

## Sub-tasks
- [ ] #<child1>
- [ ] #<child2>"
   ```
6. Record all created sub-issue numbers.

### Mark refinement complete

For every issue processed in Step 3 (whether decomposed in Step 4 or kept as a
sized S/M leaf), clear the gate label:

```
gh issue edit <number> --remove-label "agentshore/needs-refinement"
```

After this step, sized leaves carry just their `priority/*` and `size/*`
labels, making them eligible for `agentshore-issue-pickup`.

## Step 5 — Re-prioritize based on dependencies

1. For each issue (including new sub-issues), identify dependencies:
   - Explicit: references to other issues in the body (`depends on #N`, `blocked by #N`).
   - Implicit: sub-issues should be completed before their parent is closeable.
2. Issues with no blockers and small size get higher priority.
3. Apply priority labels where missing:
   ```
   gh issue edit <number> --add-label "priority/<level>"
   ```
   Levels: `critical`, `high`, `medium`, `low`.

## Step 6 — Validate

1. Confirm every processed issue no longer carries `agentshore/needs-refinement`.
2. Confirm decomposed parents link to their children.
3. Confirm every new sub-issue has the `agentshore/intake` label, references its
   parent, and does **not** carry `agentshore/needs-refinement`.
4. Confirm no duplicate sub-issues were created.

## Result

Output a fenced JSON block exactly like this:

```json
{
  "success": true,
  "artifacts": [
    {
      "issue": 10,
      "title": "Implement auth flow",
      "estimate": "XL",
      "files_touched": 7,
      "action": "decomposed",
      "children": [11, 12, 13, 14]
    },
    {
      "issue": 20,
      "title": "Fix login button",
      "estimate": "S",
      "files_touched": 1,
      "action": "kept"
    }
  ],
  "issues_created": [
    {"number": 11, "title": "Implement auth flow: token generation", "url": "https://github.com/owner/repo/issues/11"},
    {"number": 12, "title": "Implement auth flow: session management", "url": "https://github.com/owner/repo/issues/12"}
  ],
  "error": null
}
```

If all issues were already refined, set `"success": true` with empty artifacts and `"error": "all issues already refined"`.
If any step fails, set `"success": false` and populate `"error"`.
Do not omit the result block under any circumstances.
