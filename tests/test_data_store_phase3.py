"""Tests for Phase-3 DataStore extensions: ExperienceRecord, CheckpointRecord."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from agentshore.data import (
    CheckpointRecord,
    DataStore,
    ExperienceRecord,
    PlayRecord,
    SessionRecord,
)


def _ts(offset_seconds: float = 0.0) -> str:
    """Return an ISO-8601 timestamp shifted by ``offset_seconds`` from now."""
    return (datetime.now(UTC) + timedelta(seconds=offset_seconds)).isoformat(timespec="seconds")


@pytest_asyncio.fixture
async def store(tmp_path):
    db = DataStore(tmp_path / "test.db")
    await db.initialize()
    yield db
    await db.close()


async def _seed(store: DataStore) -> tuple[str, int]:
    """Create a session + one play row.  Return (session_id, play_id)."""
    sid = "sess-p3-test"
    # Session is older than the play so the relative ordering is preserved.
    await store.create_session(
        SessionRecord(session_id=sid, project_path="/tmp", started_at=_ts(-60))
    )
    play_id = await store.record_play(
        PlayRecord(
            session_id=sid,
            play_type="issue_pickup",
            started_at=_ts(),
            success=True,
        )
    )
    return sid, play_id


async def test_record_experience_returns_id_and_stores_blobs(store):
    sid, play_id = await _seed(store)
    state_vec = b"\x00\x01\x02" * 24  # 72 floats of garbage
    next_state = b"\x03\x04\x05" * 24
    mask = b"\xff" * 20

    rec = ExperienceRecord(
        session_id=sid,
        play_id=play_id,
        state_vector=state_vec,
        action=3,
        reward=1.5,
        next_state=next_state,
        done=0,
        old_log_prob=-2.3,
        value_estimate=0.8,
        action_mask=mask,
        policy_version="ppo-v1-abc",
        action_space_version=1,
        config_hash="deadbeef",
        step_index=0,
    )
    xp_id = await store.record_experience(rec)
    assert isinstance(xp_id, int)
    assert xp_id > 0


async def test_record_experience_null_optional_fields(store):
    sid, play_id = await _seed(store)
    rec = ExperienceRecord(
        session_id=sid,
        play_id=play_id,
        state_vector=b"\x00" * 288,
        action=0,
        reward=0.0,
        next_state=b"\x00" * 288,
        done=1,
        action_space_version=1,
    )
    xp_id = await store.record_experience(rec)
    assert isinstance(xp_id, int)


async def test_iter_experience_yields_in_step_index_order(store):
    sid, play_id = await _seed(store)
    play_id2 = await store.record_play(
        PlayRecord(
            session_id=sid,
            play_type="code_review",
            started_at=_ts(60),  # later than the seeded play
            success=True,
        )
    )

    for step, pid in [(1, play_id2), (0, play_id)]:
        await store.record_experience(
            ExperienceRecord(
                session_id=sid,
                play_id=pid,
                state_vector=bytes([step]) * 288,
                action=step,
                reward=float(step),
                next_state=b"\x00" * 288,
                done=0,
                action_space_version=1,
                config_hash="abc",
                step_index=step,
            )
        )

    rows = [r async for r in store.iter_experience_for_replay(sid, 1, "abc")]
    assert len(rows) == 2
    assert rows[0].step_index == 0
    assert rows[1].step_index == 1


async def test_iter_experience_round_trips_mask_reason(store):
    """Regression: ``mask_reason`` was written by ``record_experience`` but
    dropped from both replay SELECTs and ``_row_to_experience``, so it always
    read back ``None``. Persist a non-null value and assert it survives replay.
    """
    sid, play_id = await _seed(store)
    await store.record_experience(
        ExperienceRecord(
            session_id=sid,
            play_id=play_id,
            state_vector=b"\x00" * 288,
            action=0,
            reward=0.0,
            next_state=b"\x00" * 288,
            done=0,
            action_space_version=1,
            config_hash="abc",
            mask_reason="merge_pr: no approved PR",
            step_index=0,
        )
    )
    rows = [r async for r in store.iter_experience_for_replay(sid, 1, "abc")]
    assert len(rows) == 1
    assert rows[0].mask_reason == "merge_pr: no approved PR"
    # The no-config-hash SELECT must agree.
    rows_all = [r async for r in store.iter_experience_for_replay(sid, 1)]
    assert rows_all[0].mask_reason == "merge_pr: no approved PR"


async def test_iter_experience_version_mismatch_returns_empty(store):
    sid, play_id = await _seed(store)
    await store.record_experience(
        ExperienceRecord(
            session_id=sid,
            play_id=play_id,
            state_vector=b"\x00" * 288,
            action=0,
            reward=0.0,
            next_state=b"\x00" * 288,
            done=0,
            action_space_version=1,
            step_index=0,
        )
    )

    rows = [r async for r in store.iter_experience_for_replay(sid, 2)]  # wrong version
    assert rows == []


async def test_iter_experience_config_hash_filter(store):
    sid, play_id = await _seed(store)
    play_id2 = await store.record_play(
        PlayRecord(
            session_id=sid,
            play_type="run_qa",
            started_at=_ts(60),  # later than the seeded play
            success=True,
        )
    )

    await store.record_experience(
        ExperienceRecord(
            session_id=sid,
            play_id=play_id,
            state_vector=b"\x00" * 288,
            action=0,
            reward=0.0,
            next_state=b"\x00" * 288,
            done=0,
            action_space_version=1,
            config_hash="hash-a",
            step_index=0,
        )
    )
    await store.record_experience(
        ExperienceRecord(
            session_id=sid,
            play_id=play_id2,
            state_vector=b"\x00" * 288,
            action=1,
            reward=1.0,
            next_state=b"\x00" * 288,
            done=0,
            action_space_version=1,
            config_hash="hash-b",
            step_index=1,
        )
    )

    rows_a = [r async for r in store.iter_experience_for_replay(sid, 1, "hash-a")]
    rows_b = [r async for r in store.iter_experience_for_replay(sid, 1, "hash-b")]
    rows_all = [r async for r in store.iter_experience_for_replay(sid, 1)]

    assert len(rows_a) == 1
    assert rows_a[0].config_hash == "hash-a"
    assert len(rows_b) == 1
    assert len(rows_all) == 2


async def test_save_and_load_latest_checkpoint(store):
    sid = "sess-ckpt"
    await store.create_session(
        SessionRecord(session_id=sid, project_path="/tmp", started_at="2026-01-01T00:00:00")
    )

    rec = CheckpointRecord(
        session_id=sid,
        created_at="2026-01-01T01:00:00",
        play_count=10,
        weights_path="/tmp/policy.pt",
        avg_reward=1.23,
    )
    ckpt_id = await store.save_checkpoint(rec)
    assert isinstance(ckpt_id, int)

    loaded = await store.load_latest_checkpoint(session_id=sid)
    assert loaded is not None
    assert loaded.checkpoint_id == ckpt_id
    assert loaded.play_count == 10
    assert loaded.weights_path == "/tmp/policy.pt"
    assert loaded.avg_reward == pytest.approx(1.23)


async def test_load_latest_returns_highest_play_count(store):
    sid = "sess-ckpt2"
    await store.create_session(
        SessionRecord(session_id=sid, project_path="/tmp", started_at="2026-01-01T00:00:00")
    )

    for pc in [5, 15, 10]:
        await store.save_checkpoint(
            CheckpointRecord(
                session_id=sid,
                created_at="2026-01-01T00:00:00",
                play_count=pc,
                weights_path=f"/tmp/p{pc}.pt",
            )
        )

    loaded = await store.load_latest_checkpoint(session_id=sid)
    assert loaded is not None
    assert loaded.play_count == 15


async def test_load_latest_returns_none_when_absent(store):
    sid = "sess-no-ckpt"
    await store.create_session(
        SessionRecord(session_id=sid, project_path="/tmp", started_at="2026-01-01T00:00:00")
    )
    result = await store.load_latest_checkpoint(session_id=sid)
    assert result is None


async def test_load_latest_cross_session(store):
    """load_latest_checkpoint(session_id=None) returns the global latest."""
    for sid, pc in [("sess-x1", 8), ("sess-x2", 20)]:
        await store.create_session(
            SessionRecord(session_id=sid, project_path="/tmp", started_at="2026-01-01T00:00:00")
        )
        await store.save_checkpoint(
            CheckpointRecord(
                session_id=sid,
                created_at="2026-01-01T00:00:00",
                play_count=pc,
                weights_path=f"/tmp/p{pc}.pt",
            )
        )

    latest = await store.load_latest_checkpoint()
    assert latest is not None
    assert latest.play_count == 20


async def test_update_play_persists_reward_and_alignment_fields(store):
    sid = "sess-upd-p3"
    await store.create_session(
        SessionRecord(session_id=sid, project_path="/tmp", started_at="2026-01-01T00:00:00")
    )
    play_id = await store.record_play(
        PlayRecord(
            session_id=sid,
            play_type="calibrate_alignment",
            started_at="2026-01-01T00:00:00",
            success=False,
        )
    )

    await store.update_play(
        play_id,
        success=True,
        ended_at="2026-01-01T00:01:00",
        alignment_before=0.4,
        alignment_after=0.7,
        alignment_delta=0.3,
        reward=2.5,
    )

    history = await store.get_play_history(sid)
    assert len(history) == 1
    rec = history[0]
    assert rec.alignment_before == pytest.approx(0.4)
    assert rec.alignment_after == pytest.approx(0.7)
    assert rec.alignment_delta == pytest.approx(0.3)
    assert rec.reward == pytest.approx(2.5)
