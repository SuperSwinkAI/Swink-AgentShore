"""Session archiver — creates portable snapshots of completed sessions."""

from __future__ import annotations

import asyncio
import datetime
import json
import shutil
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.data.store import ArchiveRecord, DataStore, SessionRecord

from agentshore import __version__


class Archiver:
    """Creates and manages session archives."""

    def __init__(self, store: DataStore, archive_dir: Path) -> None:
        self._store = store
        self._archive_dir = archive_dir

    async def create_archive(
        self,
        session_id: str,
        *,
        db_path: Path,
        learnings_path: Path | None = None,
        policy_path: Path | None = None,
    ) -> Path:
        """Create an archive directory for a completed session.

        Steps:
        1. Get session record from DataStore
        2. Create archive dir: archive_dir / f"{date}-{session_id[:8]}"
        3. Copy session.sqlite (the DB file)
        4. Copy learnings.json if present
        5. Copy policy.pt if present
        6. Generate manifest.json with metadata
        7. Insert row into session_archives table

        Returns the path to the created archive directory.
        Raises ValueError if session not found.
        """
        from agentshore.data.store import ArchiveRecord as _ArchiveRecord

        session = await self._store.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")

        archive_path = await asyncio.to_thread(
            _write_archive_files,
            archive_dir=self._archive_dir,
            session_id=session_id,
            session=session,
            db_path=db_path,
            learnings_path=learnings_path,
            policy_path=policy_path,
        )

        # Insert DB record
        record = _ArchiveRecord(
            archive_id=str(uuid.uuid4()),
            session_id=session_id,
            archive_path=str(archive_path),
            total_cost=session.total_cost,
            final_alignment=session.final_alignment or 0.0,
            total_plays=session.total_plays,
            created_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )
        await self._store.create_archive(record)
        return archive_path

    async def list_archives(self) -> list[ArchiveRecord]:
        """Return all archive records."""
        return await self._store.list_archives()

    async def auto_archive_if_enabled(
        self,
        session_id: str,
        *,
        auto_archive: bool,
        db_path: Path,
        learnings_path: Path | None = None,
        policy_path: Path | None = None,
    ) -> Path | None:
        """Create archive on session end if auto_archive is True."""
        if not auto_archive:
            return None
        return await self.create_archive(
            session_id,
            db_path=db_path,
            learnings_path=learnings_path,
            policy_path=policy_path,
        )


def _write_archive_files(
    *,
    archive_dir: Path,
    session_id: str,
    session: SessionRecord,
    db_path: Path,
    learnings_path: Path | None,
    policy_path: Path | None,
) -> Path:
    """Assemble archive files synchronously for execution in a worker thread."""
    date_str = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d")
    archive_name = f"{date_str}-{session_id[:8]}"
    archive_path = archive_dir / archive_name
    archive_path.mkdir(parents=True, exist_ok=True)

    if db_path.exists():
        shutil.copy2(db_path, archive_path / "session.sqlite")
    if learnings_path is not None and learnings_path.exists():
        shutil.copy2(learnings_path, archive_path / "learnings.json")
    if policy_path is not None and policy_path.exists():
        shutil.copy2(policy_path, archive_path / "policy.pt")

    manifest = {
        "session_id": session_id,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
        "total_plays": session.total_plays,
        "total_cost": session.total_cost,
        "final_alignment": session.final_alignment,
        "agentshore_version": __version__,
        "config_hash": getattr(session, "config_hash", None),
        "archived_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    (archive_path / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return archive_path
