"""Base class providing the shared connection state for DataStore mixins."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite


_ACTIVE_WORK_CLAIM_STATUSES = frozenset({"queued", "claimed", "running", "retrying"})
_TERMINAL_WORK_CLAIM_STATUSES = frozenset(
    {"completed", "released", "superseded", "failed", "abandoned"}
)


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
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            msg = "DataStore is not initialized — call initialize() first"
            raise RuntimeError(msg)
        return self._db
