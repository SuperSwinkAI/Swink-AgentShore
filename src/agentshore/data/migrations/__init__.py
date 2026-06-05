"""Database migrations for the agentshore_dev_v1 schema generation.

``src/agentshore/data/schema.sql`` is the authoritative baseline for the
agentshore_dev_v1 schema (current schema_version 3).  Incremental migrations
go here as numbered async functions (e.g. ``migrate_v1_to_v2``) and are
invoked explicitly by ``DataStore.initialize()`` after the baseline schema
script has been applied.

No migration history exists prior to agentshore_dev_v1.

Migration history
-----------------
- v1 -> v2: drop the dormant ``pending_approvals`` table (no producer ever
  wrote to it; AgentShore is extreme-bypass with no human-in-the-loop
  directional control).
- v2 -> v3: add ``rl_experience.mask_reason`` so the dominant per-tick mask
  summary is persisted for post-hoc diagnosis of why a play was not selected.
- v3 -> v4: add ``github_issues.github_author`` so issue pickup can be gated to
  trusted identities (opt-in ``trusted_ids.restrict_issues_to_trusted_authors``).
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
