---
name: agentshore-groom-backlog
description: "Action slot 16 — Groom Backlog. Audits the beads project graph against current GitHub issues, closes stale or orphaned beads, corrects mislabeled items, re-links beads to their parent epics, reconciles the issues↔beads graph so every open GH issue has a beads task and every beads task has a GH issue, and flags oversized issues for refinement with a proposed decomposition."
disable-model-invocation: true
allowed-tools: Read, Bash(bd:*, gh:*, git:*)
---

# agentshore-groom-backlog

Reconcile the beads graph against GitHub from `$AGENTSHORE_PROJECT_PATH` (bd lives in the main repo). `$ARGUMENTS` is unused. Read `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md` for project conventions (label taxonomy, triage rules, epic shape); apply concrete requirements and ignore vague advice.

**Pre-flight:** read `$AGENTSHORE_PROJECT_PATH/.agentshore/context.json` (repo, owner, session_id). Confirm `.beads/` exists or stop with `error: "beads not initialised"`. Snapshot `bd list --all --json --limit 0`, derive the epic/story/task summary → `epics_before`. Also fetch `gh issue list --state open --limit 200 --json number,title,state,labels,body` and `--state closed --limit 100`.

**Reconciliation invariant** (must hold before `success: true`):
1. Every open GH issue has a beads task with `external_ref="gh-<N>"`.
2. Every open beads task has a non-null `external_ref`.
3. Every story is linked to an epic; every task to a story.
4. `bd list --all --json --limit 0` runs cleanly and returns ≥ 1 epic.

Violations populate `verification_failures` and force `success: false`.

**State roles.** Every triaged issue carries one **category** (`bug` if labeled `bug`/`type/bug`; else `enhancement`) and one **state** derived from labels:

| State | Trigger |
|---|---|
| `needs-triage` | unlabeled, or `agentshore/needs-refinement` |
| `needs-info` | `agentshore/blocked` |
| `ready-for-agent` | has `priority/*` + `size/*`, no blocker labels |
| `ready-for-human` | `needs-human-review`, or `agentshore/decomposed` (tracker) |
| `wontfix` | `agentshore/disallowed`, or closed |

Conflicting state labels (e.g. both `agentshore/blocked` and `priority/critical`) → record in `verification_failures`.

**Classify open beads.** For each, flag any that apply: **Stale** (`external_ref=gh-N`, issue closed or absent), **Shipped** (work already landed), **Orphaned** (parent epic closed/missing), **Mislabeled** (clear-cut type error only — don't guess), **Duplicate** (same `external_ref`, keep newest). Record in `grooming_plan`.

A bead is **Shipped** only when at least one holds:
- Merged PR's body contains `Closes/Fixes/Resolves #<N>` (`gh pr list --search "<N> in:body" --state merged`).
- Recent commits (`git log --since="30 days ago"`) implement the change by subject/path AND `grep -rn` confirms the named symbol/path is present in `src/`.
- For epic/story: every child is closed AND one of the above holds for the parent's outcome.

Partial evidence → **keep**, not Shipped. Record per-item verdicts in `grooming_plan.verification` as `{id, verdict: "stale_close" | "keep", evidence: "<one line>"}`.

**Apply changes.** Stale/duplicate: `bd close <id>`. Orphaned: `bd link <id> <epic> --type parent-child` (child first, parent second; `--parent` is not a valid flag, and the default `blocks` type would wrongly block the child) if a parent exists, else close. Mislabeled: `bd update <id> --type <correct>`.

**Reconcile both directions.** Open bead with no `external_ref`: `gh issue list --search "<title>" --state open --limit 5`; on exact case-insensitive single match (no other bead holds that ref) `bd update <id> --external-ref "gh-<N>"`, else `gh issue create … --label enhancement` and link the new number. Open GH issue with no bead: `bd create task "<title>" --description "Closes gh-<N>" --external-ref "gh-<N>"` and `bd link <task-id> <story-id> --type parent-child` to the most appropriate story (create one via Step 3 if none fits).

**Mirror ordering dependencies to beads.** Ordering edges written as `depends on #N` / `blocked by #N` in an issue body must become real beads `blocks` edges — otherwise the cheap `issue_pickup` candidate mask can't see them and an agent is dispatched to a blocked issue only to be rejected agent-side (~$0.19 / 6.8 min wasted per hit — #14). For each open GH issue whose body declares `depends on #N` / `blocked by #N`: if **#N is still open** and both issues have beads tasks (`external_ref` `gh-<N>` and `gh-<this>`), ensure a blocks edge exists — `bd link <this-task-id> <dep-task-id> --type blocks` (second arg blocks first, so `<this>` becomes `blocked_by <dep>`; idempotent — read the snapshot's `dependencies`/`blocked_by` and skip if already present). Never self-link; skip when #N is already closed. Reserve `blocks` strictly for these ordering deps (containment stays `parent-child`). beads auto-clears the edge when the dependency task closes (its PR merges → issue closes → task closes), re-arming pickup with no extra work. Record in `dependency_edges_added` as `{"issue": N, "blocked_by": DEP}`.

**Close shipped work.** For every verdict `stale_close`, close child tasks → stories → epics in that order. `bd close <ids…> --reason="shipped: <sha or PR #>"` and `gh issue close <N> --comment "Closed by groom-backlog: shipped in <sha or PR #>."`. Record in `beads_closed_stale` / `issues_closed_stale`. Shipped takes precedence over Stale/Duplicate/Orphaned so the evidence is the reason persisted.

**Close completed trackers.** For each open GH issue labeled `agentshore/decomposed`, take the union of children from `gh issue list --search "Parent: #<N>" --state all` (also `"depends on #<N>"`) and parsed `- [ ] #<M>` / `- [x] #<M>` entries from the parent's `## Sub-tasks` checklist. If the union is non-empty and every child is `CLOSED`, close the parent with a sub-task list comment → `trackers_closed`. Skip parents labeled `agentshore/blocked` or `needs-human-review`; empty union is malformed — record in `verification_failures`. For each open epic, if every child story is `closed`, `bd close <epic_id> --reason "All child stories complete"` → `epics_closed`. Closed-as-wontfix children still count as closed.

**Flag oversized issues for refinement** (do not decompose — `refine_tasks` does that). Flag if ≥ 2 of these fire:
1. Body > 4000 chars.
2. ≥ 3 non-standard `##` headings (exclude `Source references`, `Why current evidence is insufficient`, `Acceptance criteria`, `Likely source/test areas`, `Scope`, `Blocked by`, `Tracked by`).
3. ≥ 5 unchecked `- [ ]` items in the body.
4. Labeled `agentshore/epic` with no child referencing it via `Decomposed from #<N>` / `Sub-task of #<N>` / `Parent: #<N>`.

Skip if already labeled `agentshore/needs-refinement`, body contains `Decomposed from #` / `Parent: #`, or an open PR covers it. Cap 3 flags per run (highest signal count, then oldest number). For each, build a structural proposal from the issue's own headings (one child per non-standard `##`; group `- [ ]` items into 2–5 children if signal 3 fired), each child a 5–10 word title + 1–2 sentence scope. Don't invent acceptance criteria the parent doesn't already imply. Post `gh issue comment <N> --body` whose first line is literally `AGENTSHORE_GROOM_DECOMPOSITION_PROPOSAL` (downstream detection token), then the signal list, `## Proposed sub-tasks` enumeration, and the line `Advisory proposal; refine_tasks decides the final decomposition.`. Then `gh issue edit <N> --add-label "agentshore/needs-refinement" --remove-label "agentshore/refined"` (removing `agentshore/refined` re-arms refinement if the issue was previously refined). Record in `issues_flagged_for_refinement` as `{"issue": N, "signals": [...], "proposed_children": K}`.

**Verify.** Re-fetch `bd list --all --json --limit 0` and `gh issue list --state open --limit 200`. Check each invariant. Confirm `agentshore/needs-refinement` actually applied to every flagged issue; confirm every `trackers_closed` parent reports `CLOSED`; confirm every `epics_closed` returns `bd show … status: closed`; confirm every `beads_closed_stale` / `issues_closed_stale` is closed. Derive `epics_after`. Snapshot `open_work_after` counts (`gh issue list --state open --limit 200`, `gh pr list --state open --limit 50`). Any failure → `verification_failures`, then `success: false`.

**Forbidden mutations:**
- Never touch `.github/workflows/**` or any CI config.
- Never call `git worktree add/remove/prune` — AgentShore owns lifecycle.
- Never edit product code, tests, or docs.

**Report — one fenced JSON block, nothing else:**

```json
{
  "success": true,
  "artifacts": [],
  "beads_closed": [],
  "beads_closed_stale": [],
  "beads_relinked": [],
  "beads_relabeled": [],
  "duplicates_removed": [],
  "issues_created": [],
  "issues_closed_stale": [],
  "issues_flagged_for_refinement": [],
  "trackers_closed": [],
  "epics_closed": [],
  "ambiguous_links_resolved": [],
  "dependency_edges_added": [],
  "epics_before": [],
  "epics_after": [],
  "open_work_after": {"issues": 0, "prs": 0},
  "verification_failures": [],
  "error": null
}
```

A clean graph with no changes is `success: true` with all empty lists — not an error. Always emit the block — skipping causes `no valid result block` and discards the work.
