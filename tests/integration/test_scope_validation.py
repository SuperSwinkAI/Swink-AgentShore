"""Integration tests for scope evidence persistence.

Path-prefix drift detection was removed with the cluster store. validate_scope
now only enforces issue inflation; evidence rows can still be written directly
to the store for other consumers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentshore.data.store import (
    DataStore,
    PlayRecord,
    ScopeDriftRecord,
    SessionRecord,
)


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


async def _init_store(tmp_path: Path) -> DataStore:
    """Create, initialize, and return a DataStore rooted under *tmp_path*."""
    db_path = tmp_path / ".agentshore" / "agentshore.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = DataStore(db_path)
    await store.initialize()
    return store


@pytest.mark.asyncio
async def test_scope_drift_recorded(tmp_path: Path) -> None:
    """Scope drift entries are persisted and queryable by session."""
    store = await _init_store(tmp_path)
    try:
        now = _now_iso()
        await store.create_session(
            SessionRecord(
                session_id="drift-session",
                project_path=str(tmp_path),
                started_at=now,
            )
        )
        play_id = await store.record_play(
            PlayRecord(
                session_id="drift-session",
                play_type="issue_pickup",
                started_at=now,
                success=True,
                agent_id="agent-1",
            )
        )

        await store.log_scope_drift(
            ScopeDriftRecord(
                session_id="drift-session",
                artifact="pr#99",
                logged_at=now,
                play_id=play_id,
                reason="No matching prefix",
            )
        )
        await store.log_scope_drift(
            ScopeDriftRecord(
                session_id="drift-session",
                artifact="src/unknown/file.py",
                logged_at=now,
                play_id=play_id,
                reason="Path outside prefix set",
            )
        )

        drifts = await store.list_scope_drift("drift-session")
        assert len(drifts) == 2
        assert drifts[0].artifact == "pr#99"
        assert drifts[0].reason == "No matching prefix"
        assert drifts[1].artifact == "src/unknown/file.py"

        # A different session sees zero drift entries
        other_drifts = await store.list_scope_drift("other-session")
        assert len(other_drifts) == 0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_validate_scope_no_drift_without_path_boundaries(tmp_path: Path) -> None:
    """validate_scope no longer produces artifact drift rows."""
    from agentshore.config import ScopeConfig
    from agentshore.plays.scope import validate_scope
    from agentshore.state import PlayType, SkillResult

    store = await _init_store(tmp_path)
    try:
        now = _now_iso()
        await store.create_session(
            SessionRecord(
                session_id="scope-test",
                project_path=str(tmp_path),
                started_at=now,
            )
        )
        play_id = await store.record_play(
            PlayRecord(
                session_id="scope-test",
                play_type="issue_pickup",
                started_at=now,
                success=True,
            )
        )

        # An artifact that would have drifted under legacy cluster-prefix logic.
        skill_result = SkillResult(
            success=True,
            artifacts=[{"path": "backend/models.py"}],
        )

        # No drift source means nothing is logged regardless of scope_cfg.
        await validate_scope(
            skill_result=skill_result,
            play_id=play_id,
            play_type=PlayType.ISSUE_PICKUP,
            session_id="scope-test",
            scope_cfg=ScopeConfig(strict_mode=False),
            store=store,
        )
        drifts = await store.list_scope_drift("scope-test")
        assert len(drifts) == 0

        # Strict mode also produces no drift without a source.
        await validate_scope(
            skill_result=skill_result,
            play_id=play_id,
            play_type=PlayType.ISSUE_PICKUP,
            session_id="scope-test",
            scope_cfg=ScopeConfig(strict_mode=True),
            store=store,
        )
        drifts_after = await store.list_scope_drift("scope-test")
        assert len(drifts_after) == 0
    finally:
        await store.close()
