"""Tests for DataStore.fail_session — crash finalization (idempotent, race-safe)."""

from __future__ import annotations

import pytest

from agentshore.data.store import DataStore, SessionRecord


async def _make_running_session(store: DataStore, sid: str = "s1") -> None:
    await store.create_session(
        SessionRecord(
            session_id=sid,
            project_path="/tmp/proj",
            started_at="2026-05-31T00:00:00+00:00",
        )
    )


@pytest.mark.asyncio
async def test_fail_session_finalizes_running_row(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _make_running_session(store)
        row = await store.get_session("s1")
        assert row is not None
        assert row.status == "running"
        assert row.ended_at is None

        await store.fail_session("s1", "orchestrator_task_crashed")

        row = await store.get_session("s1")
        assert row is not None
        assert row.status == "failed"
        assert row.ended_at is not None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fail_session_is_noop_once_completed(tmp_path) -> None:
    """The ended_at IS NULL guard means a clean completion is never clobbered."""
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _make_running_session(store)
        await store.complete_session("s1", final_alignment=0.5)
        completed = await store.get_session("s1")
        assert completed is not None
        assert completed.status == "completed"

        # A late crash done-callback firing after a clean stop must not flip it.
        await store.fail_session("s1", "orchestrator_task_crashed")

        row = await store.get_session("s1")
        assert row is not None
        assert row.status == "completed"
        assert row.ended_at == completed.ended_at
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fail_session_unknown_session_is_safe(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await store.fail_session("does-not-exist", "reason")
        assert await store.get_session("does-not-exist") is None
    finally:
        await store.close()
