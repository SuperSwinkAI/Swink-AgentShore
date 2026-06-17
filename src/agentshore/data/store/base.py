"""Base class providing the shared connection state for DataStore mixins."""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING, Any, TypeVar, cast

from agentshore.errors import DatabaseError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    import aiosqlite

# Bound to a coroutine method so the decorator preserves the exact wrapped
# signature for callers (mypy keeps each method's real parameters/return type).
_F = TypeVar("_F", bound="Callable[..., Awaitable[Any]]")


class _ReentrantConnectionLock:
    """Task-reentrant async lock serializing access to the shared connection.

    AgentShore runs one process-wide ``aiosqlite.Connection``. Under concurrent
    asyncio tasks (the dispatched play plus the agent-manager monitor tasks) a
    ``COMMIT`` issued by one task can land while another task still holds an open
    cursor on the same connection, which SQLite rejects with ``cannot commit
    transaction - SQL statements in progress`` (GH #219). Serializing every
    connection-touching :class:`DataStore` method behind this lock closes that
    race — only one logical DB operation touches the connection at a time, so a
    commit can never coincide with a foreign open statement.

    Re-entrant *per task* because DataStore methods compose (a pull-request
    write calls ``supersede_work_claims(commit=False)`` before its own commit);
    the owning task must re-acquire without deadlocking. A different task always
    blocks until the owner fully releases.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[object] | None = None
        self._depth = 0

    async def __aenter__(self) -> None:
        task = asyncio.current_task()
        if task is not None and self._owner is task:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner = task
        self._depth = 1

    async def __aexit__(self, *exc: object) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()


def _serialized(method: _F) -> _F:
    """Hold the connection lock for the full duration of a DataStore coroutine.

    Applied to every method that touches ``self._conn``/``self._db`` (or composes
    other such methods) so concurrent tasks can't interleave a commit into
    another task's open cursor. See :class:`_ReentrantConnectionLock` (GH #219).
    """

    @functools.wraps(method)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        async with self._db_lock:
            return await method(self, *args, **kwargs)

    return cast("_F", wrapper)


_ACTIVE_WORK_CLAIM_STATUSES = frozenset({"queued", "claimed", "running", "retrying"})
_TERMINAL_WORK_CLAIM_STATUSES = frozenset(
    {"completed", "released", "superseded", "failed", "abandoned"}
)


def _status_in_clause(
    statuses: frozenset[str], *, column: str = "status"
) -> tuple[str, tuple[str, ...]]:
    """Build a ``status IN (?, ?, …)`` fragment and its ordered bind params.

    Returns ``(clause, params)`` so callers splice the fragment into an
    f-string and pass ``*params`` in the same position — collapsing the
    hand-rolled ``",".join("?" for _ in …)`` placeholder builders repeated
    across the work-claims mixin. ``statuses`` is a frozenset, so the
    placeholder count and the param tuple are derived from one ordering and
    cannot drift apart.
    """
    ordered = tuple(statuses)
    placeholders = ", ".join("?" for _ in ordered)
    return f"{column} IN ({placeholders})", ordered


class _DataStoreBase:
    """Holds the aiosqlite connection plus the ``_conn`` accessor.

    The mixin classes that compose ``DataStore`` declare ``_db`` and
    ``_db_path`` as class-level annotations and rely on ``self._conn`` for
    the runtime-asserted connection handle.  This base class is the
    rightmost entry in the MRO and the only one that defines ``__init__``.
    """

    _db: aiosqlite.Connection | None
    _db_path: Path

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db = None

    @property
    def _db_lock(self) -> _ReentrantConnectionLock:
        """Per-instance lock serializing connection access (GH #219).

        Lazily created on first use so instances built via ``DataStore.__new__``
        (some tests bypass ``initialize()`` that way) still get a lock without an
        ``__init__`` run. One lock per store instance — i.e. per shared
        connection. Creation is await-free, so the single-threaded event loop
        can't race two creations.
        """
        lock = self.__dict__.get("_db_lock_obj")
        if lock is None:
            lock = _ReentrantConnectionLock()
            self.__dict__["_db_lock_obj"] = lock
        return lock

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            msg = "DataStore is not initialized — call initialize() first"
            raise RuntimeError(msg)
        return self._db

    @_serialized
    async def _insert(self, table: str, **cols: object) -> int:
        """Insert one row from keyword columns and return its ``lastrowid``.

        ``**cols`` is keyed by column name, so the column list and the value
        tuple cannot drift apart (dict insertion order is stable in 3.12) —
        the misalignment class that left ``base_ref``/``mask_reason``
        write-only. Commits the row and raises ``DatabaseError`` when SQLite
        returns no row id. For plain single-row ``INSERT`` only; upserts,
        ``INSERT OR IGNORE``, and ``executemany`` keep their explicit SQL.
        """
        names = ", ".join(cols)
        placeholders = ", ".join("?" * len(cols))
        cursor = await self._conn.execute(
            f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
            tuple(cols.values()),
        )
        await self._conn.commit()
        if cursor.lastrowid is None:
            msg = f"INSERT into {table} returned no row id"
            raise DatabaseError(msg)
        return cursor.lastrowid
