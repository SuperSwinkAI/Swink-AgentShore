---
name: agentshore-design-audit
description: "Action slot 9 — Design Audit. Reviews project design files, specs, PRDs, ADRs, and related docs against source/tests/GitHub/beads, then creates or links GitHub issues and beads tasks for every concrete unmet requirement."
argument-hint: []
disable-model-invocation: true
allowed-tools: Read, Bash(bd:*, gh:*, git:*)
---

# agentshore-design-audit

Audit the project's design/spec corpus for concrete requirements that lack implementation evidence and lack open tracking. Do not implement code — only file or link trackers for real gaps.

**Project docs are authoritative.** Read `$AGENTSHORE_PROJECT_PATH/.agentshore/context.json` (seed file, learnings, conventions) and discover the corpus via `git ls-files`: `docs/PRD.md`, `docs/design/**/*.md`, `docs/specs/**/*.md`, `docs/**/*.md`, `ADR*.md`, `AGENTS.md`, `CLAUDE.md`, `README.md`, and any other repo-local planning docs. `cd "$AGENTSHORE_PROJECT_PATH"` — beads lives in the main repo, this is a trunk-scoped skill. Verify GitHub identity with `gh api user --jq .login` against `assigned_github_identity` from context (casefolded).

**Success condition.** Every concrete requirement you audit must end up in exactly one bucket:
- `verified_done` — source/tests/docs evidence shows it exists.
- `represented_open` — an open GH issue and an open beads task already track it.
- `gap_filled` — this play created or linked the open GH issue and open beads task.

Closed issues, closed beads tasks, or PR titles are not proof of done. If any concrete requirement remains untracked without implementation evidence, return `success: false`.

**Filter for friction (gate every candidate finding):**
- **Deletion test:** if removing the requirement collapses caller/test/UX complexity, file it; if it only removes a paragraph from a doc, drop it.
- **Two-adapter rule:** a proposed seam isn't real work until a second concrete adapter needs it.
- **HLD respect:** if it contradicts `docs/design/HLD.md` or a component design doc, only file when there is user-visible impact or a concrete future-change cost. Mark: `contradicts docs/design/HLD.md §X — but worth reopening because <evidence>`. Skip the gate if `docs/design/` is absent.

**Workflow.** Build a requirement inventory (title, source file/heading, expected behavior, acceptance criteria). Verify implementation in source/tests/docs with concrete paths. Then:

```
gh issue list --state open   --json number,title,body,labels --limit 200
gh issue list --state closed --json number,title,body,labels --limit 100
bd list --all --json --limit 0
```

For each unmet requirement: if an open GH issue and open beads task exist, mark `represented_open`. If only the GH issue exists, link a beads task with `--external-ref "gh-<N>"`. If neither, create a GH issue (with source reference, why current evidence is insufficient, acceptance criteria, and likely files/tests), then `bd create task "<title>" --description "Closes gh-<N>" --external-ref "gh-<N>"` and `bd link <task-id> <story-id> --type parent-child` (best matching open story; create a concise story under the relevant epic only if none fits — `--type parent-child` is required: bd's default link type is `blocks`, which would block the task behind its story and hide it from `issue_pickup`). Skip anything already linked by `external_ref`. Re-run `bd list` and confirm every gap issue has a matching open task.

**New GH issues must include:** source file + heading, why current evidence is insufficient, acceptance criteria an implementer can test, and likely source/test areas. Labels: `enhancement` by default plus existing project labels only when clearly appropriate.

**Size routing.** Sizing and decomposition belong to `agentshore-refine-tasks`. This play only routes by intent: when the cited source section describes multiple distinct deliverables worth separate issues, apply `agentshore/needs-refinement` via `gh issue edit <N> --add-label "agentshore/needs-refinement" --remove-label "agentshore/refined"` (removing `agentshore/refined` re-arms refinement if the issue was previously refined). Single-deliverable issues pass through unlabeled.

**Close shipped trackers.** For each `verified_done` requirement, match any still-open GH issue or beads task by title keyword, source-file reference, or `external_ref` and close it:

```
gh issue close <N> --comment "Closed by design-audit: implementation verified at <path:line>."
bd close <bead_id> --reason="design-audit verified shipped at <path:line>"
```

Record in `issues_closed_stale` and `beads_closed_stale`. Skip when evidence is partial (one of two criteria still unmet) — leave open.

**Snapshot remaining open work** for the next play: `gh issue list --state open --limit 200 --json number,title,labels` and `gh pr list --state open --limit 50 --json number,title,headRefName`. Counts go in `open_work_after`.

**Forbidden:**
- Editing product code, tests, docs, or CI files.
- Closing or reopening GH issues beyond the `verified_done` cleanup above.
- Deleting beads nodes.
- Creating duplicate issues for already-tracked open work.
- `git worktree add/remove/prune` (AgentShore owns worktree lifecycle).

**Report — one fenced JSON block:**

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
  "issues_closed_stale": [],
  "beads_closed_stale": [],
  "tasks_linked": ["tlc-abc.1", "tlc-def.2"],
  "open_work_after": {"issues": 0, "prs": 0},
  "error": null
}
```

On any failed GH/beads mutation set `success: false`, name the failed requirement in `error`, and keep successfully created issue/task numbers in the result.
