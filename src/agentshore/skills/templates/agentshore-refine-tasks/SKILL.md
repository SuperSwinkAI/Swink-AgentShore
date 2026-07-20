---
name: agentshore-refine-tasks
description: "Action slot 12 — Refine Task Breakdown. Scans open GitHub issues, estimates scope, decomposes oversized issues into linked sub-issues, and re-prioritizes based on dependencies. Use when unrefined issues exist in the backlog."
argument-hint: []
disable-model-invocation: true
allowed-tools: Read, Bash(gh:*, git fetch:*, git remote:*, git grep:*, git log:*, git status:*, git diff:*, git show:*)
---

# agentshore-refine-tasks

Decompose oversized open issues into linked sub-issues and re-prioritize by dependency. Self-contained — no `$ARGUMENTS`.

**Project docs are authoritative.** Read `$AGENTSHORE_PROJECT_PATH/.agentshore/context.json` (repo, owner, `priority_scheme`, learnings on past sizing mistakes) and `CLAUDE.md`/`AGENTS.md`/`CONTRIBUTING.md` for project structure and conventions. Resolve the repo from context or `git remote get-url origin`. `git fetch origin`.

**Fetch the working set:**

```
gh issue list --state open --limit 200 --json number,title,labels,body,assignees
```

Process only issues still carrying `agentshore/needs-refinement` and **not** `agentshore/refined` — anything without the gate (or already marked refined) is already sized. If none, exit with `success: true`, empty artifacts, `error: "all issues already refined"`. (Selection already excludes refined issues; this is a defensive double-check.)

**Choose decomposition source (one per issue).** If the issue has an `AGENTSHORE_IMPLEMENTATION_PLAN` comment with **≥ 3** `### Task N` headers, it's canonical — use the plan as the map (`decomposition_source: "plan"`). Otherwise estimate from the codebase: read the body, `git grep`/list implied files, and size as **S** (<15 min, 1–2 files), **M** (15–30 min, 2–3 files), **L** (30–60 min, 3–5 files), **XL** (>60 min, >5 files). Record `decomposition_source: "estimate"`. Decompose if the path was plan-structured, or if the estimate is L/XL. Otherwise skip to the label swap.

**Duplicate-children guard:** `gh issue list --search "Parent: #<parent_number>" --json number,title,state`. If sub-issues already exist, skip decomposition and go straight to the label swap.

**Decompose:**
- **Plan path:** one child per `### Task N` in declared order — do not invent, merge, or reorder. Title `<parent title>: <task name>`. The task block's `- [ ]` checkboxes become the child's acceptance criteria verbatim; pull anything from the parent's `## Acceptance Criteria` that clearly belongs to this task. Copy the parent's `## Validation` commands that apply. Linear `depends on #<previous_child>` chain; first child has no `depends on`.
- **Estimate path:** 2–5 **tracer-bullet vertical slices**, each cutting end-to-end (schema → API → UI → tests). Prefer many thin slices over few thick ones. Each slice S or M, owns its tests. Express edges explicitly with `depends on #N` — `agentshore-issue-pickup` and `agentshore-calibrate-alignment` consume that ordering.

**Child issue body (both paths) must include:** `Parent: #<N>`; one paragraph describing the end-to-end behavior delivered by the slice (no per-layer breakdown); acceptance criteria as `- [ ]` checkboxes; likely files/areas; likely tests; exact validation commands where inferable; `depends on #N` edges.

Create with `gh issue create --label "agentshore/intake"` (sub-issues are pre-scoped — do **not** carry `agentshore/needs-refinement`). Example:

```
gh issue create --title "<parent title>: <sub-task>" \
  --body "Parent: #<N>

## Acceptance Criteria
- [ ] <criterion>

## Likely Files
- <path>

## Tests
- <test path>

## Validation
- <command>" \
  --label "agentshore/intake"
```

Append a `## Sub-tasks` checklist to the parent body without clobbering existing content:

```
ORIGINAL=$(gh issue view <parent> --json body --jq .body)
gh issue edit <parent> --body "$ORIGINAL

## Sub-tasks
- [ ] #<child1>
- [ ] #<child2>"
```

**Label swap (every processed issue):** `gh issue edit <N> --remove-label "agentshore/needs-refinement" --add-label "agentshore/refined"`. The `agentshore/refined` mark is mandatory — it removes the issue from refine selection so an agent is never re-dispatched to no-op on it (re-armed only when groom/design-audit later removes the label). For decomposed parents additionally `--remove-label "agentshore/planned" --add-label "agentshore/decomposed"` so `issue_pickup` doesn't grab the now-empty parent — it stays open as a tracker and closes when its children's PRs close. Sized S/M leaves from the estimate path keep their `priority/*`/`size/*` labels plus `agentshore/refined` and become pickup-eligible (`agentshore/refined` does not block pickup).

**Re-prioritize.** Identify dependencies (explicit `depends on #N`/`blocked by #N` in bodies; implicit parent-child) and apply priority labels where missing: `gh issue edit <N> --add-label "priority/<critical|high|medium|low>"`. No-blocker + small = higher priority.

**Validate:** every processed issue no longer carries `agentshore/needs-refinement` and now carries `agentshore/refined`; decomposed parents link to children; every new sub-issue has `agentshore/intake`, references its parent, and lacks `agentshore/needs-refinement`; no duplicate sub-issues.

**Forbidden — this play runs in the main checkout:**
- AgentShore runs you in the project's **main working tree on the target branch**, not an isolated worktree. You decompose issues and edit GitHub labels only — you must **never** touch the working tree or move git refs.
- **Never** `git checkout`/`git switch`/`git checkout -b`/`git switch -c`/`git branch`/`git worktree add`/`git reset`/`git clean`/`git stash`/`git commit`/`git merge`/`git rebase`/`git push`. Creating or switching a branch here moves the **main checkout's HEAD** onto a feature branch and wedges the orchestrator — the trunk-dispatch guard cannot restore a branch-switched HEAD left with untracked work. Git is **read-only** in this play: `git fetch`, `git remote`, `git grep`, `git log`, `git status`, `git diff`, `git show` only.
- Never create/edit/restore/delete `.github/workflows/**`, `.github/actions/**`, `.gitlab-ci.yml`, `.circleci/**`, `azure-pipelines.yml`, `Jenkinsfile`, `bitbucket-pipelines.yml`, or tests asserting their existence.
- Leave the working tree clean. If you need scratch/working files, write them under `tmp/` at the project root (gitignored, never treated as a dirty-trunk blocker) — never loose at the repo root.

**Report — one fenced JSON block:**

```json
{
  "success": true,
  "artifacts": [
    {"issue": 10, "title": "Implement auth flow", "estimate": "XL", "files_touched": 7, "action": "decomposed", "children": [11, 12, 13, 14]},
    {"issue": 20, "title": "Fix login button", "estimate": "S", "files_touched": 1, "action": "kept"}
  ],
  "issues_created": [
    {"number": 11, "title": "Implement auth flow: token generation", "url": "..."},
    {"number": 12, "title": "Implement auth flow: session management", "url": "..."}
  ],
  "error": null
}
```

If all issues were already refined: `success: true`, empty artifacts, `error: "all issues already refined"`. On step failure: `success: false` with populated `error`. Never omit the result block.
