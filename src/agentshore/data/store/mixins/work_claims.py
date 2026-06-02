"""DataStore mixin for the ``work_claims`` and ``dispatch_replay`` tables."""

from __future__ import annotations

import sqlite3
import uuid
from typing import TYPE_CHECKING

from agentshore.data.store.base import (
    _ACTIVE_WORK_CLAIM_STATUSES,
    _TERMINAL_WORK_CLAIM_STATUSES,
    _status_in_clause,
)
from agentshore.data.store.helpers import _dedupe_resource_keys
from agentshore.data.store.rows import _row_to_dispatch_replay, _row_to_work_claim
from agentshore.utils import now_iso

if TYPE_CHECKING:
    import aiosqlite

    from agentshore.data.models import DispatchReplayRecord, WorkClaimRecord


class _WorkClaimsMixin:
    """Methods that operate on ``work_claims`` and ``dispatch_replay``."""

    _db: aiosqlite.Connection | None
    _conn: aiosqlite.Connection

    async def acquire_work_claims(
        self,
        session_id: str,
        play_type: str,
        resource_keys: list[str] | tuple[str, ...],
        *,
        status: str = "claimed",
        claim_group_id: str | None = None,
        agent_id: str | None = None,
        play_id: int | None = None,
        review_queue_id: int | None = None,
    ) -> str | None:
        """Atomically acquire active claims for every resource key.

        Returns the claim group id on success, or None when any resource already
        has an active claim in the session.
        """
        if status not in _ACTIVE_WORK_CLAIM_STATUSES:
            raise ValueError(f"work claim status must be active, got {status!r}")
        keys = _dedupe_resource_keys(resource_keys)
        if not keys:
            return None
        group_id = claim_group_id or uuid.uuid4().hex
        now = now_iso()
        try:
            await self._conn.execute("BEGIN IMMEDIATE")
            await self._insert_work_claim_rows(
                group_id,
                session_id,
                play_type,
                keys,
                status=status,
                now=now,
                agent_id=agent_id,
                play_id=play_id,
                review_queue_id=review_queue_id,
            )
            await self._conn.commit()
            return group_id
        except sqlite3.IntegrityError:
            await self._conn.rollback()
            return None
        except (sqlite3.DatabaseError, OSError):
            await self._conn.rollback()
            raise

    async def claim_review_with_work_claims(
        self,
        *,
        session_id: str,
        queue_id: int,
        agent_id: str,
        play_type: str,
        resource_keys: list[str] | tuple[str, ...],
    ) -> str | None:
        """Claim a review queue row and its PR resource in one transaction."""
        keys = _dedupe_resource_keys(resource_keys)
        if not keys:
            return None
        group_id = uuid.uuid4().hex
        now = now_iso()
        try:
            await self._conn.execute("BEGIN IMMEDIATE")
            async with self._conn.execute(
                """
                UPDATE review_queue
                   SET status = 'claimed', claimed_by = ?, claimed_at = ?
                 WHERE queue_id = ? AND session_id = ? AND status = 'pending'
                """,
                (agent_id, now, queue_id, session_id),
            ) as cursor:
                if cursor.rowcount != 1:
                    await self._conn.rollback()
                    return None
            await self._insert_work_claim_rows(
                group_id,
                session_id,
                play_type,
                keys,
                status="claimed",
                now=now,
                agent_id=agent_id,
                review_queue_id=queue_id,
            )
            await self._conn.commit()
            return group_id
        except sqlite3.IntegrityError:
            await self._conn.rollback()
            return None
        except (sqlite3.DatabaseError, OSError):
            await self._conn.rollback()
            raise

    async def _insert_work_claim_rows(
        self,
        claim_group_id: str,
        session_id: str,
        play_type: str,
        resource_keys: list[str],
        *,
        status: str,
        now: str,
        agent_id: str | None = None,
        play_id: int | None = None,
        review_queue_id: int | None = None,
    ) -> None:
        # ``request_mutation_key`` column is left to its NULL default (the
        # request_play mechanism that populated it was removed); the column
        # itself is retained as an inert nullable field.
        await self._conn.executemany(
            """
            INSERT INTO work_claims
                (claim_group_id, session_id, play_type, resource_key, status,
                 agent_id, play_id, review_queue_id,
                 created_at, claimed_at, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            [
                (
                    claim_group_id,
                    session_id,
                    play_type,
                    key,
                    status,
                    agent_id,
                    play_id,
                    review_queue_id,
                    now,
                    now if status in _ACTIVE_WORK_CLAIM_STATUSES else None,
                )
                for key in resource_keys
            ],
        )

    async def get_work_claim_group(
        self, session_id: str, claim_group_id: str
    ) -> list[WorkClaimRecord]:
        """Return all rows in a claim group."""
        cursor = await self._conn.execute(
            """
            SELECT claim_id, claim_group_id, session_id, play_type, resource_key,
                   status, agent_id, play_id, request_mutation_key, review_queue_id,
                   created_at, claimed_at, started_at, finished_at
            FROM work_claims
            WHERE session_id = ? AND claim_group_id = ?
            ORDER BY claim_id ASC
            """,
            (session_id, claim_group_id),
        )
        rows = await cursor.fetchall()
        return [_row_to_work_claim(row) for row in rows]

    async def work_claim_group_is_active(self, session_id: str, claim_group_id: str) -> bool:
        """Return True when at least one row in the group is still active."""
        status_clause, status_params = _status_in_clause(_ACTIVE_WORK_CLAIM_STATUSES)
        async with self._conn.execute(
            f"""
            SELECT 1 FROM work_claims
            WHERE session_id = ?
              AND claim_group_id = ?
              AND {status_clause}
            LIMIT 1
            """,
            (session_id, claim_group_id, *status_params),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def find_active_work_claims(
        self, session_id: str, resource_keys: list[str] | tuple[str, ...]
    ) -> list[WorkClaimRecord]:
        """Return active claims for any of the provided resource keys."""
        keys = _dedupe_resource_keys(resource_keys)
        if not keys:
            return []
        key_placeholders = ",".join("?" for _ in keys)
        status_clause, status_params = _status_in_clause(_ACTIVE_WORK_CLAIM_STATUSES)
        cursor = await self._conn.execute(
            f"""
            SELECT claim_id, claim_group_id, session_id, play_type, resource_key,
                   status, agent_id, play_id, request_mutation_key, review_queue_id,
                   created_at, claimed_at, started_at, finished_at
            FROM work_claims
            WHERE session_id = ?
              AND resource_key IN ({key_placeholders})
              AND {status_clause}
            ORDER BY claim_id ASC
            """,
            (session_id, *keys, *status_params),
        )
        rows = await cursor.fetchall()
        return [_row_to_work_claim(row) for row in rows]

    async def find_active_work_claims_for_agents(
        self,
        session_id: str,
        agent_ids: list[str] | tuple[str, ...] | set[str] | frozenset[str],
    ) -> list[WorkClaimRecord]:
        """Return active claims owned by any of the provided agents."""
        ids = sorted({str(agent_id) for agent_id in agent_ids if agent_id})
        if not ids:
            return []
        agent_placeholders = ",".join("?" for _ in ids)
        status_clause, status_params = _status_in_clause(_ACTIVE_WORK_CLAIM_STATUSES)
        cursor = await self._conn.execute(
            f"""
            SELECT claim_id, claim_group_id, session_id, play_type, resource_key,
                   status, agent_id, play_id, request_mutation_key, review_queue_id,
                   created_at, claimed_at, started_at, finished_at
            FROM work_claims
            WHERE session_id = ?
              AND agent_id IN ({agent_placeholders})
              AND {status_clause}
            ORDER BY claim_id ASC
            """,
            (session_id, *ids, *status_params),
        )
        rows = await cursor.fetchall()
        return [_row_to_work_claim(row) for row in rows]

    async def start_work_claim_group(
        self,
        session_id: str,
        claim_group_id: str,
        *,
        play_id: int,
        agent_id: str | None,
    ) -> bool:
        """Transition queued/claimed rows to running for dispatch."""
        status_clause, status_params = _status_in_clause(_ACTIVE_WORK_CLAIM_STATUSES)
        async with self._conn.execute(
            f"""
            UPDATE work_claims
               SET status = 'running',
                   play_id = ?,
                   agent_id = COALESCE(?, agent_id),
                   started_at = COALESCE(started_at, ?)
            WHERE session_id = ?
              AND claim_group_id = ?
              AND {status_clause}
            """,
            (
                play_id,
                agent_id,
                now_iso(),
                session_id,
                claim_group_id,
                *status_params,
            ),
        ) as cursor:
            await self._conn.commit()
            return cursor.rowcount > 0

    async def finish_work_claim_group(
        self, session_id: str, claim_group_id: str, *, status: str
    ) -> None:
        """Finish active rows in a claim group with a terminal status."""
        if status == "retrying":
            status_clause, status_params = _status_in_clause(_ACTIVE_WORK_CLAIM_STATUSES)
            await self._conn.execute(
                f"""
                UPDATE work_claims
                   SET status = 'retrying', finished_at = NULL
                 WHERE session_id = ?
                   AND claim_group_id = ?
                   AND {status_clause}
                """,
                (session_id, claim_group_id, *status_params),
            )
            await self._conn.commit()
            return
        if status not in _TERMINAL_WORK_CLAIM_STATUSES:
            raise ValueError(f"work claim status must be terminal, got {status!r}")
        finishable_statuses = (
            _ACTIVE_WORK_CLAIM_STATUSES | {"superseded"}
            if status == "completed"
            else _ACTIVE_WORK_CLAIM_STATUSES
        )
        status_clause, status_params = _status_in_clause(finishable_statuses)
        await self._conn.execute(
            f"""
            UPDATE work_claims
               SET status = ?, finished_at = COALESCE(finished_at, ?)
             WHERE session_id = ?
               AND claim_group_id = ?
               AND {status_clause}
            """,
            (status, now_iso(), session_id, claim_group_id, *status_params),
        )
        await self._conn.commit()

    async def save_dispatch_replay(
        self,
        *,
        session_id: str,
        claim_group_id: str,
        play_id: int,
        skill_name: str,
        params_json: str,
        prompt: str,
        branch: str | None,
    ) -> None:
        """Persist replay payload for deterministic timeout retries."""
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO dispatch_replay
                (
                    session_id, claim_group_id, play_id, skill_name,
                    params_json, prompt, branch, created_at
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                claim_group_id,
                play_id,
                skill_name,
                params_json,
                prompt,
                branch,
                now_iso(),
            ),
        )
        await self._conn.commit()

    async def get_dispatch_replay(
        self, *, session_id: str, claim_group_id: str, play_id: int
    ) -> DispatchReplayRecord | None:
        """Load replay payload for a specific claim group + play."""
        async with self._conn.execute(
            """
            SELECT
                session_id, claim_group_id, play_id, skill_name,
                params_json, prompt, branch, created_at
            FROM dispatch_replay
            WHERE session_id = ? AND claim_group_id = ? AND play_id = ?
            """,
            (session_id, claim_group_id, play_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_dispatch_replay(row)

    async def get_work_claim_retry_attempts(self, session_id: str, claim_group_id: str) -> int:
        """Return current retry attempt count for a claim group."""
        async with self._conn.execute(
            """
            SELECT COALESCE(MAX(retry_attempts), 0) AS attempts
            FROM work_claims
            WHERE session_id = ? AND claim_group_id = ?
            """,
            (session_id, claim_group_id),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["attempts"]) if row is not None else 0

    async def increment_work_claim_retry(self, session_id: str, claim_group_id: str) -> int:
        """Increment retry attempts for all rows in a claim group and return new value."""
        await self._conn.execute(
            """
            UPDATE work_claims
            SET retry_attempts = retry_attempts + 1
            WHERE session_id = ? AND claim_group_id = ?
            """,
            (session_id, claim_group_id),
        )
        await self._conn.commit()
        return await self.get_work_claim_retry_attempts(session_id, claim_group_id)

    async def release_work_claim_group(self, session_id: str, claim_group_id: str) -> None:
        """Release active rows in a claim group."""
        await self.finish_work_claim_group(session_id, claim_group_id, status="released")

    async def release_active_work_claims_for_agents(
        self,
        session_id: str,
        agent_ids: list[str] | tuple[str, ...] | set[str] | frozenset[str],
    ) -> int:
        """Release all active work claims owned by the provided idle agents."""
        ids = sorted({str(agent_id) for agent_id in agent_ids if agent_id})
        if not ids:
            return 0
        agent_placeholders = ",".join("?" for _ in ids)
        status_clause, status_params = _status_in_clause(_ACTIVE_WORK_CLAIM_STATUSES)
        cursor = await self._conn.execute(
            f"""
            UPDATE work_claims
               SET status = 'released', finished_at = COALESCE(finished_at, ?)
             WHERE session_id = ?
               AND agent_id IN ({agent_placeholders})
               AND {status_clause}
            """,
            (now_iso(), session_id, *ids, *status_params),
        )
        await self._conn.commit()
        return cursor.rowcount

    async def supersede_work_claims(
        self,
        session_id: str,
        resource_keys: list[str] | tuple[str, ...],
    ) -> None:
        """Mark active claims for these resources superseded."""
        keys = _dedupe_resource_keys(resource_keys)
        if not keys:
            return
        key_placeholders = ",".join("?" for _ in keys)
        status_clause, status_params = _status_in_clause(_ACTIVE_WORK_CLAIM_STATUSES)
        await self._conn.execute(
            f"""
            UPDATE work_claims
               SET status = 'superseded', finished_at = COALESCE(finished_at, ?)
             WHERE session_id = ?
               AND resource_key IN ({key_placeholders})
               AND {status_clause}
            """,
            (now_iso(), session_id, *keys, *status_params),
        )
        await self._conn.commit()

    async def abandon_active_work_claims(self, session_id: str) -> None:
        """Mark leftover active claims abandoned during startup recovery."""
        status_clause, status_params = _status_in_clause(_ACTIVE_WORK_CLAIM_STATUSES)
        await self._conn.execute(
            f"""
            UPDATE work_claims
               SET status = 'abandoned', finished_at = COALESCE(finished_at, ?)
             WHERE session_id = ?
               AND {status_clause}
            """,
            (now_iso(), session_id, *status_params),
        )
        await self._conn.commit()
