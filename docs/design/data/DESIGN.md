# Data Layer — Functional Design

## Responsibility

The data layer owns AgentShore's per-project SQLite database — the session-scoped RL/orchestration state store. It creates and migrates the schema, then persists sessions, plays, agents, GitHub cache rows, PR/review state, work claims, RL experience, archives, and learnings.

All database I/O uses `aiosqlite`. AgentShore does not use raw `sqlite3` in the orchestration path.

## Design Decisions

- **Single SQLite DB per project, single writer.** The whole orchestrator runs as one asyncio process, so a single embedded DB with one writer is sufficient and avoids a separate database service. There is no second writer to coordinate with.
- **WAL journaling with durability hardening.** `initialize()` sets `journal_mode=WAL`, `synchronous=FULL` (forces `F_FULLFSYNC` on macOS so the OS cannot defer the fsync under power management / screen-lock I/O throttling), and `wal_autocheckpoint=100` (vs the 1000-page default) to shrink the checkpointed-vs-main desync window. `foreign_keys=ON` is enforced.
- **Lock-race tolerance on quick stop→start.** `busy_timeout=5000` plus a bounded application-level retry around schema application absorb the transient writer-lock held by an outgoing session's sidecar that hasn't fully released the WAL lock. Both the schema script and the migrations are idempotent, so re-running is safe.
- **Crash-safe close via the Online Backup API.** `close()` snapshots the live connection with SQLite's Online Backup API to a sibling `.tmp` file, then `os.replace`s it over the main DB. This avoids the `wal_checkpoint(TRUNCATE)` failure mode where a concurrent external reader can truncate the main file to zero bytes; the atomic replace guarantees the main file is either the old good copy or the new snapshot, never empty. `snapshot_to()` (`VACUUM INTO`) provides additional consistent off-line copies for the snapshot ring.
- **Mixin-composed store.** `DataStore` (`store/core.py`) is assembled by inheriting one mixin per domain table-group (sessions, plays, agents, issues, pull_requests, branch_activity, reviews, work_claims, external_mutations, scope, feedback, learnings, trajectory, review_patterns, archive, rl, worktrees) over a shared base. Lifecycle and migration helpers live in `core.py` because they are tightly coupled to `initialize`. This keeps each table group's queries isolated without splitting the single connection.

## Three-Layer State Split

AgentShore deliberately keeps three stores with distinct authority; SQLite is only the third:

- **BEADS** is the canonical project graph (epics → stories → tasks). SQLite is *not* the source of truth for the beads hierarchy.
- **GitHub** is the human conversation surface (issues/PRs). The `github_issues` / `pull_requests` tables are session-scoped *caches/snapshots* of live GitHub state, not a replacement for it.
- **AgentShore SQLite** holds session-scoped RL and orchestration state.

Because repo + GitHub are authoritative, `reset_session_scoped_tables()` truncates session-scoped tables at the start of each new session (preserving the cross-session ones — see below), and bootstrap repopulates the caches from live GitHub. This prevents stale rows (e.g. prior-session `pull_requests.author_agent_id` stamps) from dead-locking code-review anti-confirmation.

## Schema

- **Namespace:** `agentshore_dev_v1` (validated at startup; a mismatched or namespace-less DB is rejected).
- **Schema version:** **4** (seeded by `schema.sql`, pinned by `tests/test_schema_fresh_db.py`).
- **Tables:** **22** (two meta tables + 20 domain tables), pinned by the same test.

The source of truth is `src/agentshore/data/schema.sql`. Pre-existing databases are carried forward by forward-only, individually idempotent migrations in `src/agentshore/data/migrations/`, applied after the baseline script during `initialize`.

### Table groups by purpose

| Group | Tables | Purpose |
|-------|--------|---------|
| Meta | `schema_info`, `schema_version` | Namespace marker and applied-version log used for startup validation and migration gating. |
| Session core | `sessions`, `plays`, `agents` | One row per run; completed play records (outcome, costs, reward, failure category, `bead_id`); agent lifecycle and aggregate token/cost/dispatch stats. |
| GitHub cache | `github_issues`, `pull_requests`, `branch_activity` | Session-scoped snapshots of issues, PRs (author identity, mergeability, AgentShore review verdict state), and last-implementer/commit per branch. |
| Concurrency & dispatch | `review_queue`, `work_claims`, `dispatch_replay`, `external_mutations` | PR review queue; idempotent resource claims that prevent duplicate concurrent assignment; replay/idempotency log of the most recent dispatched skill prompt; idempotency + audit log for GitHub and other external writes. |
| Scope | `scope_drift_log` | Non-blocking scope-drift observations (evidence log, not a gate). |
| RL & policy | `policy_checkpoints`, `rl_experience`, `trajectory_snapshots` | Checkpoint metadata + avg reward; PPO trajectory rows with behavior-policy metadata and masks; projected alignment/cost/remaining-play estimates. |
| Learning & feedback | `agent_handoffs`, `human_feedback`, `review_feedback_patterns` | Handoff metrics; feedback checkpoints + action taken; recurring code-review feedback patterns. Learnings are stored in `.agentshore/learnings.json` (JSON store), not in SQLite. |
| Archives | `session_archives` | Index rows for generated session archives. |
| Worktrees | `worktrees` | AgentShore-managed git worktree allocation and lifecycle for parallel agent work. |

## Important Invariants

- SQLite is not the source of truth for the beads hierarchy; GitHub cache rows are session-scoped snapshots, not a replacement for GitHub.
- `work_claims` is the concurrency guard for duplicate assignment; `review_queue`, `work_claims`, and `worktrees` use partial unique indexes to keep active claims/reviews/worktree-allocations unique while letting terminal rows accumulate.
- `rl_experience` rows used for PPO training must carry old log probability, value estimate, action mask, policy version, action-space version, config hash, and step index.
- `reset_session_scoped_tables()` must preserve the cross-session tables: `schema_info`, `schema_version`, `sessions`, `plays`, `rl_experience`, `session_archives`, and `review_feedback_patterns`.

## Source Pointers

- Schema: `src/agentshore/data/schema.sql`
- Store + lifecycle/migration: `src/agentshore/data/store/core.py` (with per-group mixins in `src/agentshore/data/store/mixins/`)
- Migrations: `src/agentshore/data/migrations/`
- Schema pin (version, table set, namespace): `tests/test_schema_fresh_db.py`
