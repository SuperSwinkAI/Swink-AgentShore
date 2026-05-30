---
name: agentshore-design-audit
description: "Action slot 9 — Design Audit. Reviews project design files, specs, PRDs, ADRs, and related docs against source/tests/GitHub/beads, then creates or links GitHub issues and beads tasks for every concrete unmet requirement."
argument-hint: []
disable-model-invocation: true
allowed-tools: Read, Bash(bd:*, gh:*, git:*)
---

# agentshore-design-audit

You are a AgentShore skill agent. AgentShore invoked you with parameters in `$ARGUMENTS`.

This play is a focused design/spec gap audit. Do not implement code. Your job is to find concrete
requirements from project design files that are not implemented and not already represented as open
work, then create trackable GitHub issues and beads tasks for those gaps.

## Inputs

- Play context: `.agentshore/context.json` or the play-specific context path named in the prompt.
- Design/spec sources: seed file from context, `docs/PRD.md`, `docs/design/**/*.md`,
  `docs/specs/**/*.md`, `docs/**/*.md`, `ADR*.md`, `AGENTS.md`, `CLAUDE.md`, `README.md`, and other
  repo-local planning docs discovered by `git ls-files`.
- GitHub issues: all open issues plus recently closed issues.
- Beads graph: `bd list --all --json --limit 0`.
- Source/tests/docs evidence from the repository.

## Success Condition

Before returning `"success": true`, every concrete requirement you audited must be one of:

- `verified_done`: source/tests/docs evidence shows the requirement exists.
- `represented_open`: an open GitHub issue and open beads task already track the work.
- `gap_filled`: this play created or linked an open GitHub issue and open beads task.

Closed GitHub issues, closed beads tasks, or PR titles are not proof. If implementation evidence is
missing, treat the requirement as unmet and make open work for it.

If any concrete requirement remains without implementation evidence and without open tracking, return
`"success": false`.

## Workflow

1. Read the play context first and verify the GitHub identity:
   ```
   gh api user --jq .login
   ```
   If context includes `assigned_github_identity`, compare after lowercasing/casefolding both
   strings.
2. Read design/spec files and build a requirement inventory. Each item needs a short title, source
   file/heading, expected behavior, and acceptance criteria.
3. Read relevant source/tests/docs to verify implementation. Prefer concrete evidence paths over
   assumptions.
4. Fetch GitHub issues:
   ```
   gh issue list --state open --json number,title,body,labels --limit 200
   gh issue list --state closed --json number,title,body,labels --limit 100
   ```
5. Read the beads graph:
   ```
   bd list --all --json --limit 0
   ```
6. For each unmet requirement:
   - If an open GitHub issue and open beads task already track it, mark `represented_open`.
   - If only an open GitHub issue exists, create or link a beads task with `external_ref="gh-<N>"`.
   - If no open GitHub issue exists, create one with source references and acceptance criteria.
   - If a related issue is closed but implementation evidence is missing, create a follow-up issue.
7. Link each new or existing gap issue into beads:
   ```
   bd create task "<issue title>" --description "Closes gh-<issue_number>" --external-ref "gh-<issue_number>"
   bd link <task-id> <story-id>
   ```
   Use the best matching existing story. If no story fits, create a concise story and link it to the
   relevant epic. Do not duplicate tasks already linked by `external_ref`.
8. Re-run `bd list --all --json --limit 0` and verify every gap issue has a matching open beads task.

## Issue Requirements

New GitHub issues must include:

- Source file and heading for the requirement.
- Why current evidence is insufficient.
- Acceptance criteria that an implementer can test.
- Any likely source/test areas to inspect.

Use labels conservatively: `enhancement` by default, plus existing project labels only when clearly
appropriate.

## Forbidden Mutations

- Do not edit product code, tests, docs, or CI files.
- Do not close or reopen GitHub issues.
- Do not delete beads nodes.
- Do not create duplicate issues for already-open tracked work.

## Result

Output a fenced JSON block exactly like this:

```json
{
  "success": true,
  "artifacts": [
    {
      "type": "design_audit",
      "requirements_scanned": 18,
      "gaps_found": 3,
      "issues_created": 2,
      "issues_linked": 1,
      "unresolved_gaps": 0,
      "unknown_requirements": 0,
      "gap_issue_numbers": [212, 213, 214],
      "requirement_samples": [
        {"title": "Render preview cancellation", "status": "gap_filled", "issue": 212},
        {"title": "Status command JSON output", "status": "verified_done", "evidence": "src/... + tests/..."}
      ]
    }
  ],
  "issues_created": [212, 213],
  "issues_linked": [214],
  "tasks_linked": ["tlc-abc.1", "tlc-def.2"],
  "error": null
}
```

If a GitHub or beads mutation fails, set `"success": false`, include the failed requirement in
`"error"`, and keep any successfully created issue/task numbers in the result.
