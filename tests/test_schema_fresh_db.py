"""Verify a fresh DataStore produces the agentshore_dev_v1 schema.

Covers the agentshore_dev_v1 schema baseline: namespace, version, tables,
idempotency, legacy rejection, and key columns.
"""

from __future__ import annotations

import sqlite3

import pytest

from agentshore.data.store import DataStore

EXPECTED_TABLES = frozenset(
    {
        "schema_info",
        "schema_version",
        "sessions",
        "plays",
        "agents",
        "github_issues",
        "pull_requests",
        "branch_activity",
        "review_queue",
        "work_claims",
        "dispatch_replay",
        "external_mutations",
        "scope_drift_log",
        "policy_checkpoints",
        "rl_experience",
        "agent_handoffs",
        "trajectory_snapshots",
        "human_feedback",
        "session_learnings",
        "session_archives",
        "review_feedback_patterns",
        "worktrees",
    }
)


@pytest.mark.asyncio
async def test_fresh_db_has_agentshore_dev_v1_namespace(tmp_path) -> None:
    """schema_info stores ('schema_namespace', 'agentshore_dev_v1')."""
    db_path = tmp_path / "fresh.db"
    store = DataStore(db_path)
    await store.initialize()
    try:
        async with store._conn.execute(
            "SELECT value FROM schema_info WHERE key = 'schema_namespace'"
        ) as cursor:
            row = await cursor.fetchone()
    finally:
        await store.close()

    assert row is not None
    assert row["value"] == "agentshore_dev_v1"


@pytest.mark.asyncio
async def test_fresh_db_schema_version_is_4(tmp_path) -> None:
    """schema_version table reflects the current version (4)."""
    db_path = tmp_path / "fresh.db"
    store = DataStore(db_path)
    await store.initialize()
    try:
        async with store._conn.execute("SELECT MAX(version) AS v FROM schema_version") as cursor:
            row = await cursor.fetchone()
    finally:
        await store.close()

    assert row is not None
    assert int(row["v"]) == 4


@pytest.mark.asyncio
async def test_fresh_db_has_all_expected_tables(tmp_path) -> None:
    """All 22 tables from schema.sql exist after initialize."""
    db_path = tmp_path / "fresh.db"
    store = DataStore(db_path)
    await store.initialize()
    try:
        async with store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ) as cursor:
            tables = {row["name"] for row in await cursor.fetchall()}
    finally:
        await store.close()

    assert tables == EXPECTED_TABLES


@pytest.mark.asyncio
async def test_fresh_db_is_idempotent(tmp_path) -> None:
    """Re-initializing the same DB file does not error."""
    db_path = tmp_path / "idem.db"
    store1 = DataStore(db_path)
    await store1.initialize()
    await store1.close()

    store2 = DataStore(db_path)
    await store2.initialize()
    await store2.close()


@pytest.mark.asyncio
async def test_key_columns_exist(tmp_path) -> None:
    """Spot-check important columns exist on fresh DB."""
    db_path = tmp_path / "cols.db"
    store = DataStore(db_path)
    await store.initialize()
    try:
        async with store._conn.execute("PRAGMA table_info(agents)") as cursor:
            agent_cols = {row["name"] for row in await cursor.fetchall()}
        async with store._conn.execute("PRAGMA table_info(sessions)") as cursor:
            session_cols = {row["name"] for row in await cursor.fetchall()}
        async with store._conn.execute("PRAGMA table_info(worktrees)") as cursor:
            wt_cols = {row["name"] for row in await cursor.fetchall()}
    finally:
        await store.close()

    assert "dispatch_count" in agent_cols
    assert "model_tier" in agent_cols
    assert "display_name" in agent_cols
    assert "last_issue_sync_at" in session_cols
    assert "worktree_id" in wt_cols
    assert "failure_reason" in wt_cols


@pytest.mark.asyncio
async def test_increment_agent_dispatch_count_persists(tmp_path) -> None:
    """``increment_agent_dispatch_count`` increments the column durably."""
    from agentshore.data.store import AgentRecord, SessionRecord

    db_path = tmp_path / "increment.db"
    store = DataStore(db_path)
    await store.initialize()
    try:
        await store.create_session(
            SessionRecord(
                session_id="s1",
                project_path="/tmp",
                started_at="2026-01-01T00:00:00+00:00",
            )
        )
        await store.register_agent(
            AgentRecord(
                agent_id="a1",
                session_id="s1",
                agent_type="claude_code",
                created_at="2026-01-01T00:00:00+00:00",
            )
        )
        await store.increment_agent_dispatch_count("a1")
        await store.increment_agent_dispatch_count("a1")
        await store.increment_agent_dispatch_count("a1")

        agents = await store.get_agents("s1")
    finally:
        await store.close()

    assert len(agents) == 1
    assert agents[0].dispatch_count == 3


@pytest.mark.asyncio
async def test_partial_unique_index_enforces_active_only(tmp_path) -> None:
    """Active rows on the same branch collide; ``reaped`` rows do not."""
    db_path = tmp_path / "indextest.db"
    store = DataStore(db_path)
    await store.initialize()
    try:
        await store._conn.execute(
            "INSERT INTO sessions (session_id, project_path, started_at) "
            "VALUES ('s', '/tmp', '2026-01-01T00:00:00+00:00')"
        )
        await store._conn.execute(
            """
            INSERT INTO worktrees (
                session_id, branch_name, pre_branch_key, worktree_path,
                status, original_play_type, base_ref, created_at, last_used_at
            ) VALUES ('s', 'b', NULL, '/p1', 'active', 'code_review',
                      'origin/b', '2026-01-01', '2026-01-01')
            """
        )
        await store._conn.commit()

        # Second active row on same branch must fail.
        try:
            await store._conn.execute(
                """
                INSERT INTO worktrees (
                    session_id, branch_name, pre_branch_key, worktree_path,
                    status, original_play_type, base_ref, created_at, last_used_at
                ) VALUES ('s', 'b', NULL, '/p2', 'active', 'code_review',
                          'origin/b', '2026-01-01', '2026-01-01')
                """
            )
            await store._conn.commit()
            raise AssertionError("expected IntegrityError on duplicate active branch")
        except sqlite3.IntegrityError:
            await store._conn.rollback()

        # Reap the first; second active insert now succeeds.
        await store._conn.execute(
            "UPDATE worktrees SET status = 'reaped' WHERE worktree_path = '/p1'"
        )
        await store._conn.execute(
            """
            INSERT INTO worktrees (
                session_id, branch_name, pre_branch_key, worktree_path,
                status, original_play_type, base_ref, created_at, last_used_at
            ) VALUES ('s', 'b', NULL, '/p2', 'active', 'code_review',
                      'origin/b', '2026-01-01', '2026-01-01')
            """
        )
        await store._conn.commit()
    finally:
        await store.close()
