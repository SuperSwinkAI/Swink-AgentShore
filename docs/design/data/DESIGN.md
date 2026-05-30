# Data Layer — Functional Design

## Responsibility

The data layer manages AgentShore's project/session SQLite database:

- Create and migrate schema.
- Persist sessions, plays, agents, GitHub cache rows, PR state, work claims, RL experience, reports, archives, and learnings.

All database I/O uses `aiosqlite`. AgentShore does not use raw `sqlite3` in the orchestration path.

## Schema

Current schema version: **13**.

The current schema has **21** tables:

| Table | Purpose |
|-------|---------|
| `schema_version` | Applied schema versions. |
| `sessions` | One row per AgentShore run. Tracks project path, state, cost, play count, seed path, and final alignment. |
| `plays` | Completed play records with outcome, costs, artifacts, reward, failure category, and optional `bead_id`. |
| `agents` | Agent lifecycle and aggregate token/cost/task stats. |
| `github_issues` | Session-local GitHub issue cache, including labels, source, URL, closed time, and state. |
| `pull_requests` | Session-local PR cache, author identity, review status, mergeability, and AgentShore review verdict state. |
| `branch_activity` | Last implementer and commit SHA by branch. |
| `review_queue` | PR review queue with pending/claimed/completed states. |
| `work_claims` | Idempotent resource claims for issue/PR/session work. Prevents duplicate concurrent assignment. |
| `dispatch_replay` | Per-(session, claim_group, play) record of the most recent dispatched skill prompt — used as an idempotency / replay log for skill-backed plays. |
| `external_mutations` | Idempotency and audit log for GitHub and other external writes. |
| `scope_drift_log` | Non-blocking scope-drift observations. |
| `policy_checkpoints` | Policy checkpoint metadata and average reward. |
| `rl_experience` | PPO trajectory rows, including behavior-policy metadata and masks. |
| `agent_handoffs` | Agent-to-agent handoff metrics. |
| `trajectory_snapshots` | Projected alignment/cost/remaining-play estimates. |
| `human_feedback` | Feedback checkpoint records and actions taken. |
| `session_learnings` | Audit trail for learned codebase patterns. |
| `session_archives` | Archive index rows for generated session archives. |
| `review_feedback_patterns` | Recurring code-review feedback patterns. |
| `worktrees` | Git worktree allocation and lifecycle for parallel agent work. |

The schema source of truth is `src/agentshore/data/schema.sql`; compatibility checks and opportunistic table/column creation live in `src/agentshore/data/store/core.py` (alongside `DataStore.initialize`) and `src/agentshore/data/migrations/`.

## Important Invariants

- SQLite is not the source of truth for the beads hierarchy.
- GitHub issue and PR cache rows are session-scoped snapshots, not a replacement for GitHub.
- `work_claims` is the concurrency guard for duplicate assignment.
- `rl_experience` rows used for PPO training must include old log probability, value estimate, action mask, policy version, action-space version, config hash, and step index.
- `review_queue` and `work_claims` use partial unique indexes to keep active review and resource claims unique.
