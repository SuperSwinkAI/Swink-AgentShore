---
name: agentshore-groom-backlog
description: "Action slot 16 — Groom Backlog. Keeps the current GitHub backlog correct first (triage label hygiene, un-stick issues whose blockers have resolved, flag oversized issues for refinement), then cleans up stale beads (close shipped/stale/orphaned/duplicate, relink, reconcile so every open issue has a bead and every bead a GH issue), then applies a small bounded pass of frontier dependency edges. Every bd command runs strictly sequentially."
disable-model-invocation: true
allowed-tools: Read, Bash(bd:*, gh:*, git:*)
---

# agentshore-groom-backlog

Groom the backlog from `$AGENTSHORE_PROJECT_PATH` (bd lives in the main repo). `$ARGUMENTS` is unused. Read `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md` for project conventions (label taxonomy, triage rules, epic shape); apply concrete requirements and ignore vague advice.

**Re-distill learnings (do this first).** Read the full `$AGENTSHORE_PROJECT_PATH/.agentshore/learnings.json` (each entry has an `id`, `pattern`, `category`, and `confidence`). Produce a consolidated set: merge entries that say the same thing, reword for sharpness, and drop entries now obsolete or contradicted by the current repo. This is qualitative — **keep every distinct insight, collapse only genuine redundancy, and do not aim for any target count** (a small clean store is fine left as-is). Hold this consolidated understanding and **apply it as you groom below** — it is your sharpest record of this repo's conventions, so let it inform triage, refinement flags, and reconciliation. Emit the result as `learnings_compacted` in the result block. For each consolidated entry, list `merged_from`: the `id`s of the source entries you folded into it (echo them exactly) so AgentShore preserves their confidence/recency; a genuinely new synthesis can use an empty `merged_from`. Do **not** include a `confidence` on these — AgentShore re-derives it. **Omit `learnings_compacted` entirely if you merged/removed nothing** (the store stays untouched). This is separate from the incremental `learnings` array below, which is for net-new insights from *this* grooming run.

**Pre-flight.** Read `$AGENTSHORE_PROJECT_PATH/.agentshore/context.json` (repo, owner, session_id). Confirm `.beads/` exists or stop with `error: "beads not initialised"`. Snapshot `bd list --all --json --limit 0`, derive the epic/story/task summary → `epics_before`. Fetch `gh issue list --state open --limit 200 --json number,title,state,labels,body` and `--state closed --limit 100`. The GH list (bodies + labels + numbers) is the working set for Phases 1–3; recompute nothing per-issue that this list already answers.

> ⚠️ **Never background or parallelize `bd`.** No `&`, no background loops, no concurrent `bd` invocations, no "run it in the background while I do X". Embedded Dolt is a single-writer store; concurrent `bd` processes pile up on its lock, each gets slower, and the play burns its entire timeout producing nothing (no result block, work discarded). Run every `bd` command **sequentially, one at a time, foreground**. `gh` and `git` reads are fine to interleave.

> ℹ️ **The bd snapshot already carries edge targets — no per-bead calls needed.** `bd list --all --json` emits a per-bead `dependencies` array; each entry has a `depends_on_id` (the bead this one is blocked by) and a `type`. Read edge targets directly from `.dependencies[].depends_on_id` in the Pre-flight snapshot. Do **not** fan out per-bead `bd dep list <id> --json` calls to discover or existence-check edges — that fan-out hits the same single-writer bottleneck called out above; the snapshot you already hold has every edge.

**Reconciliation invariants** — these describe the state groom **resolves the backlog into**; they are *work to do*, not gates that fail the play:
1. Every open GH issue has a bead (**any type** — task, story, feature, or epic) with `external_ref="gh-<N>"`. A GH issue that already mirrors to a story/feature/epic **satisfies this** — do not demand a `task` specifically. Only a genuinely bead-less issue is unreconciled; Phase 2 fixes it by creating a task and linking it.
2. Every open beads task has a non-null `external_ref` — reconcile it in Phase 2 if missing.
3. Every story is linked to an epic; every task to a story — `bd link` any that aren't.
4. `bd list --all --json --limit 0` runs cleanly and returns ≥ 1 epic.

**Imperfections are resolved, never failed.** Work each violation above in Phases 2–3 until it holds. An imperfection you genuinely cannot auto-resolve (e.g. a decomposed tracker whose children can't be inferred, or an ambiguous title→issue mapping) is **flagged for a human**, not failed: `gh issue edit <N> --add-label "needs-human-review"` with an explanatory comment, and record it in `flagged_for_human`. Reserve `verification_failures` + `success: false` strictly for a **hard execution error** — the bd snapshot won't run (invariant 4), beads isn't initialised, or a `bd`/`gh` mutation command errored — never for a backlog imperfection you detected and handled.

**Dolt persistence is local — never push, never fail on it.** `bd` mutations auto-commit to the **local** beads DB; that local state is the authoritative result of this play and is all the next play reads. AgentShore does not require or configure a Dolt remote. Treat any Dolt sync/remote notice `bd` prints — e.g. `bd dolt push skipped because no Dolt remote`, "no remote configured", "could not push" — as an expected, environmental no-op. **Do not run `bd dolt push` yourself, and never set `success: false` because of a Dolt push/sync/remote message.**

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

**Triage label hygiene.** For each open GH issue, derive its category + state from the table above.

**Blocked + priority conflict (auto-resolve).** When an issue carries both `agentshore/blocked` and any `priority/*` label simultaneously, `blocked` wins: remove every `priority/*` label with `gh issue edit <N> --remove-label "<label>"` (one call per label; removing an absent label errors — only remove labels the issue actually carries), then add a comment `gh issue comment <N> --body "groom-backlog: removed priority label(s) while issue is blocked. Re-apply once unblocked."`. Record each resolution in `conflicting_labels_resolved` as `{"issue": N, "removed": ["priority/..."], "reason": "blocked wins"}`. Cap 20 per run; record extras in `conflicting_labels_deferred` as `{"issue": N}`.

Any other conflicting state label combination → record in `conflicting_labels_skipped` as `{"issue": N, "conflict": ["label-a", "label-b"]}` and skip that issue for this run (do not guess a resolution, do not add to `verification_failures`). Cap 20 per run; record extras in `conflicting_labels_skipped_deferred` as `{"issue": N}`.

**Remove resolved blocked labels.** A GH issue's `blocked` / `agentshore/blocked` label is sticky — it does **not** auto-clear when the blocker resolves, so the issue silently stays out of the `issue_pickup` pool forever even though nothing blocks it. For each open GH issue carrying `blocked` or `agentshore/blocked`:

0. **Needs-human override (check first, before tracing).** If the issue also carries `agentshore/needs-human` or `needs-human-review`, **do not trace or clear anything for it in this step** — leave every label untouched and move to the next issue. Either label means a prior pass already determined this needs a human decision (for `agentshore/needs-human`, frequently a beads dependency-cycle conflict where the recorded blocker direction contradicts a real one — clearing `agentshore/blocked` here would just let `issue_pickup` re-discover the identical contradiction and re-block it). Only a human removing `agentshore/needs-human` / `needs-human-review` re-arms this issue for automatic clearing.

1. **Trace its blocker(s)** from every source you already hold or can cheaply read:
   - **Body** (`gh` list already fetched): `blocked by #N` / `depends on #N` declarations.
   - **beads edges** (Pre-flight snapshot, no per-bead calls): the issue's own bead (`external_ref="gh-<this>"`) `.dependencies[].depends_on_id`; map each `depends_on_id` bead back to its `external_ref` `gh-<M>` — that `#M` is a blocker. beads edges self-heal on close, so an *open* edge here means a live blocker.
   - **Marker comment** (only when steps above found nothing, and only for issues carrying `agentshore/blocked`): `gh issue view <N> --json comments` and scan for `<!-- agentshore:blocked-by #M -->`. AgentShore posts this when it stamps `agentshore/blocked` without a bead mirror; the `#M` is the blocker. (Targeted per-issue `gh` read — fine to interleave; never a `bd` call.)

2. **Decide** (each `#M` is decidable `OPEN`/`CLOSED` from the open/closed GH lists in hand — re-check the specific `#M` against those lists before writing anything; never assert "resolved" from memory or inference):
   - **Any traced blocker still `OPEN`** → leave the label (genuinely blocked).
   - **≥ 1 traced blocker AND every one verified `CLOSED`** → remove it: `gh issue edit <N> --remove-label "<the blocking label actually present>"` (remove whichever of `blocked` / `agentshore/blocked` the issue carries — removing an absent label errors), then `gh issue comment <N> --body "Unblocked by groom-backlog: all blocking dependencies resolved (#M, #M, ...)."` **citing every specific `#M` you verified closed — never write this comment with an empty or omitted citation.** Record in `blocks_cleared` as `{"issue": N, "resolved_deps": [...]}`. **Cap 15 per run** (oldest number first); extras → `blocks_clear_deferred` as `{"issue": N}`.
   - **No blocker traceable from any source** → this depends on the label:
     - **`agentshore/blocked`** (AgentShore-namespaced — it only ever comes from a `block_issue_on` gate whose blocker is now untraceable, i.e. lost/stale) **and the issue does not carry `needs-human-review`** (already excluded by step 0 if it also carries `agentshore/needs-human`) → **sweep it**: `gh issue edit <N> --remove-label "agentshore/blocked"`, then `gh issue comment <N> --body "Unblocked by groom-backlog: agentshore/blocked carried no traceable dependency (no body declaration, beads edge, or marker) — clearing the stale gate. Re-block via issue_pickup if a real dependency remains."`. Record in `blocks_swept` as `{"issue": N}`. **Cap 15 per run** (oldest number first); extras → `blocks_swept_deferred` as `{"issue": N}`.
     - **plain `blocked`** (may be a human-set gate, not AgentShore's) **or `needs-human-review` present** → leave the label untouched (do not guess at a human's intent).

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

**Close completed trackers.** For each open GH issue labeled `agentshore/decomposed`, take the union of children from `gh issue list --search "Parent: #<N>" --state all` (also `"depends on #<N>"`) and parsed `- [ ] #<M>` / `- [x] #<M>` entries from the parent's `## Sub-tasks` checklist. If the union is non-empty and every child is `CLOSED`, close the parent with a sub-task list comment → `trackers_closed`. Skip parents labeled `agentshore/blocked` or `needs-human-review`; an empty union means the tracker's children can't be inferred — **flag it, don't fail**: `gh issue edit <N> --add-label "needs-human-review"` and `gh issue comment <N> --body "groom-backlog: labeled agentshore/decomposed but no child issues were discoverable via Parent/depends-on search or a '## Sub-tasks' checklist — needs a human to relink or close."`, then record in `flagged_for_human`. For each open epic, if every child story is `closed`, `bd close <epic_id> --reason "All child stories complete"` → `epics_closed`. Closed-as-wontfix children still count as closed.

---

## Phase 3 — Bounded frontier dependency edges (capped, sequential)

Mirror ordering edges into beads so the cheap `issue_pickup` candidate mask can see them. **Do not reconcile the whole graph** — that does not scale and is the source of the lock-pileup failure. Reconcile only the **pickup frontier**, the small set of issues a dispatch could actually hit next, capped per run; later runs cover the rest.

**Select the frontier (from the GH list — no bd calls).** Take open GH issues that are `ready-for-agent` (have `priority/*` + `size/*`, no blocker label) **and** whose body declares `depends on #N` / `blocked by #N` with **#N still open**. Order by priority then oldest number. **Cap K = 15.**

**Apply each frontier edge sequentially.** For each selected issue (one at a time, never in a loop that backgrounds): both issues must have beads tasks (`external_ref` `gh-<this>` and `gh-<N>`). Check the Pre-flight snapshot's `dependencies` for `<this-task-id>` — if it already holds a `blocks` edge with `depends_on_id == <dep-task-id>`, skip; else `bd link <this-task-id> <dep-task-id> --type blocks` (second arg blocks first, so `<this>` becomes `blocked_by <dep>`). The snapshot already carries existing edges (see the note above), so no per-bead `bd dep list` is needed for the existence check. Never self-link; skip when `#N` is already closed. Reserve `blocks` strictly for these ordering deps (containment stays `parent-child`). beads auto-clears the edge when the dependency task closes (its PR merges → issue closes → task closes), re-arming pickup with no extra work. Record applied edges in `dependency_edges_added` as `{"issue": N, "blocked_by": DEP}`; record any frontier issue beyond the cap in `dependency_edges_deferred` as `{"issue": N}` — never silently drop them.

---

**Verify.** Re-fetch `bd list --all --json --limit 0` and `gh issue list --state open --limit 200`. Check each invariant. Confirm every issue in `blocks_cleared` and `blocks_swept` no longer carries the label that was removed; confirm `agentshore/needs-refinement` applied to every flagged issue; confirm every `trackers_closed` parent reports `CLOSED`; confirm every `epics_closed` returns `bd show … status: closed`; confirm every `beads_closed_stale` / `issues_closed_stale` is closed. Confirm `dependency_edges_deferred` + `blocks_clear_deferred` + `blocks_swept_deferred` hold whatever the per-run caps deferred (no silent truncation). Derive `epics_after`. Snapshot `open_work_after` counts (`gh issue list --state open --limit 200`, `gh pr list --state open --limit 50`). An invariant still unmet that you **could** resolve → resolve it now; one you genuinely cannot → flag for human (`needs-human-review` + comment) and record in `flagged_for_human`. Only a hard execution error (snapshot won't run, a `bd`/`gh` mutation errored) goes to `verification_failures` → `success: false`.

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
  "beads_relinked": [],
  "issues_created": [],
  "issues_flagged_for_refinement": [],
  "blocks_cleared": [],
  "blocks_clear_deferred": [],
  "blocks_swept": [],
  "blocks_swept_deferred": [],
  "conflicting_labels_resolved": [],
  "conflicting_labels_deferred": [],
  "conflicting_labels_skipped": [],
  "conflicting_labels_skipped_deferred": [],
  "trackers_closed": [],
  "epics_closed": [],
  "dependency_edges_added": [],
  "dependency_edges_deferred": [],
  "open_work_after": {"issues": 0, "prs": 0},
  "flagged_for_human": [],
  "verification_failures": [],
  "learnings_compacted": [{"pattern": "label `priority/*` and `agentshore/blocked` are mutually exclusive; remove priority when blocked", "category": "conventions", "merged_from": ["<id-a>", "<id-b>"]}],
  "learnings": [{"pattern": "the agentshore/decomposed tracker pattern requires a '## Sub-tasks' section; plain checkbox lists in the body are not detected by the close-tracker logic", "confidence": 0.75, "category": "conventions"}],
  "error": null
}
```

`learnings_compacted` is the re-distilled **full** learnings store from the first step — a wholesale replacement, emitted only when you actually merged or removed something (omit it otherwise). Each entry: `pattern` (the consolidated insight), `category` short tag, `merged_from` (the source `id`s you folded in; `[]` for a genuinely new synthesis). No `confidence` — AgentShore re-derives it from the folded sources. Anything you leave out of this set is dropped from the store, so carry forward every distinct insight.

Optionally include 0–3 `learnings` entries capturing ONLY durable, repo-specific patterns worth reusing in future plays (label taxonomy surprises, beads graph conventions, recurring backlog debt patterns) — grounded in what actually happened this run, not generic advice. Each entry: `pattern` (the insight), `confidence` 0.0–1.0 (default 0.5), `category` short tag (default `"general"`). Omit the field entirely if nothing reusable was learned. NEVER record secrets, tokens, or one-off details. Emit learnings as this top-level `learnings` array (the harvester only ingests this form).

`flagged_for_human` holds imperfections groom could not auto-resolve and escalated (each `{"issue": N, "reason": "<one line>"}`); emitting it keeps the play `success: true`. A clean graph with no changes is `success: true` with all empty lists — not an error. Imperfections you found and **resolved or flagged** also leave the play `success: true` — `success: false` is only for a hard execution error (snapshot won't run, a `bd`/`gh` mutation errored), never for backlog debt you detected and handled. Always emit the block — skipping causes `no valid result block` and discards the work. This is a single turn with no callback: never end it to "wait for the task notification" or watch a file/process. Finish your grooming pass now and emit the block.
