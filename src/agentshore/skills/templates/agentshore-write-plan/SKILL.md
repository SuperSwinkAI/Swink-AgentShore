---
name: agentshore-write-plan
description: "Action slot 2 — Write Implementation Plan. Converts an issue into a concrete, task-level plan before implementation. Produces issue comments and labels that issue pickup can consume."
argument-hint: [issue_number]
disable-model-invocation: true
allowed-tools: Read, Bash(gh:*, git:*, rg:*, sed:*, ls:*, find:*)
---

# agentshore-write-plan

Convert an issue into a concrete, task-level plan. `$ARGUMENTS` is an optional issue number; if empty, pick one. **Synthesize from existing artifacts** — no user to interview. Plan from the issue body, comments, `$AGENTSHORE_PROJECT_PATH/.agentshore/context.json`, the codebase, and project conventions. Do not invent acceptance criteria the issue does not already imply. If the issue is too ambiguous to plan from existing context, that is the result — exit with `success: false`, `error: "issue too ambiguous to plan from existing context"`, and leave a comment naming the specific gap.

**Project docs are authoritative.** Read `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, and `docs/design/HLD.md` (when present) to discover conventions, layering, and TDD expectations. Default to TDD sequencing in every implementation task when project docs are silent.

**Select an issue.** If `$ARGUMENTS` is set, use it. Otherwise list open issues, exclude any covered by an open PR (`gh pr list --state open --json number,title,headRefName,body`) or carrying `agentshore/blocked`, `agentshore/disallowed`, `agentshore/needs-refinement`, `agentshore/planned`, or `agentshore/has-plan`. Pick by priority label, then smallest size label, then lowest issue number.

**Understand the issue and codebase.** `gh issue view $ISSUE_NUMBER --json number,title,body,labels,comments`. Use `rg` for relevant symbols/files/tests, read the files implementation will touch and nearby tests. Identify acceptance criteria, likely files, risks, and validation commands.

**Early exit: already satisfied.** Run the issue's validation/acceptance checks against the current branch. If criteria are fully met (bug already fixed, feature already exists, test already passes), close and exit — do **not** write a plan:

```
gh issue close $ISSUE_NUMBER --comment "Acceptance criteria already satisfied on the default branch. No implementation plan needed."
```

Then emit the result block with `issue_picked_up: <N>`, `verification_evidence` containing the proof command, and `error: null`. Continue only if not satisfied.

**Prerequisite issues (optional).** While reading the codebase, for each **genuine blocking dependency** you find with no existing GH issue, dedup first: `gh issue list --state open --search "<2-3 keywords>" --json number,title --limit 5`. If a match exists, reference it in the plan. Otherwise create one with `gh issue create --label "enhancement"` describing why it blocks #$ISSUE_NUMBER. Record numbers in `issues_created`. Only blockers — no follow-ups or nice-to-haves.

**Plan structure (the executor contract — `agentshore-issue-pickup` parses this).** Post the plan as an issue comment starting with the `AGENTSHORE_IMPLEMENTATION_PLAN` header:

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

Rules: no placeholders (TBD/TODO/"add tests"/"handle edge cases"); exact file paths and exact commands; each task small enough for one issue-pickup pass; each `### Task N` describes an **end-to-end vertical slice** (schema→API→UI→tests), not a horizontal layer — horizontal layering forces `refine_tasks` to re-derive slicing. Do not edit code in this play.

**Publish and route.** Add the plan comment. Apply labels:

```
gh issue edit $ISSUE_NUMBER --add-label "agentshore/planned"
```

Judge plan cohesion: if it covers multiple distinct deliverables that should be sliced into separate issues before pickup, also apply `agentshore/needs-refinement` so `refine_tasks` clones the plan into one child issue per slice before any agent picks it up. A plan that is one cohesive deliverable ships as a single issue. Do **not** open a branch or PR.

**Validate.** Re-read the posted comment and confirm it contains `AGENTSHORE_IMPLEMENTATION_PLAN`, acceptance criteria, likely files, the task checklist, and validation commands. Confirm `agentshore/planned` (plus `agentshore/needs-refinement` when the plan spans multiple distinct deliverables) is on the issue.

**Forbidden:** Plans must not propose creating/editing/restoring/deleting `.github/workflows/**`, `.github/actions/**`, `.gitlab-ci.yml`, `.circleci/**`, `azure-pipelines.yml`, `Jenkinsfile`, `bitbucket-pipelines.yml`, or tests asserting their existence. If the issue calls for any of these, exclude that work from the plan, leave a comment noting CI configuration is owned by the human maintainer, and plan only the non-CI portions. If the issue is entirely about CI, exit with `success: false`, request `agentshore/disallowed`, `error: "ci-change requested but forbidden by skill policy"`.

**Report — one fenced JSON block:**

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

If no eligible issue exists: `success: false`, `error: "no eligible issue available"`. Never omit the result block.
