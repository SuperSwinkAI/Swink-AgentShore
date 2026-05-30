---
name: agentshore-groom-backlog
description: "Action slot 16 — Groom Backlog. Audits the beads project graph against current GitHub issues, closes stale or orphaned beads, corrects mislabeled items, re-links beads to their parent epics, reconciles the issues↔beads graph so every open GH issue has a beads task and every beads task has a GH issue, and flags oversized issues for refinement with a proposed decomposition."
disable-model-invocation: true
allowed-tools: Read, Bash(bd:*, gh:*, git:*)
---

# agentshore-groom-backlog

You are a AgentShore skill agent. AgentShore is a pure RL scheduler — not an LLM.
It invoked you with parameters in `$ARGUMENTS`.

## Inputs

`$ARGUMENTS` — not used by this skill; leave empty.

## Reconciliation Invariant (success condition)

Before returning `"success": true`, all of these must hold:

1. Every open GH issue appears in the beads graph as a task with `external_ref="gh-<N>"`.
2. Every open beads task has a non-null `external_ref`.
3. Every story is linked to an epic. Every task is linked to a story.
4. `bd list --all --json --limit 0` runs cleanly and returns at least one epic bead.

If any condition fails, set `"success": false` and populate `"error"` with the first violation.

## Step 1 — Pre-flight

1. Read `.agentshore/context.json`. Extract `repo`, `owner`, `session_id`.
2. Read project conventions (`CLAUDE.md`, `AGENTS.md`, etc.) if present.
3. Confirm beads is initialised by checking that `.beads/` exists. If not, stop with
   `"success": false` and `"error": "beads not initialised"`.
4. Snapshot the current beads graph before making any changes:
   ```
   bd list --all --json --limit 0
   ```
   Filter by `type` and record the derived epic/story/task summary as `epics_before`.

## Step 2 — Load current state

```
bd list --all --json --limit 0  # → all_beads; derive open_beads and parent-closed checks
gh issue list --state open  --limit 200 --json number,title,state,labels,body
gh issue list --state closed --limit 100 --json number,title,state,labels
```

## Step 3 — Classify open beads

For each bead in `open_beads`, identify which of these apply:

- **Stale:** `external_ref=gh-N` and issue N is closed or absent from open issues. → close.
- **Orphaned:** parent epic is closed or missing from `all_beads`. → relink or close.
- **Mislabeled:** clear-cut type error only (task spanning multiple work-streams → epic; epic
  with one outcome and no children → task). Do not guess ambiguous cases.
- **Duplicate:** two open beads share the same `external_ref`. → close all but the newest.

Record all findings in `grooming_plan`.

## Step 4 — Apply changes

### 4a — Close stale and duplicate beads
```
bd close <bead_id>
```

### 4b — Relink orphaned beads
```
bd link <bead_id> --parent <correct_epic_id>
```
If no correct parent exists, close the bead instead.

### 4c — Fix mislabeled beads
```
bd update <bead_id> --type <correct_type>
```

### 4d — Reconcile issues ↔ beads (both directions)

**For each open beads task with no `external_ref`:**

1. Search for an existing open GH issue by title:
   ```
   gh issue list --search "<bead title>" --state open --limit 5 --json number,title,state
   ```
2. **Exact match** (one result, title matches case-insensitively, no other bead holds that ref):
   ```
   bd update <bead_id> --external-ref "gh-<N>"
   ```
3. **No match or ambiguous:** create a new GH issue and link it:
   ```
   gh issue create --title "<bead title>" --body "<description>" --label "enhancement"
   bd update <bead_id> --external-ref "gh-<new_N>"
   ```
   Record in `issues_created`.

**For each open GH issue with no beads task:**
```
bd create task "<issue title>" --description "Closes gh-<N>" --external-ref "gh-<N>"
bd link <task-id> <most-appropriate-story-id>
```
If no story exists, create one first and link it to the relevant epic.

## Step 5 — Size audit & flag oversized issues for refinement

After reconciliation, scan all open GH issues for oversized scope. The goal is
**not** to decompose — only to flag. Issues you flag here become eligible for
the `refine_tasks` play (action slot 12, tiers `{medium, large}`), which
performs its own scope estimation and decomposition. The proposal comment you
leave is advisory; `refine_tasks` does not consume it.

### Signals

An issue is **oversized** if **at least 2** of these signals fire:

1. **Body length > 4000 characters.**
2. **≥ 3 non-standard `##` headings.** Standard headings to *exclude* from the
   count: `Source references`, `Why current evidence is insufficient`,
   `Acceptance criteria`, `Likely source/test areas`, `Scope`, `Blocked by`,
   `Tracked by`.
3. **Acceptance criteria contains ≥ 5 unchecked `- [ ]` items** (count anywhere
   in the body, not just under an `## Acceptance criteria` heading).
4. **Labeled `agentshore/epic` but has no child issues** — i.e. no other open
   issue's body matches `Decomposed from #<N>`, `Sub-task of #<N>`, or
   `Parent: #<N>` pointing back to this one.

### Skip rules

Do **not** flag an issue if any of these are true:

- Already labeled `agentshore/needs-refinement` (don't re-flag).
- Body contains `Decomposed from #` or `Parent: #` (this issue is itself a child).
- Issue is already covered by an open PR (already in flight; refinement is moot).

### Hard cap

Flag **at most 3 oversized issues per groom run.** If more candidates exist,
pick by (highest signal count, then oldest issue number).

### For each flagged issue

1. Build a proposed decomposition from the issue's existing structure:
   - Prefer `##` section headings as children (one child per section, excluding
     the standard-section list above).
   - If the trigger was signal 3 (criteria items), group related `- [ ]` items
     into 2–5 children instead.
   - Each proposed child needs: a short title (5–10 words) and a 1–2 sentence
     scope summary. Do **not** invent acceptance criteria the parent doesn't
     already have — the proposal is structural, not creative.

2. Write the proposal to a temp file and post it as a comment. The comment
   body must start with the literal token `AGENTSHORE_GROOM_DECOMPOSITION_PROPOSAL`
   so future skills/humans can detect it:
   ```
   cat > /tmp/groom-decomp-<N>.md <<'EOF'
   AGENTSHORE_GROOM_DECOMPOSITION_PROPOSAL

   This issue triggered <K> of 4 oversized signals: <comma-separated signal names>.

   ## Proposed sub-tasks

   1. **<title>** — <scope summary>
   2. **<title>** — <scope summary>
   ...

   Note: this is an advisory proposal from `agentshore-groom-backlog`. The
   `refine_tasks` play will perform its own scope estimation and decide the
   final decomposition.
   EOF
   gh issue comment <N> --body-file /tmp/groom-decomp-<N>.md
   ```

3. Apply the gate label so `refine_tasks` picks it up and `issue_pickup` /
   `write_implementation_plan` skip it:
   ```
   gh issue edit <N> --add-label "agentshore/needs-refinement"
   ```

4. Record the flag in `issues_flagged_for_refinement` (see Result block) as
   `{"issue": <N>, "signals": [<list of signal names that fired, e.g. "body_length_4500", "headings_5", "criteria_items_8", "epic_no_children">], "proposed_children": <int>}`.

## Step 6 — Verify the invariant

1. Re-fetch: `bd list --all --json --limit 0` and `gh issue list --state open --limit 200 --json number,title,labels`.
2. Check each invariant condition. Record any violations in `verification_failures`.
3. For every issue listed in `issues_flagged_for_refinement`, confirm the
   `agentshore/needs-refinement` label is present on the re-fetched issue. If
   missing, record the issue number in `verification_failures`.
4. Derive the final epic/story/task summary from the all-beads snapshot → `epics_after`.
5. If `verification_failures` is non-empty, set `"success": false`.

## Result

```json
{
  "success": true,
  "artifacts": [],
  "beads_closed": [],
  "beads_relinked": [],
  "beads_relabeled": [],
  "duplicates_removed": [],
  "issues_created": [],
  "issues_flagged_for_refinement": [],
  "ambiguous_links_resolved": [],
  "epics_before": [],
  "epics_after": [],
  "verification_failures": [],
  "error": null
}
```

A clean graph with no changes is `"success": true` with all empty lists — not an error.
If any step fails unrecoverably, set `"success": false` and populate `"error"`.
Do not omit the result block under any circumstances.
