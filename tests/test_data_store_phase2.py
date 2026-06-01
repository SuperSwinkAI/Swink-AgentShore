"""Tests for Phase-2 DataStore extensions.

Covers: ScopeDriftRecord, SessionLearningRecord,
TrajectorySnapshotRecord, ReviewFeedbackPatternRecord, list_open_pull_requests,
and the record_play -> play_id return value.
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio

from agentshore.data import (
    DataStore,
    PlayRecord,
    PullRequestRecord,
    SessionRecord,
)
from agentshore.data.store import (
    HumanFeedbackRecord,
    ScopeDriftRecord,
    SessionLearningRecord,
    TrajectorySnapshotRecord,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def store(tmp_path):
    db = DataStore(tmp_path / "test.db")
    await db.initialize()
    yield db
    await db.close()


async def _seed(store: DataStore) -> tuple[str, int]:
    """Create a session + one play row.  Return (session_id, play_id)."""
    sid = "sess-p2-test"
    await store.create_session(
        SessionRecord(session_id=sid, project_path="/tmp", started_at="2026-01-01T00:00:00")
    )
    play_id = await store.record_play(
        PlayRecord(
            session_id=sid,
            play_type="issue_pickup",
            started_at="2026-01-01T00:01:00",
            success=True,
        )
    )
    return sid, play_id


# ---------------------------------------------------------------------------
# record_play now returns play_id
# ---------------------------------------------------------------------------


async def test_record_play_returns_auto_increment_play_id(store):
    sid = "sess-play-id"
    await store.create_session(
        SessionRecord(session_id=sid, project_path="/tmp", started_at="2026-01-01T00:00:00")
    )
    id1 = await store.record_play(
        PlayRecord(
            session_id=sid,
            play_type="seed_project",
            started_at="2026-01-01T00:01:00",
            success=True,
        )
    )
    id2 = await store.record_play(
        PlayRecord(
            session_id=sid,
            play_type="issue_pickup",
            started_at="2026-01-01T00:02:00",
            success=False,
        )
    )
    assert isinstance(id1, int)
    assert isinstance(id2, int)
    assert id2 > id1


# ---------------------------------------------------------------------------
# scope_drift_log
# ---------------------------------------------------------------------------


async def test_log_scope_drift_persists_row(store):
    sid, play_id = await _seed(store)
    rec = ScopeDriftRecord(
        session_id=sid,
        play_id=play_id,
        artifact="src/unexpected/file.py",
        reason="no_active_cluster_match",
        logged_at="2026-01-01T00:02:00",
    )
    await store.log_scope_drift(rec)

    rows = await store._conn.execute("SELECT * FROM scope_drift_log WHERE session_id = ?", (sid,))
    results = await rows.fetchall()
    assert len(results) == 1
    assert results[0]["artifact"] == "src/unexpected/file.py"
    assert results[0]["reason"] == "no_active_cluster_match"
    assert results[0]["play_id"] == play_id


async def test_log_scope_drift_fk_requires_existing_play(store):
    sid = "sess-fk"
    await store.create_session(
        SessionRecord(session_id=sid, project_path="/tmp", started_at="2026-01-01T00:00:00")
    )
    rec = ScopeDriftRecord(
        session_id=sid,
        play_id=999999,
        artifact="file.py",
        logged_at="2026-01-01T00:00:01",
    )

    with pytest.raises(aiosqlite.IntegrityError):
        await store.log_scope_drift(rec)


# ---------------------------------------------------------------------------
# human_feedback
# ---------------------------------------------------------------------------


async def test_record_and_list_human_feedback_round_trip(store):
    sid, play_id = await _seed(store)
    rec = HumanFeedbackRecord(
        session_id=sid,
        play_id=play_id,
        trigger="loop_detected",
        feedback_text="Please adjust strategy",
        action_taken="pause_requested",
        created_at="2026-01-01T00:04:00",
    )
    feedback_id = await store.record_human_feedback(rec)
    assert isinstance(feedback_id, int)

    rows = await store.list_human_feedback(sid)
    assert len(rows) == 1
    assert rows[0].feedback_id == feedback_id
    assert rows[0].trigger == "loop_detected"
    assert rows[0].action_taken == "pause_requested"

    assert await store.count_human_feedback(sid) == 1


async def test_reset_session_scoped_tables_clears_human_feedback(store):
    sid, play_id = await _seed(store)
    await store.record_human_feedback(
        HumanFeedbackRecord(
            session_id=sid,
            play_id=play_id,
            trigger="stagnation",
            feedback_text=None,
            action_taken="pause_requested",
            created_at="2026-01-01T00:05:00",
        )
    )
    assert await store.count_human_feedback(sid) == 1

    await store.reset_session_scoped_tables()
    assert await store.count_human_feedback(sid) == 0


# ---------------------------------------------------------------------------
# session_learnings
# ---------------------------------------------------------------------------


async def test_record_learning_returns_id_and_lists_with_confidence_filter(store):
    sid, play_id = await _seed(store)
    now = "2026-01-01T00:00:00"
    lid = await store.record_learning(
        SessionLearningRecord(
            session_id=sid,
            pattern="Always add tests for edge cases",
            category="testing",
            source_play_id=play_id,
            confidence=0.9,
            created_at=now,
            last_reinforced_at=now,
        )
    )
    assert isinstance(lid, int)

    # below threshold — not returned
    await store.record_learning(
        SessionLearningRecord(
            session_id=sid,
            pattern="Low confidence pattern",
            category="testing",
            confidence=0.3,
            created_at=now,
            last_reinforced_at=now,
        )
    )

    results = await store.list_learnings(sid, min_confidence=0.5)
    assert len(results) == 1
    assert results[0].learning_id == lid
    assert results[0].pattern == "Always add tests for edge cases"


async def test_reinforce_learning_increments_count_and_updates_timestamp(store):
    sid, play_id = await _seed(store)
    now = "2026-01-01T00:00:00"
    lid = await store.record_learning(
        SessionLearningRecord(
            session_id=sid,
            pattern="pattern A",
            category="general",
            created_at=now,
            last_reinforced_at=now,
            reinforcement_count=1,
        )
    )
    await store.reinforce_learning(lid)

    results = await store.list_learnings(sid)
    assert len(results) == 1
    assert results[0].reinforcement_count == 2
    assert results[0].last_reinforced_at != now


# ---------------------------------------------------------------------------
# trajectory_snapshots
# ---------------------------------------------------------------------------


async def test_record_trajectory_snapshot_round_trip(store):
    sid, play_id = await _seed(store)
    rec = TrajectorySnapshotRecord(
        session_id=sid,
        play_id=play_id,
        projected_alignment_at_budget_end=0.82,
        estimated_remaining_plays=8,
        estimated_remaining_cost=1.20,
        created_at="2026-01-01T00:05:00",
    )
    await store.record_trajectory_snapshot(rec)

    result = await store.get_latest_trajectory(sid)
    assert result is not None
    assert result.projected_alignment_at_budget_end == pytest.approx(0.82)
    assert result.estimated_remaining_plays == 8


async def test_get_latest_trajectory_returns_none_when_absent(store):
    sid = "sess-no-traj"
    await store.create_session(
        SessionRecord(session_id=sid, project_path="/tmp", started_at="2026-01-01T00:00:00")
    )
    result = await store.get_latest_trajectory(sid)
    assert result is None


# ---------------------------------------------------------------------------
# list_open_pull_requests
# ---------------------------------------------------------------------------


async def test_list_open_pull_requests_returns_only_open(store):
    sid = "sess-prs"
    await store.create_session(
        SessionRecord(session_id=sid, project_path="/tmp", started_at="2026-01-01T00:00:00")
    )

    for pr_num, state in [(1, "open"), (2, "merged"), (3, "review_requested"), (4, "closed")]:
        await store.record_pull_request(
            PullRequestRecord(
                pr_number=pr_num,
                session_id=sid,
                state=state,
                created_at="2026-01-01T00:00:00",
            )
        )

    open_prs = await store.list_open_pull_requests(sid)
    assert {p.pr_number for p in open_prs} == {1, 3}


async def test_pull_request_metadata_round_trips_for_environment_state(store):
    sid = "sess-pr-metadata"
    await store.create_session(
        SessionRecord(session_id=sid, project_path="/tmp", started_at="2026-01-01T00:00:00")
    )

    await store.record_pull_request(
        PullRequestRecord(
            pr_number=42,
            session_id=sid,
            state="open",
            created_at="2026-01-01T00:00:00",
            issue_number=12,
            linked_issue_numbers=(12, 13),
            branch="fix-blocked-flow",
            title="Fix blocked flow",
            url="https://github.com/acme/repo/pull/42",
            github_author="octocat",
            labels=["changes-requested"],
            review_decision="CHANGES_REQUESTED",
            status_check_summary="failed",
            is_draft=False,
            author_agent_id="agent-a",
            author_agent_type="codex",
        )
    )

    active_prs = await store.list_active_pull_requests(sid)

    assert len(active_prs) == 1
    pr = active_prs[0]
    assert pr.pr_number == 42
    assert pr.issue_number == 12
    assert pr.linked_issue_numbers == (12, 13)
    assert pr.title == "Fix blocked flow"
    assert pr.labels == ["changes-requested"]
    assert pr.review_decision == "CHANGES_REQUESTED"
    assert pr.status_check_summary == "failed"
    assert pr.is_draft is False
    assert pr.author_agent_id == "agent-a"


# ---------------------------------------------------------------------------
# update_play
# ---------------------------------------------------------------------------


async def test_update_play_sets_outcome_fields(store):
    sid = "sess-upd"
    await store.create_session(
        SessionRecord(session_id=sid, project_path="/tmp", started_at="2026-01-01T00:00:00")
    )
    play_id = await store.record_play(
        PlayRecord(
            session_id=sid,
            play_type="issue_pickup",
            started_at="2026-01-01T00:00:00",
            success=False,
        )
    )
    await store.update_play(
        play_id,
        success=True,
        ended_at="2026-01-01T00:01:00",
        duration_ms=1200,
        token_cost=500,
        dollar_cost=0.03,
        error=None,
    )
    history = await store.get_play_history(sid)
    assert len(history) == 1
    assert history[0].success is True
    assert history[0].duration_ms == 1200
    assert history[0].dollar_cost == pytest.approx(0.03)
