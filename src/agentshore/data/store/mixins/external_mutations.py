"""DataStore mixin for the ``external_mutations`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.data.store.rows import _row_to_external_mutation

if TYPE_CHECKING:
    import aiosqlite

    from agentshore.data.models import ExternalMutationRecord


class _ExternalMutationsMixin:
    """Methods that operate on the ``external_mutations`` table."""

    _db: aiosqlite.Connection | None
    _conn: aiosqlite.Connection

    async def record_external_mutation(self, mutation: ExternalMutationRecord) -> None:
        """Insert a GitHub-mutation audit record (idempotency_key must be unique)."""
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO external_mutations
                (session_id, play_id, idempotency_key, mutation_type, target,
                 request_json, response_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mutation.session_id,
                mutation.play_id,
                mutation.idempotency_key,
                mutation.mutation_type,
                mutation.target,
                mutation.request_json,
                mutation.response_json,
                mutation.status,
                mutation.created_at,
            ),
        )
        await self._conn.commit()

    async def get_external_mutation(
        self, session_id: str, idempotency_key: str
    ) -> ExternalMutationRecord | None:
        """Look up an existing mutation by idempotency key."""
        async with self._conn.execute(
            "SELECT * FROM external_mutations WHERE session_id=? AND idempotency_key=?",
            (session_id, idempotency_key),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_external_mutation(row)

    async def update_external_mutation_status(
        self,
        session_id: str,
        idempotency_key: str,
        status: str,
        response: str,
    ) -> None:
        """Update the status and response of an existing mutation record."""
        await self._conn.execute(
            """
            UPDATE external_mutations
               SET status=?, response_json=?
             WHERE session_id=? AND idempotency_key=?
            """,
            (status, response, session_id, idempotency_key),
        )
        await self._conn.commit()

    async def batch_update_external_mutations_status(
        self,
        session_id: str,
        idempotency_keys: list[str],
        status: str,
        response: str,
    ) -> None:
        """Batch-update status and response for multiple mutation records in one commit."""
        await self._conn.executemany(
            "UPDATE external_mutations SET status = ?, response_json = ? "
            "WHERE session_id = ? AND idempotency_key = ?",
            [(status, response, session_id, key) for key in idempotency_keys],
        )
        await self._conn.commit()

    async def list_external_mutations(
        self,
        session_id: str,
        *,
        mutation_types: list[str] | tuple[str, ...] | None = None,
    ) -> list[ExternalMutationRecord]:
        """Return external mutation/audit records for a session."""

        params: list[object] = [session_id]
        where = "session_id = ?"
        if mutation_types:
            placeholders = ", ".join("?" for _ in mutation_types)
            where += f" AND mutation_type IN ({placeholders})"
            params.extend(mutation_types)
        cursor = await self._conn.execute(
            f"""
            SELECT session_id, idempotency_key, mutation_type, target,
                   status, created_at, play_id, request_json, response_json
              FROM external_mutations
             WHERE {where}
             ORDER BY created_at ASC
            """,
            params,
        )
        rows = await cursor.fetchall()
        return [_row_to_external_mutation(row) for row in rows]
