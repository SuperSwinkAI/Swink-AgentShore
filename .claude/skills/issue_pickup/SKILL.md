---
name: issue_pickup
description: Auto-implement up to 5 open GitHub issues in parallel. Selects the top priority issues, spawns isolated worktree agents for each, and produces one PR per issue. Use this skill whenever the user wants to pick up work, implement issues, work on the next tasks, or automate issue-driven development. Also triggers for scheduled/autonomous runs that need to select and implement work.
allowed-tools: Agent, Bash, Read, Write, Edit, Grep, Glob, TaskCreate, TaskUpdate, WebFetch
argument-hint: [optional: specific issue numbers, e.g. "42 57 63"]
---

# Auto-Implement: Pick Up and Implement Up to 5 Issues in Parallel

You are an orchestrator agent running on the SuperSwinkAI/Swink-AgentShore repository.
Select up to 5 of the highest-priority open GitHub issues, then spawn one isolated
worktree agent per issue to implement, test, and push each as a separate PR.

## How This Works

You (the orchestrator) handle issue selection, claiming, and coordination.
Each worker agent gets a single issue and runs the full implement-test-push cycle
independently in its own git worktree. Agents can read, write, and run tests
concurrently — no build serialization is needed.

## Task Tracking

Create a TODO list at the start of every run. Update items as you go:

- [ ] Check for duplicate/stale runs
- [ ] Fetch and rank all open issues
- [ ] Select up to 5 issues (verify authorship, skip ineligible)
- [ ] Claim all selected issues (add `in-progress` label)
- [ ] Spawn worker agents (one per issue, each in its own worktree)
- [ ] Monitor agent completion
- [ ] Report results summary
- [ ] Cleanup any failed runs

## Step 0: Guard Against Duplicate Runs

Check for issues with the `in-progress` label. If any exist and the label was added
>2 hours ago, remove it and comment "Automated agent timed out, releasing issue."
If added recently, another run is active for that issue — exclude it from selection
but don't stop entirely (other issues may still be available).

## Step 1: Select Up to 5 Issues

Fetch all open issues with labels, author, assignees, and body.

### Priority order

1. Issues labeled `critical` or `P0`
2. Issues labeled `bug` or `P1`
3. Issues labeled `enhancement` or `P2`
4. Issues labeled `refactor`, `chore`, or `style`, oldest first
5. All others, oldest first

### Skip issues that:
- Have an assignee (human is working on it)
- Are labeled `wontfix`, `duplicate`, `blocked`, or `in-progress`
- Already have a linked open PR
- Touch the RL training loop, PPO policy, or SQLite schema in ways that could
  corrupt accumulated experience data (flag for human review instead)

### Verify authorship

Accepted authors: `example-user`, `claude-code`, `claude[bot]`, `codex`, `codex[bot]`,
`dependabot[bot]`, and repo collaborators. Skip issues from unrecognized authors.

### Conflict detection

Before finalizing the set, check whether any selected issues touch overlapping
files or modules. If overlap is detected, keep only the higher-priority one and
backfill from the queue.

Select up to 5 eligible issues. Fewer is fine. No actionable issues? Report and stop.

## Step 2: Claim All Selected Issues

For each selected issue, add the `in-progress` label and comment that the
automated agent is picking it up, including which aspect will be addressed.

## Step 3: Spawn Worker Agents

Spawn one Agent per issue, **all in the same message** so they run concurrently.
Each agent runs in worktree isolation (`isolation: "worktree"`).

Give each worker agent the full context it needs. Here is the prompt template
(fill in the specifics for each issue):

```
You are implementing GitHub issue #{number} for the SuperSwinkAI/Swink-AgentShore repo.

## Issue
Title: {title}
Labels: {labels}
Body: {body}

## Your task
Implement the full scope described in the issue body.

## Project context
- Python 3.12+, `src/agentshore/` layout (hatch build backend)
- `from __future__ import annotations` in every Python module
- Ruff for linting/formatting (line length 100), mypy strict mode
- asyncio for all I/O — no blocking calls in the core loop
- SQLite via aiosqlite (never raw sqlite3)
- structlog → NDJSON for all logging
- Dashboard: TypeScript + Vite in `dashboard/`
- CLAUDE.md in the repo root has full architecture and convention details

## Instructions

1. Read CLAUDE.md and any relevant source files before writing code.

2. Create branch: `fix/{number}-{short-slug}` for bugs,
   `feat/{number}-{short-slug}` for enhancements,
   `refactor/{number}-{short-slug}` for refactors.

3. Implement the change. Follow existing patterns — don't introduce new
   abstractions unless the issue explicitly requires them.

4. Run the full check suite sequentially. Fix any failures before pushing:
   ```
   uv run ruff format src/ tests/
   uv run ruff check src/ tests/
   uv run mypy src/
   uv run pytest tests/ -v
   ```
   If the issue touches the dashboard, also run:
   ```
   cd dashboard && npx tsc --noEmit
   ```
   On failure: read output, fix, retry (max 3 attempts). If still failing,
   report the error — do NOT push broken code.

5. Commit with a conventional message:
   - Bug: `fix: <imperative summary>\n\nCloses #{number}`
   - Feature: `feat: <imperative summary>\n\nCloses #{number}`
   - Refactor: `refactor: <imperative summary>\n\nCloses #{number}`
   - Chore: `chore: <imperative summary>\n\nCloses #{number}`
   Always append: `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>`

6. Push: `git push -u origin <branch-name>`

7. Create PR targeting main:
   gh pr create --title "<title>" --body "$(cat <<'PREOF'
   ## Summary
   <what and why>

   ## Changes
   <bullet list of what changed>

   ## Test plan
   - [ ] `uv run pytest tests/ -v` passes
   - [ ] `uv run ruff check src/ tests/` clean
   - [ ] `uv run mypy src/` clean
   - [ ] Dashboard `npx tsc --noEmit` clean (if applicable)

   Closes #{number}
   🤖 Generated with [Claude Code](https://claude.com/claude-code)
   PREOF
   )"

## Safety rails
- **Never** run `agentshore start`, `agentshore init`, or any `agentshore` CLI subcommand
  that starts a server or agent loop — these leave orphaned processes.
- **Never** run AgentShore against this repository as a project. Only example-repo.
- Never force-push or use --no-verify
- Never modify .github/, CLAUDE.md, CODEOWNERS
- Max diff: 1500 lines
- Regression test required for every behavioral change
- Always create a PR, never merge directly to main
- Never modify the SQLite schema (schema.sql) without explicit human approval
- Never touch src/agentshore/rl/ training logic without explicit human approval
```

## Step 4: Monitor and Report

As each worker agent completes, note its result (success/failure, PR URL if created).
After all agents finish:

1. **Successful agents**: Comment on each issue with PR link. Remove `in-progress` label.

2. **Failed agents**: Comment with which step failed and why. Remove `in-progress` label.
   Delete the remote branch if no PR was created.

3. **Summary**: Report which issues were picked up, which PRs were created, and any failures.

## Safety Rails (Orchestrator)

- **One issue per agent.** Each worker handles exactly one issue.
- **Up to 5 agents max.** Never spawn more than 5 concurrent workers.
- **Never run `agentshore` CLI commands** — no `agentshore start`, `agentshore dashboard`, etc.
- **Never run AgentShore against this repo** — only against example-repo or another sandbox.
- **Never force-push** or use `--no-verify`.
- **Never modify** `.github/`, `CLAUDE.md`, `CODEOWNERS`.
- **Max diff: 1500 lines per PR.**
- **Regression test required** for every behavioral change. No test = no merge.
- **Always create a PR** — never merge directly to `main`.
- **Conflict detection.** Don't assign overlapping issues to concurrent agents.
- **Schema and RL policy changes** require human approval — flag and skip if an issue
  requires them.
