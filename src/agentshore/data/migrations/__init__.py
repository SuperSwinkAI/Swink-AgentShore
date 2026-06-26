"""Forward-only migrations for the agentshore_dev_v1 schema generation.

``schema.sql`` is the authoritative baseline. ``DataStore.initialize()`` applies
idempotent numbered migrations after the baseline script.

Migration history
-----------------
- v1 -> v2: drop the dormant ``pending_approvals`` table (no producer ever
  wrote to it; AgentShore is extreme-bypass with no human-in-the-loop
  directional control).
- v2 -> v3: add ``rl_experience.mask_reason`` so the dominant per-tick mask
  summary is persisted for post-hoc diagnosis of why a play was not selected.
- v3 -> v4: add ``github_issues.github_author`` so issue pickup can be gated to
  trusted identities (opt-in ``trusted_ids.restrict_issues_to_trusted_authors``).
- v4 -> v5: drop ``session_learnings`` table (and its indexes). The JSON store
  at ``.agentshore/learnings.json`` is the single source of truth; the SQLite
  table was never written in production.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


async def migrate_v1_to_v2(conn: aiosqlite.Connection) -> None:
    """Drop the dormant ``pending_approvals`` table.

    Idempotent: ``DROP TABLE IF EXISTS`` is a no-op on fresh databases
    (whose baseline schema no longer creates the table) and on databases
    that have already been migrated. Indexes are dropped implicitly with
    the table.
    """
    await conn.execute("DROP TABLE IF EXISTS pending_approvals")
    await conn.execute(
        "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (2, datetime('now'))"
    )


async def migrate_v2_to_v3(conn: aiosqlite.Connection) -> None:
    """Add ``rl_experience.mask_reason`` (TEXT) for mask diagnosability.

    Stores a compact dominant per-tick mask summary alongside the existing
    ``action_mask`` blob so it is possible to answer, post-hoc, why a play
    (e.g. ``merge_pr``) was not selected on a given tick.

    Idempotent: ``ALTER TABLE ... ADD COLUMN`` is not itself idempotent in
    SQLite (it raises on a duplicate column), so the column is added only when
    absent. A no-op on fresh databases (whose baseline schema already declares
    the column) and on already-migrated databases.
    """
    async with conn.execute("PRAGMA table_info(rl_experience)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if "mask_reason" not in columns:
        await conn.execute("ALTER TABLE rl_experience ADD COLUMN mask_reason TEXT")
    await conn.execute(
        "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (3, datetime('now'))"
    )


async def migrate_v3_to_v4(conn: aiosqlite.Connection) -> None:
    """Add ``github_issues.github_author`` (TEXT) for trusted-identity gating.

    Captures the GitHub login that opened each issue so issue pickup can be
    restricted to trusted authors when
    ``trusted_ids.restrict_issues_to_trusted_authors`` is enabled.

    Idempotent: the column is added only when absent (``ALTER TABLE ... ADD
    COLUMN`` raises on a duplicate in SQLite). A no-op on fresh databases
    (whose baseline schema already declares the column) and on already-migrated
    databases. Pre-existing rows backfill to NULL until the next full issue
    re-sync repopulates the author.
    """
    async with conn.execute("PRAGMA table_info(github_issues)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if "github_author" not in columns:
        await conn.execute("ALTER TABLE github_issues ADD COLUMN github_author TEXT")
    await conn.execute(
        "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (4, datetime('now'))"
    )


async def migrate_v4_to_v5(conn: aiosqlite.Connection) -> None:
    """Drop the dead ``session_learnings`` table and its indexes.

    The JSON store at ``.agentshore/learnings.json`` is the single source of
    truth for learnings; the ``session_learnings`` SQLite table was never
    written in production (``record_learning`` was only called from tests).

    Idempotent: ``DROP TABLE IF EXISTS`` and ``DROP INDEX IF EXISTS`` are no-ops
    when the objects do not exist (fresh v5 databases, already-migrated databases).
    Indexes are normally dropped with the table in SQLite, but explicit drops are
    included for safety.
    """
    await conn.execute("DROP INDEX IF EXISTS idx_learnings_category")
    await conn.execute("DROP INDEX IF EXISTS idx_learnings_session")
    await conn.execute("DROP TABLE IF EXISTS session_learnings")
    await conn.execute(
        "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (5, datetime('now'))"
    )
