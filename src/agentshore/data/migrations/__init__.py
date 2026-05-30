"""Database migrations for the agentshore_dev_v1 schema generation.

``src/agentshore/data/schema.sql`` is the authoritative baseline for the
agentshore_dev_v1 schema (current schema_version 2).  Incremental migrations
go here as numbered async functions (e.g. ``migrate_v1_to_v2``) and are
invoked explicitly by ``DataStore.initialize()`` after the baseline schema
script has been applied.

No migration history exists prior to agentshore_dev_v1.

Migration history
-----------------
- v1 -> v2: drop the dormant ``pending_approvals`` table (no producer ever
  wrote to it; AgentShore is extreme-bypass with no human-in-the-loop
  directional control).
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
        "INSERT OR IGNORE INTO schema_version (version, applied_at) "
        "VALUES (2, datetime('now'))"
    )
