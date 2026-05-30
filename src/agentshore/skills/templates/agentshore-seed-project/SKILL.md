---
name: agentshore-seed-project
description: "Action slot 17 — Seed Project. Full project audit on session start and after cooldown: reconciles the seed file, PRD/HLD/design docs, beads graph, and GitHub issues into a consistent, complete backlog. Creates or repairs epics, stories, tasks, and GH issues as needed."
argument-hint: []
disable-model-invocation: true
allowed-tools: Read, Bash(bd:*, gh:*, git:*)
---

# agentshore-seed-project

Full beads↔GitHub reconciliation from `$AGENTSHORE_PROJECT_PATH` (bd lives in main repo). Runs at session start and after cooldown — this is a reconciliation, not one-time setup. It must leave the beads graph and GH issues consistent regardless of what prior sessions did. `$ARGUMENTS` is unused.

**Project docs are authoritative.** Read `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md` for conventions. The seed file (path in `context.json → params → seed_path`, else `test_ready.md`/`SEED.md`/`PLAN.md` at repo root) plus `docs/PRD.md`, `docs/design/PRD.md`, `docs/design/HLD.md`, and other `docs/**/*.md` are the requirement source — apply concrete deliverables, ignore vague advice.

**Reconciliation invariant** (must hold before `success: true`):
1. Every open GH issue has a beads task with `external_ref="gh-<N>"`.
2. Every open beads task has a non-null `external_ref`.
3. Every story is linked to an epic; every task to a story.
4. `bd list --all --json --limit 0` runs cleanly and returns ≥ 1 epic.
5. Every concrete requirement from seed + design docs is either verified-done or represented by an open GH issue + open beads task.
6. `artifacts` includes a `seed_audit` entry with numeric coverage counts and zero `unresolved` / `unknown` requirements.

Any failure → `success: false`, `error` = first violation.

**Pre-flight.** `bd --version` or stop with `success: false`. Read `$AGENTSHORE_PROJECT_PATH/.agentshore/context.json`. Read the seed file + design docs and build a mental model: epics (themes), stories (coherent sub-goals), concrete tasks (deliverables). Fetch GH issues both open and closed — closed are first-class evidence:

```
gh issue list --state open --json number,title,body,labels --limit 200
gh issue list --state closed --json number,title,labels --limit 200
```

Snapshot the beads graph **once**: `bd list --all --json --limit 0`, filter by `type` for epic/story/task. Read prior `seed_audit` artifacts from `.agentshore/contexts/*/play-*.json` (where `seed_project` returned `success: true`) — their cached `requirement_samples` + `scope_gap_issue_numbers` tell you which requirements are already mapped this session. **Do NOT re-create issues for requirements already mapped to a prior issue, even if that issue is now closed.** Use `git ls-files` and read source/tests/docs to confirm implementation evidence.

**Audit: desired vs. actual.** Inventory every concrete requirement (short title, source path/heading, exactly one status):

- **`verified_done`** — implementation evidence present. Any of these counts:
  (a) source/tests/docs show feature is built;
  (b) a **closed** GH issue whose title/body matches (closed issues = positive evidence of past completion intent — do not dismiss them);
  (c) a **closed** beads task matching (beads is canonical per CLAUDE.md; a closed bead is at least as strong as a closed issue);
  (d) prior seed_audit mapped this requirement to an issue (open OR closed).
- **`represented_open`** — an open GH issue and open beads task track remaining work.
- **`scope_gap`** — **no positive evidence under (a)/(b)/(c)/(d)** AND no open issue or bead covers it. Only when you can affirmatively show the work has not been started and no prior issue tracked it.
- **`unknown`** — too ambiguous to map. Ambiguity is a failure, not success.

Only `scope_gap` produces a new issue. Lean toward `verified_done` when a closed bead, closed issue, or prior seed_audit covers the requirement — the cost of a false phantom issue (multiplied across re-runs) is far higher than a missed requirement that `groom_backlog` / `refine_tasks` will surface later.

Then classify graph gaps: **A** missing/unlinked epics/stories; **B** beads tasks with no `external_ref`; **C** open GH issues with no matching bead; **D** scope gaps (no bead, no issue).

**Repair epics and stories (A).** Create missing or relink unlinked: `bd create epic "<title>" --description "<one-sentence summary>"`, `bd create story "<title>" --description "<summary>"`, `bd link <story-id> <epic-id>`. Cap 5 epics, 10 stories per epic. Reuse existing where they already cover the intended scope.

**Hard cap: ≤ 25 new GH issues per run.** Past 25, stop creating, set `success: false`, `error: "too_many_scope_gaps_detected: <count> requirements look unmet — human review required before backlog explosion"`. A healthy run on a reconciled project creates 0–5 issues; > 25 indicates a classification mistake.

**Create new work (B, D) — beads-canonical order.** Always check beads before GitHub. For each candidate gap, `bd search "<2-3 distinctive keywords>" --json`. Clear match → skip creation and reclassify: closed bead → `verified_done` (regression-only exception with explicit "this regressed" reasoning); open bead with `external_ref` → `represented_open`; open bead without `external_ref` → fall into gap B path. Only after beads dedup, back-check `gh issue list --search "<same keywords>" --state all --limit 20`. GH match (open or closed) wins: closed → `verified_done` skip; open → fall to gap C (Step 5) to attach a bead to the existing issue.

For **gap D (true scope gap)**, create the bead first; GitHub mirrors it. If GH creation fails halfway you get an unlinked bead (gap B for next run) — preferable to an orphan GH issue with no bead.

```
bd create task "<concise title>" --description "<what + why>"
gh issue create --title "<concise title>" --body "<what + why>\n\nTracked in beads as <bead-id>." --label "enhancement"
bd set-external-ref <bead-id> "gh-<issue_number>"
bd link <bead-id> <story-id>
```

For **gap B (unlinked bead)**: `gh issue create --title "<bead title>" --body "<bead description>\n\nTracked in beads as <bead-id>." --label "enhancement"`, then `bd set-external-ref <bead-id> "gh-<issue_number>"`. Record every new number in `issues_created`.

**Size routing at creation.** You don't size or decompose (that's `refine_tasks`); you route. After creating each issue, apply `agentshore/needs-refinement` if the source design section spans **≥ 3** deliverables — measured by ≥ 3 `### ` sub-headings OR ≥ 3 top-level `- ` bullets each starting with an action verb (Add/Build/Create/Implement/Render/Support/Wire/etc.). `gh issue edit <N> --add-label "agentshore/needs-refinement"`. Catches story-shaped requirements before `issue_pickup` grabs them.

**Beads tasks for orphan GH issues (C).** For each open GH issue with no matching bead: `bd create task "<issue title>" --description "Closes gh-<issue_number>" --external-ref "gh-<issue_number>"`, then `bd link <task-id> <story-id>` (create a story via Step 3 if none fits). Don't duplicate tasks already in the graph.

**Performance constraints (in-skill, non-negotiable):**
- **Snapshot once.** Call `bd list --all --json --limit 0` exactly once in Pre-flight; reuse the result across the audit, repair, and verify phases.
- **Work in-memory.** Parse the snapshot into a dict keyed by bead id; build the adjacency map from each bead's `dependencies` field. Never fan out per-bead `bd show <id>` calls — each is a ~100 ms `fork/exec/wait`; 200 beads = 20+ s of pure subprocess overhead and the pattern repeats every run (same class of bug as gh#529 batch-write).
- **Cap at 1000 issues / 1000 beads per scan.** If `bd list` is missing a field you'd need for traversal (e.g. `dependents`), file an upstream `bd` issue describing the gap and complete the play with whatever `bd list` does provide — never fan out to fill the hole.

**Verify.** Reuse the snapshot — do not re-fetch. Confirm expected epics each have ≥ 1 story; every open task has an `external_ref`; `gh issue list --state open --json number,title --limit 200` shows every open issue has a corresponding bead. Unclosable gaps → `issues_skipped`. Recompute the requirement inventory; any remaining `scope_gap` or `unknown` → `success: false`. If `scope_gaps_found > 0`, `scope_gap_issue_numbers` must include an issue number for every gap created or linked this play — missing entries are a failure.

**Forbidden mutations:**
- Never touch `.github/workflows/**` or any CI config.
- Never close or reopen GitHub issues.
- Never delete existing beads — only create or update.
- Never call `git worktree add/remove/prune` — AgentShore owns lifecycle.
- Never fan out per-bead `bd show` calls in place of the single `bd list` snapshot.

**Report — one fenced JSON block, nothing else:**

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

`bd` unavailable or any creation step failing unrecoverably → `success: false` with a concise `error`. Always emit the block — skipping causes `no valid result block` and discards the work.
