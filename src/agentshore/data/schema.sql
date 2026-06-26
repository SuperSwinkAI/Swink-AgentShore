-- AgentShore database schema — agentshore_dev_v1
-- Applied by DataStore on first run; versioned via schema_info + schema_version tables.

PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS schema_info (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id          TEXT PRIMARY KEY,
    project_path        TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    status              TEXT NOT NULL DEFAULT 'running',
    -- Valid values: 'initializing' | 'running' | 'paused' | 'draining' | 'shutting_down' | 'completed'
    seed_path           TEXT,
    initial_issue_count INTEGER,
    total_cost          REAL NOT NULL DEFAULT 0.0,
    total_plays         INTEGER NOT NULL DEFAULT 0,
    scope_estimate      REAL,
    scope_remaining     REAL,
    final_alignment     REAL,
    last_issue_sync_at  TEXT
);

CREATE TABLE IF NOT EXISTS plays (
    play_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT NOT NULL REFERENCES sessions(session_id),
    play_type        TEXT NOT NULL,
    agent_id         TEXT,
    started_at       TEXT NOT NULL,
    ended_at         TEXT,
    duration_ms      INTEGER,
    success          INTEGER NOT NULL,
    partial          INTEGER NOT NULL DEFAULT 0,
    token_cost       INTEGER NOT NULL DEFAULT 0,
    dollar_cost      REAL NOT NULL DEFAULT 0.0,
    alignment_before REAL,
    alignment_after  REAL,
    alignment_delta  REAL,
    reward           REAL,
    failure_category TEXT,
    error            TEXT,
    artifacts        TEXT,
    bead_id          TEXT
);
CREATE INDEX IF NOT EXISTS idx_plays_session ON plays(session_id);
CREATE INDEX IF NOT EXISTS idx_plays_type ON plays(play_type);

CREATE TABLE IF NOT EXISTS agents (
    agent_id         TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL REFERENCES sessions(session_id),
    agent_type       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    terminated_at    TEXT,
    total_tokens     INTEGER NOT NULL DEFAULT 0,
    total_cost       REAL NOT NULL DEFAULT 0.0,
    tasks_completed  INTEGER NOT NULL DEFAULT 0,
    tasks_failed     INTEGER NOT NULL DEFAULT 0,
    -- desktop-j8b: persisted so the ESR play log can render a human-readable
    -- agent name (e.g. "Claude/large: Ember Raven") instead of a raw UUID.
    -- Nullable for old rows; new rows populate from the AgentHandle.
    model_tier       TEXT,
    display_name     TEXT,
    -- desktop-31h2: count of plays dispatched to this agent across the session,
    -- regardless of success/failure. Surfaced as `dispatch_share` in agent
    -- performance rollups so operators can spot fleet-utilisation imbalance
    -- (some agents getting 0 plays for long stretches while work is available).
    -- Distinct from tasks_completed/tasks_failed which gate on outcome.
    dispatch_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_agents_session ON agents(session_id);

CREATE TABLE IF NOT EXISTS github_issues (
    issue_number INTEGER NOT NULL,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id),
    title        TEXT NOT NULL,
    state        TEXT NOT NULL,
    priority     INTEGER,
    labels       TEXT,
    source       TEXT,
    url          TEXT,
    created_at   TEXT NOT NULL,
    closed_at    TEXT,
    github_author TEXT,
    PRIMARY KEY (issue_number, session_id)
);
CREATE INDEX IF NOT EXISTS idx_github_issues_session ON github_issues(session_id);
CREATE INDEX IF NOT EXISTS idx_github_issues_state ON github_issues(state);
CREATE INDEX IF NOT EXISTS idx_github_issues_session_state ON github_issues(session_id, state);

CREATE TABLE IF NOT EXISTS pull_requests (
    pr_number         INTEGER NOT NULL,
    session_id        TEXT NOT NULL REFERENCES sessions(session_id),
    issue_number      INTEGER,
    linked_issue_numbers TEXT,
    branch            TEXT,
    state             TEXT NOT NULL,
    title             TEXT NOT NULL DEFAULT '',
    url               TEXT,
    github_author     TEXT,
    labels            TEXT,
    review_decision   TEXT,
    status_check_summary TEXT,
    is_draft          INTEGER,
    author_agent_id   TEXT,
    author_agent_type TEXT,    -- "claude_code" | "codex" | "api_*" — used by code_review
    created_at        TEXT NOT NULL,
    merged_at         TEXT,
    head_sha          TEXT,    -- current PR HEAD commit SHA (headRefOid from GitHub)
    mergeable         TEXT,    -- "MERGEABLE" | "CONFLICTING" | "UNKNOWN" from GitHub API
    base_ref          TEXT,    -- baseRefName from GitHub API; NULL for legacy rows
    last_reviewed_sha TEXT,    -- HEAD SHA at time of last successful code_review play
    last_review_status TEXT,   -- "PASS" | "BLOCK" | NULL — AgentShore's verdict at last_reviewed_sha;
                               -- lapses automatically when head_sha advances past last_reviewed_sha
    PRIMARY KEY (pr_number, session_id)
);
CREATE INDEX IF NOT EXISTS idx_pull_requests_session ON pull_requests(session_id);
CREATE INDEX IF NOT EXISTS idx_pull_requests_author ON pull_requests(author_agent_id);
CREATE INDEX IF NOT EXISTS idx_pull_requests_author_type ON pull_requests(author_agent_type);

CREATE TABLE IF NOT EXISTS branch_activity (
    branch                    TEXT NOT NULL,
    session_id                 TEXT NOT NULL REFERENCES sessions(session_id),
    last_implementer_agent_id  TEXT,
    last_commit_sha            TEXT,
    updated_at                 TEXT NOT NULL,
    PRIMARY KEY (branch, session_id)
);
CREATE INDEX IF NOT EXISTS idx_branch_activity_session ON branch_activity(session_id);

CREATE TABLE IF NOT EXISTS review_queue (
    queue_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_number     INTEGER NOT NULL,
    session_id    TEXT NOT NULL REFERENCES sessions(session_id),
    author_label  TEXT,
    enqueued_at   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    claimed_by    TEXT,
    claimed_at    TEXT,
    completed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_queue_session ON review_queue(session_id);
CREATE INDEX IF NOT EXISTS idx_review_queue_status ON review_queue(status, session_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_queue_pending
    ON review_queue(pr_number, session_id) WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_queue_active
    ON review_queue(pr_number, session_id) WHERE status IN ('pending', 'claimed');

CREATE TABLE IF NOT EXISTS work_claims (
    claim_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_group_id       TEXT NOT NULL,
    session_id           TEXT NOT NULL REFERENCES sessions(session_id),
    play_type            TEXT NOT NULL,
    resource_key         TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'claimed',
    agent_id             TEXT,
    play_id              INTEGER REFERENCES plays(play_id),
    request_mutation_key TEXT,
    review_queue_id      INTEGER REFERENCES review_queue(queue_id),
    retry_attempts       INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    claimed_at           TEXT,
    started_at           TEXT,
    finished_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_work_claims_session ON work_claims(session_id);
CREATE INDEX IF NOT EXISTS idx_work_claims_group
    ON work_claims(session_id, claim_group_id);
CREATE INDEX IF NOT EXISTS idx_work_claims_resource
    ON work_claims(session_id, resource_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_claims_active_resource
    ON work_claims(session_id, resource_key)
    WHERE status IN ('queued', 'claimed', 'running', 'retrying');

CREATE TABLE IF NOT EXISTS dispatch_replay (
    session_id     TEXT NOT NULL REFERENCES sessions(session_id),
    claim_group_id TEXT NOT NULL,
    play_id        INTEGER NOT NULL,
    skill_name     TEXT NOT NULL,
    params_json    TEXT NOT NULL,
    prompt         TEXT NOT NULL,
    branch         TEXT,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (session_id, claim_group_id, play_id)
);
CREATE INDEX IF NOT EXISTS idx_dispatch_replay_session_group
    ON dispatch_replay(session_id, claim_group_id);

CREATE TABLE IF NOT EXISTS external_mutations (
    mutation_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(session_id),
    play_id         INTEGER REFERENCES plays(play_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    mutation_type   TEXT NOT NULL,
    target          TEXT NOT NULL,
    request_json    TEXT,
    response_json   TEXT,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mutations_session ON external_mutations(session_id);
CREATE INDEX IF NOT EXISTS idx_mutations_type ON external_mutations(mutation_type);

CREATE TABLE IF NOT EXISTS scope_drift_log (
    drift_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    play_id    INTEGER REFERENCES plays(play_id),
    artifact   TEXT NOT NULL,
    reason     TEXT,
    logged_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scope_drift_session ON scope_drift_log(session_id);

CREATE TABLE IF NOT EXISTS policy_checkpoints (
    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL REFERENCES sessions(session_id),
    created_at    TEXT NOT NULL,
    play_count    INTEGER NOT NULL,
    weights_path  TEXT NOT NULL,
    avg_reward    REAL
);

CREATE TABLE IF NOT EXISTS rl_experience (
    experience_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL REFERENCES sessions(session_id),
    play_id       INTEGER NOT NULL REFERENCES plays(play_id),
    state_vector  BLOB NOT NULL,
    action        INTEGER NOT NULL,
    reward        REAL NOT NULL,
    next_state    BLOB NOT NULL,
    done          INTEGER NOT NULL DEFAULT 0,
    old_log_prob  REAL,
    value_estimate REAL,
    action_mask   BLOB,
    mask_reason   TEXT,
    policy_version TEXT,
    action_space_version INTEGER NOT NULL DEFAULT 1,
    config_hash   TEXT,
    step_index    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_experience_session ON rl_experience(session_id);

CREATE TABLE IF NOT EXISTS agent_handoffs (
    handoff_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id                TEXT NOT NULL REFERENCES sessions(session_id),
    play_id                   INTEGER NOT NULL REFERENCES plays(play_id),
    source_agent_id           TEXT NOT NULL,
    target_agent_id           TEXT NOT NULL,
    context_tokens_transferred INTEGER NOT NULL DEFAULT 0,
    ramp_up_duration_ms       INTEGER,
    context_loss_estimate     REAL
);
CREATE INDEX IF NOT EXISTS idx_handoffs_session ON agent_handoffs(session_id);
CREATE INDEX IF NOT EXISTS idx_handoffs_play ON agent_handoffs(play_id);

CREATE TABLE IF NOT EXISTS trajectory_snapshots (
    snapshot_id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id                        TEXT NOT NULL REFERENCES sessions(session_id),
    play_id                           INTEGER NOT NULL REFERENCES plays(play_id),
    projected_alignment_at_budget_end REAL NOT NULL,
    estimated_remaining_plays         INTEGER NOT NULL,
    estimated_remaining_cost          REAL NOT NULL,
    created_at                        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trajectory_session ON trajectory_snapshots(session_id);

CREATE TABLE IF NOT EXISTS human_feedback (
    feedback_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL REFERENCES sessions(session_id),
    play_id       INTEGER NOT NULL REFERENCES plays(play_id),
    trigger       TEXT NOT NULL,
    feedback_text TEXT,
    action_taken  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_session ON human_feedback(session_id);

CREATE TABLE IF NOT EXISTS session_archives (
    archive_id      TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(session_id),
    archive_path    TEXT NOT NULL,
    total_cost      REAL NOT NULL,
    final_alignment REAL NOT NULL,
    total_plays     INTEGER NOT NULL,
    issues_closed   INTEGER NOT NULL DEFAULT 0,
    issues_created  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_archives_session ON session_archives(session_id);

CREATE TABLE IF NOT EXISTS review_feedback_patterns (
    pattern_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    play_id    INTEGER NOT NULL REFERENCES plays(play_id),
    pattern    TEXT NOT NULL,
    category   TEXT NOT NULL,
    frequency  INTEGER NOT NULL DEFAULT 1,
    injected   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_patterns_session ON review_feedback_patterns(session_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_patterns_unique
    ON review_feedback_patterns(session_id, pattern, category);

-- desktop-8qa5: AgentShore-managed git worktrees. One row per AgentShore-owned
-- worktree on disk; the partial unique indexes guard against concurrent
-- allocations for the same PR branch (or the same pre-branch key for
-- branch-creating plays). Status transitions:
--   active --> stale | reaping --> reaped
--   active --> failed (terminal)
CREATE TABLE IF NOT EXISTS worktrees (
    worktree_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id         TEXT    NOT NULL REFERENCES sessions(session_id),
    branch_name        TEXT,           -- NULL until rekey (branch-creating plays)
    pre_branch_key     TEXT,           -- e.g. "pickup-bd-123"; NULL for PR-scoped
    worktree_path      TEXT    NOT NULL,
    status             TEXT    NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active','stale','reaping','reaped','failed')),
    original_play_type TEXT    NOT NULL,
    head_sha           TEXT,
    base_ref           TEXT    NOT NULL,
    created_at         TEXT    NOT NULL,
    last_used_at       TEXT    NOT NULL,
    reaped_at          TEXT,
    failure_reason     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_worktrees_active_branch
    ON worktrees(session_id, branch_name)
    WHERE status IN ('active','reaping') AND branch_name IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_worktrees_active_prebranch
    ON worktrees(session_id, pre_branch_key)
    WHERE status IN ('active','reaping') AND pre_branch_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_worktrees_status
    ON worktrees(status, last_used_at);

-- Seed the schema namespace for startup validation.
INSERT OR IGNORE INTO schema_info (key, value)
VALUES ('schema_namespace', 'agentshore_dev_v1');

-- Current schema version for the agentshore_dev_v1 generation.
INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (5, datetime('now'));
