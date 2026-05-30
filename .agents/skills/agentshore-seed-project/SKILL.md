---
name: agentshore-seed-project
description: "Action slot 17 — Seed Project. Full project audit on session start and after cooldown: reconciles the seed file, PRD/HLD/design docs, beads graph, and GitHub issues into a consistent, complete backlog. Creates or repairs epics, stories, tasks, and GH issues as needed."
argument-hint: []
disable-model-invocation: true
allowed-tools: Read, Bash(bd:*, gh:*, git:*)
---

# agentshore-seed-project

You are a AgentShore skill agent. AgentShore is a pure RL scheduler — not an LLM.
It invoked you with parameters in `$ARGUMENTS`.

This play runs at the start of every session and may run again later after cooldown. Its job is a
**full reconciliation** — not a one-time setup. It must leave the beads graph and GitHub issues in a
complete, consistent state regardless of what prior sessions did or did not do.

## Inputs

- `.agentshore/context.json` — session state including `open_issues`, `repo`, `owner`, and `seed_path`.
- Seed file (path in `context.json → params → seed_path`, or look for `test_ready.md`, `SEED.md`,
  `PLAN.md` in the repo root).
- Design documents: the seed file, `docs/PRD.md`, `docs/design/PRD.md`,
  `docs/design/HLD.md`, any `docs/**/*.md` design files, plus `CLAUDE.md`, `AGENTS.md`,
  `CONTRIBUTING.md` for conventions.
- GitHub issues (all open, plus recently closed — fetch both).
- The existing beads graph (`bd list --all --json --limit 0`, filtered by `type`).

## Reconciliation Invariant (success condition)

Before returning `"success": true`, all of these must hold:

1. Every open GH issue appears in the beads graph as a task with `external_ref="gh-<N>"`.
2. Every open beads task has a non-null `external_ref`.
3. Every story is linked to an epic. Every task is linked to a story.
4. `bd list --all --json --limit 0` runs cleanly and returns at least one epic bead.
5. Every concrete requirement from the seed file and design docs is either:
   - verified as implemented by source/tests/docs evidence, or
   - represented by an open GH issue and open beads task.
6. The result includes an `artifacts` entry with `"type": "seed_audit"`, numeric coverage
   counts, and no unresolved or unknown requirements.

If any condition fails, set `"success": false` and populate `"error"` with the first violation.

## Step 1 — Pre-flight

1. Read `.agentshore/context.json`.
2. Verify `bd` is available: `bd --version`. If not, exit with `"success": false`.
3. Read the seed file and all available design documents. Build a mental model of:
   - What epics/themes the project needs.
   - What stories (coherent sub-goals) fall under each epic.
   - What concrete tasks (individual deliverables) are expected.
4. Fetch GitHub issues:
   ```
   gh issue list --state open --json number,title,body,labels --limit 200
   gh issue list --state closed --json number,title,labels --limit 50
   ```
5. Snapshot the existing beads graph:
   ```
   bd list --all --json --limit 0
   ```
   Filter the returned beads by `type == "epic"`, `type == "story"`, and `type == "task"`.
6. Inspect the repository with `git ls-files` and read relevant source, tests, and docs for each
   requirement. A closed GitHub issue or closed beads task is not proof of completion by itself.

## Step 2 — Audit: desired state vs. actual state

Produce a gap analysis comparing what the seed file + design docs say should exist against what
actually exists in beads and GitHub. Identify:

First build a requirement inventory from the seed/design docs. Each inventory item must have:

- A short requirement title.
- The source document path or heading that introduced it.
- One of these statuses:
  - `verified_done`: source/tests/docs evidence shows it is implemented.
  - `represented_open`: an open GH issue and open beads task track the remaining work.
  - `scope_gap`: no open GH issue and no open beads task track the work.
  - `unknown`: you could not verify implementation and could not confidently map it to open work.

Do not mark a requirement `verified_done` only because a related GH issue is closed. If you cannot
find implementation evidence, treat the requirement as unmet. Every unmet requirement must end this
play with an open GitHub issue and an open linked beads task. Use `unknown` only when the
requirement text is too ambiguous to turn into a concrete issue; ambiguity is a failure, not success.

**A. Missing or incomplete epics** — epics implied by the seed/design that don't exist in beads.

**B. Missing or incomplete stories** — stories implied by the design that have no beads counterpart,
or stories that exist in beads but are not linked to their parent epic.

**C. Unlinked beads tasks** — tasks that exist in the graph but have no `external_ref` (i.e., no
corresponding GH issue). These represent planned work that AgentShore cannot pick up.

**D. Open GH issues not in the graph** — open issues with no matching beads task. These need a task
created and linked so AgentShore can track and close them.

**E. Scope gaps** — work described in the seed/design that is not verified done and has neither an
open GH issue nor an open beads task. These need both a GH issue and a beads task created.

## Step 3 — Repair epics and stories

For each missing epic (gap A):
```
bd create epic "<title>" --description "<one-sentence summary>"
```

For each missing story (gap B):
```
bd create story "<title>" --description "<summary>"
bd link <story-id> <epic-id>
```

For each story that exists but is unlinked to its epic, link it:
```
bd link <story-id> <epic-id>
```

Cap at 5 epics and 10 stories per epic. Use existing epics/stories where they already cover the
intended scope — do not create duplicates.

## Step 4 — Create GH issues for scope gaps and unlinked tasks

For each scope gap (gap E) — work that has no GH issue:
```
gh issue create --title "<concise title>" --body "<what needs to be done and why>" --label "enhancement"
```
Record the returned issue number.
Every unmet seed/PRD/design requirement that is not verified implemented must go through this path
unless it already has an open GH issue. Closed GH issues do not count as open work; create a new
follow-up issue when the requirement still lacks implementation evidence.

For each unlinked beads task (gap C) that represents real planned work with no GH issue:
```
gh issue create --title "<task title>" --body "<description of the work>" --label "enhancement"
```
Record the returned issue number.

Do not create GH issues for work that is already represented by an open GH issue, even if the
beads task is missing — instead, create the beads task and link it (Step 5).

## Step 5 — Create and link beads tasks

For every open GH issue that has no beads task (gaps D and E, after Step 4):
```
bd create task "<issue title>" --description "Closes gh-<issue_number>" --external-ref "gh-<issue_number>"
bd link <task-id> <story-id>
```

For every unlinked beads task (gap C) that now has a GH issue (created in Step 4 or matched to
an existing one):
```
bd set-external-ref <task-id> "gh-<issue_number>"
```
(If `set-external-ref` is not available, update via `bd edit task <task-id> --external-ref "gh-<issue_number>"`.)

Assign each task to the most appropriate story. If no story fits, create one first (Step 3).

Do not create duplicate tasks for issues already in the graph.

## Step 6 — Verify

1. `bd list --all --json --limit 0` — confirm expected epics are present, each with at least one story.
2. From that same all-beads snapshot, confirm every open task has an `external_ref`.
3. `gh issue list --state open --json number,title --limit 200` — confirm every open issue has a
   corresponding beads task.
4. If any gap remains that you could not close (bd error, API failure), record it in `"issues_skipped"`.
5. Recompute the requirement inventory. If any requirement remains `scope_gap` or `unknown`, set
   `"success": false`.
6. If `scope_gaps_found > 0`, `scope_gap_issue_numbers` must include an issue number for every
   scope gap that was created or linked during this play. Missing issue numbers are a failure.

## Forbidden mutations

- Never touch `.github/workflows/**` or any CI configuration files.
- Never close or reopen GitHub issues.
- Never delete existing beads nodes — only create or update.

## Result

Output a fenced JSON block exactly like this:

```json
{
  "success": true,
  "artifacts": [
    {
      "type": "seed_audit",
      "requirements_total": 12,
      "verified_requirements": 8,
      "represented_open_requirements": 4,
      "scope_gaps_found": 4,
      "unresolved_scope_gaps": 0,
      "unknown_requirements": 0,
      "scope_gap_issue_numbers": [12, 13, 14, 15],
      "requirement_samples": [
        {"title": "Capture CLI", "status": "verified_done", "evidence": "src/... + tests/..."},
        {"title": "Render command", "status": "represented_open", "issue": 14}
      ]
    }
  ],
  "epics_created": [
    {"bead_id": "bd-001", "title": "API & Backend", "stories": 3, "tasks": 8}
  ],
  "issues_created": [12, 13, 14, 15],
  "issues_mapped": [1, 2, 3, 5, 7, 12, 13, 14, 15],
  "issues_skipped": [],
  "tasks_linked": ["bd-042", "bd-051"],
  "error": null
}
```

If `bd` is unavailable or any creation step fails unrecoverably, set `"success": false` and populate
`"error"` with a concise description. Do not omit the result block under any circumstances.
