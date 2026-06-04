---
name: agentshore-groom-backlog
description: "Action slot 16 — Groom Backlog. Keeps the current GitHub backlog correct first (triage label hygiene, un-stick issues whose blockers have resolved, flag oversized issues for refinement), then cleans up stale beads (close shipped/stale/orphaned/duplicate, relink, reconcile so every open issue has a bead and every bead a GH issue), then applies a small bounded pass of frontier dependency edges. Every bd command runs strictly sequentially."
disable-model-invocation: true
allowed-tools: Read, Bash(bd:*, gh:*, git:*)
---

# agentshore-groom-backlog

Groom the backlog from `$AGENTSHORE_PROJECT_PATH` (bd lives in the main repo). `$ARGUMENTS` is unused. Read `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md` for project conventions (label taxonomy, triage rules, epic shape); apply concrete requirements and ignore vague advice.

**Pre-flight.** Read `$AGENTSHORE_PROJECT_PATH/.agentshore/context.json` (repo, owner, session_id). Confirm `.beads/` exists or stop with `error: "beads not initialised"`. Snapshot `bd list --all --json --limit 0`, derive the epic/story/task summary → `epics_before`. Fetch `gh issue list --state open --limit 200 --json number,title,state,labels,body` and `--state closed --limit 100`. The GH list (bodies + labels + numbers) is the working set for Phases 1–3; recompute nothing per-issue that this list already answers.

> ⚠️ **Never background or parallelize `bd`.** No `&`, no background loops, no concurrent `bd` invocations, no "run it in the background while I do X". Embedded Dolt is a single-writer store; concurrent `bd` processes pile up on its lock, each gets slower, and the play burns its entire timeout producing nothing (no result block, work discarded). Run every `bd` command **sequentially, one at a time, foreground**. `gh` and `git` reads are fine to interleave.

> ℹ️ **The bd snapshot carries `dependency_count` / `dependent_count` only — NOT the edge targets.** `bd list --json` and `.beads/issues.jsonl` tell you *how many* edges a bead has, never *which* beads they point to. To read edge targets you must call `bd dep list <id> --json` — do this only in Phase 3, and only for the capped frontier set.

**Reconciliation invariants** (must hold before `success: true`):
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

---

## Phase 1 — Current GitHub backlog updates (primary)

Keep the human-facing GitHub backlog correct first. These are GH-surface edits (`gh`), no beads-graph traversal required.

**Triage label hygiene.** For each open GH issue, derive its category + state from the table above. Conflicting state labels (e.g. both `agentshore/blocked` and `priority/critical`) → record in `verification_failures` (do not guess a resolution).

**Remove resolved blocked labels.** A GH issue's `blocked` / `agentshore/blocked` label is sticky — it does **not** auto-clear when the blocker resolves, so the issue silently stays out of the `issue_pickup` pool forever even though nothing blocks it. For each open GH issue carrying `blocked` or `agentshore/blocked`, read its `blocked by #N` / `depends on #N` declarations **from the body in the GH list already fetched** (no bd calls). Remove the label **only** when there is ≥ 1 identifiable dependency **AND every** referenced `#N` is `CLOSED` (decidable from the open/closed GH lists in hand): `gh issue edit <N> --remove-label "<the blocking label actually present>"` (remove whichever of `blocked` / `agentshore/blocked` the issue carries — removing an absent label errors), then `gh issue comment <N> --body "Unblocked by groom-backlog: all blocking dependencies resolved (<#N…>)."`. **Leave the label in place** when no dependency can be identified (an opaque/manual block — a human gate we cannot reason about), any referenced blocker is still open, or the issue also carries `needs-human-review`. **Cap 15 per run** (oldest issue number first). Record each removal in `blocks_cleared` as `{"issue": N, "resolved_deps": [...]}`; record any beyond the cap in `blocks_clear_deferred` as `{"issue": N}`.

**Flag oversized issues for refinement** (do not decompose — `refine_tasks` does that). Flag if ≥ 2 of these fire:
1. Body > 4000 chars.
2. ≥ 3 non-standard `##` headings (exclude `Source references`, `Why current evidence is insufficient`, `Acceptance criteria`, `Likely source/test areas`, `Scope`, `Blocked by`, `Tracked by`).
3. ≥ 5 unchecked `- [ ]` items in the body.
4. Labeled `agentshore/epic` with no child referencing it via `Decomposed from #<N>` / `Sub-task of #<N>` / `Parent: #<N>`.

Skip if already labeled `agentshore/needs-refinement`, body contains `Decomposed from #` / `Parent: #`, or an open PR covers it. Cap 3 flags per run (highest signal count, then oldest number). For each, build a structural proposal from the issue's own headings (one child per non-standard `##`; group `- [ ]` items into 2–5 children if signal 3 fired), each child a 5–10 word title + 1–2 sentence scope. Don't invent acceptance criteria the parent doesn't already imply. Post `gh issue comment <N> --body` whose first line is literally `AGENTSHORE_GROOM_DECOMPOSITION_PROPOSAL` (downstream detection token), then the signal list, `## Proposed sub-tasks` enumeration, and the line `Advisory proposal; refine_tasks decides the final decomposition.`. Then `gh issue edit <N> --add-label "agentshore/needs-refinement" --remove-label "agentshore/refined"` (removing `agentshore/refined` re-arms refinement if the issue was previously refined). Record in `issues_flagged_for_refinement` as `{"issue": N, "signals": [...], "proposed_children": K}`.

---

## Phase 2 — Stale beads cleanup (secondary)

Now reconcile the canonical beads graph against the (freshly-updated) GitHub state. All `bd` mutations here run **sequentially**.

**Classify open beads.** For each, flag any that apply: **Stale** (`external_ref=gh-N`, issue closed or absent), **Shipped** (work already landed), **Orphaned** (parent epic closed/missing), **Mislabeled** (clear-cut type error only — don't guess), **Duplicate** (same `external_ref`, keep newest). Record in `grooming_plan`.

A bead is **Shipped** only when at least one holds:
- Merged PR's body contains `Closes/Fixes/Resolves #<N>` (`gh pr list --search "<N> in:body" --state merged`).
- Recent commits (`git log --since="30 days ago"`) implement the change by subject/path AND `grep -rn` confirms the named symbol/path is present in `src/`.
- For epic/story: every child is closed AND one of the above holds for the parent's outcome.

Partial evidence → **keep**, not Shipped. Record per-item verdicts in `grooming_plan.verification` as `{id, verdict: "stale_close" | "keep", evidence: "<one line>"}`.

**Apply changes.** Stale/duplicate: `bd close <id>`. Orphaned: `bd link <id> <epic> --type parent-child` (child first, parent second; `--parent` is not a valid flag, and the default `blocks` type would wrongly block the child) if a parent exists, else close. Mislabeled: `bd update <id> --type <correct>`.

**Reconcile both directions.** Open bead with no `external_ref`: `gh issue list --search "<title>" --state open --limit 5`; on exact case-insensitive single match (no other bead holds that ref) `bd update <id> --external-ref "gh-<N>"`, else `gh issue create … --label enhancement` and link the new number. Open GH issue with no bead: `bd create task "<title>" --description "Closes gh-<N>" --external-ref "gh-<N>"` and `bd link <task-id> <story-id> --type parent-child` to the most appropriate story (create one if none fits).

**Close shipped work.** For every verdict `stale_close`, close child tasks → stories → epics in that order. `bd close <ids…> --reason="shipped: <sha or PR #>"` and `gh issue close <N> --comment "Closed by groom-backlog: shipped in <sha or PR #>."`. Record in `beads_closed_stale` / `issues_closed_stale`. Shipped takes precedence over Stale/Duplicate/Orphaned so the evidence is the reason persisted.

**Close completed trackers.** For each open GH issue labeled `agentshore/decomposed`, take the union of children from `gh issue list --search "Parent: #<N>" --state all` (also `"depends on #<N>"`) and parsed `- [ ] #<M>` / `- [x] #<M>` entries from the parent's `## Sub-tasks` checklist. If the union is non-empty and every child is `CLOSED`, close the parent with a sub-task list comment → `trackers_closed`. Skip parents labeled `agentshore/blocked` or `needs-human-review`; empty union is malformed — record in `verification_failures`. For each open epic, if every child story is `closed`, `bd close <epic_id> --reason "All child stories complete"` → `epics_closed`. Closed-as-wontfix children still count as closed.

---

## Phase 3 — Bounded frontier dependency edges (capped, sequential)

Mirror ordering edges into beads so the cheap `issue_pickup` candidate mask can see them — otherwise an agent is dispatched to a blocked issue only to be rejected agent-side (~$0.19 / 6.8 min wasted per hit — #14). **Do not reconcile the whole graph** — that does not scale and is the source of the lock-pileup failure. Reconcile only the **pickup frontier**, the small set of issues a dispatch could actually hit next, capped per run; later runs cover the rest.

**Select the frontier (from the GH list — no bd calls).** Take open GH issues that are `ready-for-agent` (have `priority/*` + `size/*`, no blocker label) **and** whose body declares `depends on #N` / `blocked by #N` with **#N still open**. Order by priority then oldest number. **Cap K = 15.**

**Apply each frontier edge sequentially.** For each selected issue (one at a time, never in a loop that backgrounds): both issues must have beads tasks (`external_ref` `gh-<this>` and `gh-<N>`). Run a single `bd dep list <this-task-id> --json` to check whether a `blocks` edge to the dep already exists; if absent, `bd link <this-task-id> <dep-task-id> --type blocks` (second arg blocks first, so `<this>` becomes `blocked_by <dep>`). Never self-link; skip when `#N` is already closed. Reserve `blocks` strictly for these ordering deps (containment stays `parent-child`). beads auto-clears the edge when the dependency task closes (its PR merges → issue closes → task closes), re-arming pickup with no extra work. Record applied edges in `dependency_edges_added` as `{"issue": N, "blocked_by": DEP}`; record any frontier issue beyond the cap in `dependency_edges_deferred` as `{"issue": N}` — never silently drop them.

---

**Verify.** Re-fetch `bd list --all --json --limit 0` and `gh issue list --state open --limit 200`. Check each invariant. Confirm every issue in `blocks_cleared` no longer carries `blocked` / `agentshore/blocked`; confirm `agentshore/needs-refinement` applied to every flagged issue; confirm every `trackers_closed` parent reports `CLOSED`; confirm every `epics_closed` returns `bd show … status: closed`; confirm every `beads_closed_stale` / `issues_closed_stale` is closed. Confirm `dependency_edges_deferred` + `blocks_clear_deferred` hold whatever the per-run caps deferred (no silent truncation). Derive `epics_after`. Snapshot `open_work_after` counts (`gh issue list --state open --limit 200`, `gh pr list --state open --limit 50`). Any failure → `verification_failures`, then `success: false`.

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
  "blocks_cleared": [],
  "blocks_clear_deferred": [],
  "trackers_closed": [],
  "epics_closed": [],
  "ambiguous_links_resolved": [],
  "dependency_edges_added": [],
  "dependency_edges_deferred": [],
  "epics_before": [],
  "epics_after": [],
  "open_work_after": {"issues": 0, "prs": 0},
  "verification_failures": [],
  "error": null
}
```

A clean graph with no changes is `success: true` with all empty lists — not an error. Always emit the block — skipping causes `no valid result block` and discards the work.
