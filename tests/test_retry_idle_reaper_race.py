"""Tests for #205: retry-vs-idle-reaper race ("work claim inactive").

A claim group parked in ``retrying`` (between a completion-retry being scheduled
and the retry dispatch re-running ``start_work_claim_group``) has no live
dispatch, so the idle-claim reaper would release it out from under the pending
retry. These tests pin:
- ``list_retrying_claim_group_ids`` returns only retrying groups.
- A retrying group is protected from idle release when unioned into the
  exclude set.
- A retry can re-acquire a group that was released by the reaper.
"""

from __future__ import annotations

import pytest

from agentshore.data.store import DataStore, PlayRecord, SessionRecord
from agentshore.plays.base import PlayParams
from agentshore.plays.executor import PlayExecutor
from agentshore.state import PlayType


async def _setup_session(store: DataStore) -> None:
    await store.create_session(
        SessionRecord(
            session_id="s1",
            project_path="/tmp/proj",
            started_at="2026-06-16T00:00:00+00:00",
        )
    )


@pytest.mark.asyncio
async def test_list_retrying_claim_group_ids_returns_only_retrying(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store)
        retrying_group = await store.acquire_work_claims(
            "s1", "issue_pickup", ["issue:1"], status="running", agent_id="a1"
        )
        running_group = await store.acquire_work_claims(
            "s1", "issue_pickup", ["issue:2"], status="running", agent_id="a1"
        )
        assert retrying_group is not None
        assert running_group is not None
        # Park the first group in retrying.
        await store.finish_work_claim_group("s1", retrying_group, status="retrying")

        ids = await store.list_retrying_claim_group_ids("s1")
        assert ids == {retrying_group}
        assert running_group not in ids
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_retrying_group_excluded_from_idle_release(tmp_path) -> None:
    """A retrying group unioned into the exclude set survives the idle reaper."""
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store)
        retry_group = await store.acquire_work_claims(
            "s1", "issue_pickup", ["issue:1"], status="running", agent_id="idle-agent"
        )
        stale_group = await store.acquire_work_claims(
            "s1", "issue_pickup", ["issue:2"], status="running", agent_id="idle-agent"
        )
        assert retry_group is not None
        assert stale_group is not None
        await store.finish_work_claim_group("s1", retry_group, status="retrying")

        protected = await store.list_retrying_claim_group_ids("s1")
        released = await store.release_active_work_claims_for_agents(
            "s1", {"idle-agent"}, exclude_claim_group_ids=protected
        )

        # Only the non-retrying stale group is released.
        assert released == 1
        retry_claims = await store.get_work_claim_group("s1", retry_group)
        stale_claims = await store.get_work_claim_group("s1", stale_group)
        assert [c.status for c in retry_claims] == ["retrying"]
        assert [c.status for c in stale_claims] == ["released"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_retry_reacquires_released_group(tmp_path) -> None:
    """A retry whose group the reaper released can re-acquire and start it."""
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store)
        group = await store.acquire_work_claims(
            "s1", "issue_pickup", ["issue:1"], status="running", agent_id="a1"
        )
        assert group is not None
        # A real play row backs the play_id FK (executor records one first).
        play_id = await store.record_play(
            PlayRecord(
                session_id="s1",
                play_type="issue_pickup",
                started_at="2026-06-16T00:01:00+00:00",
                success=False,
            )
        )
        # Reaper releases it out from under the pending retry.
        await store.release_active_work_claims_for_agents("s1", {"a1"})
        # start_work_claim_group now finds no active rows -> False.
        assert (
            await store.start_work_claim_group("s1", group, play_id=play_id, agent_id="a1") is False
        )

        executor = PlayExecutor.__new__(PlayExecutor)
        executor._store = store  # type: ignore[attr-defined]
        executor._session_id = "s1"  # type: ignore[attr-defined]
        params = PlayParams(
            issue_number=1,
            agent_id="a1",
            extras={"claim_group_id": group, "__retry_prompt": "redo it"},
        )

        ok = await executor._reacquire_claim_group(
            params, play_id=play_id, play_type=PlayType.ISSUE_PICKUP
        )
        assert ok is True
        claims = await store.get_work_claim_group("s1", group)
        # The fresh active rows for the group are now running.
        assert any(c.status == "running" for c in claims)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_retry_reacquire_fails_when_resource_truly_held(tmp_path) -> None:
    """Re-acquire returns False (-> benign skip) when another live claim holds it."""
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store)
        group = await store.acquire_work_claims(
            "s1", "issue_pickup", ["issue:1"], status="running", agent_id="a1"
        )
        assert group is not None
        await store.release_active_work_claims_for_agents("s1", {"a1"})
        # A different live claim now holds the same resource.
        other = await store.acquire_work_claims(
            "s1", "issue_pickup", ["issue:1"], status="running", agent_id="a2"
        )
        assert other is not None

        executor = PlayExecutor.__new__(PlayExecutor)
        executor._store = store  # type: ignore[attr-defined]
        executor._session_id = "s1"  # type: ignore[attr-defined]
        params = PlayParams(
            issue_number=1,
            agent_id="a1",
            extras={"claim_group_id": group, "__retry_prompt": "redo it"},
        )

        ok = await executor._reacquire_claim_group(
            params, play_id=8, play_type=PlayType.ISSUE_PICKUP
        )
        assert ok is False
    finally:
        await store.close()
