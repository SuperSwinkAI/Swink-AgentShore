"""Tests for Phase-6 DataStore extensions: reports and archives.

Covers: get_session, list_sessions, get_agents, list_all_issues,
list_scope_drift, list_trajectory_snapshots, list_review_patterns,
create_archive, list_archives, get_archive.
"""

from __future__ import annotations

import pytest

from agentshore.data import (
    AgentRecord,
    ArchiveRecord,
    DataStore,
    GitHubIssueRecord,
    PlayRecord,
    ReviewFeedbackPatternRecord,
    ScopeDriftRecord,
    SessionRecord,
    TrajectorySnapshotRecord,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path):
    db = DataStore(tmp_path / "test.db")
    await db.initialize()
    yield db
    await db.close()


async def _seed(store: DataStore) -> tuple[str, int]:
    """Create a session + one play row.  Return (session_id, play_id)."""
    sid = "sess-p6-test"
    await store.create_session(
        SessionRecord(
            session_id=sid,
            project_path="/tmp/proj",
            started_at="2026-04-27T00:00:00",
        )
    )
    play_id = await store.record_play(
        PlayRecord(
            session_id=sid,
            play_type="issue_pickup",
            started_at="2026-04-27T00:01:00",
            success=True,
        )
    )
    return sid, play_id


# ---------------------------------------------------------------------------
# get_session
# ---------------------------------------------------------------------------


async def test_get_session_returns_record(store):
    sid = "sess-get"
    await store.create_session(
        SessionRecord(
            session_id=sid,
            project_path="/tmp/proj",
            started_at="2026-04-27T00:00:00",
            status="running",
            total_cost=1.23,
            total_plays=5,
        )
    )
    rec = await store.get_session(sid)
    assert rec is not None
    assert rec.session_id == sid
    assert rec.project_path == "/tmp/proj"
    assert rec.status == "running"
    assert rec.total_cost == pytest.approx(1.23)
    assert rec.total_plays == 5


async def test_get_session_not_found(store):
    rec = await store.get_session("nonexistent-id")
    assert rec is None


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------


async def test_list_sessions_ordered(store):
    for sid, ts in [("s-old", "2026-04-26T00:00:00"), ("s-new", "2026-04-27T00:00:00")]:
        await store.create_session(
            SessionRecord(session_id=sid, project_path="/tmp", started_at=ts)
        )
    sessions = await store.list_sessions()
    assert len(sessions) == 2
    # Most recent first
    assert sessions[0].session_id == "s-new"
    assert sessions[1].session_id == "s-old"


# ---------------------------------------------------------------------------
# get_agents
# ---------------------------------------------------------------------------


async def test_get_agents_for_session(store):
    sid, _ = await _seed(store)
    for aid, ts in [("a1", "2026-04-27T00:01:00"), ("a2", "2026-04-27T00:02:00")]:
        await store.register_agent(
            AgentRecord(agent_id=aid, session_id=sid, agent_type="claude_code", created_at=ts)
        )
    agents = await store.get_agents(sid)
    assert len(agents) == 2
    assert agents[0].agent_id == "a1"
    assert agents[1].agent_id == "a2"
    assert agents[0].agent_type == "claude_code"


async def test_get_agents_empty(store):
    sid = "sess-no-agents"
    await store.create_session(
        SessionRecord(session_id=sid, project_path="/tmp", started_at="2026-04-27T00:00:00")
    )
    agents = await store.get_agents(sid)
    assert agents == []


# ---------------------------------------------------------------------------
# list_all_issues
# ---------------------------------------------------------------------------


async def test_list_all_issues(store):
    sid, _ = await _seed(store)
    issues = [
        GitHubIssueRecord(
            issue_number=1,
            session_id=sid,
            title="Open bug",
            state="open",
            created_at="2026-04-27T00:00:00",
        ),
        GitHubIssueRecord(
            issue_number=2,
            session_id=sid,
            title="Closed feature",
            state="closed",
            created_at="2026-04-27T00:00:00",
            closed_at="2026-04-27T01:00:00",
        ),
    ]
    await store.cache_github_issues(sid, issues)

    all_issues = await store.list_all_issues(sid)
    assert len(all_issues) == 2
    states = {i.state for i in all_issues}
    assert states == {"open", "closed"}


# ---------------------------------------------------------------------------
# list_scope_drift
# ---------------------------------------------------------------------------


async def test_list_scope_drift(store):
    sid, play_id = await _seed(store)
    for i, ts in enumerate(["2026-04-27T00:02:00", "2026-04-27T00:03:00"]):
        await store.log_scope_drift(
            ScopeDriftRecord(
                session_id=sid,
                play_id=play_id,
                artifact=f"file_{i}.py",
                reason="drift",
                logged_at=ts,
            )
        )
    drifts = await store.list_scope_drift(sid)
    assert len(drifts) == 2
    # Ordered by logged_at ASC
    assert drifts[0].artifact == "file_0.py"
    assert drifts[1].artifact == "file_1.py"


# ---------------------------------------------------------------------------
# list_trajectory_snapshots
# ---------------------------------------------------------------------------


async def test_list_trajectory_snapshots(store):
    sid, play_id = await _seed(store)
    play_id2 = await store.record_play(
        PlayRecord(
            session_id=sid,
            play_type="code_review",
            started_at="2026-04-27T00:02:00",
            success=True,
        )
    )
    for pid in [play_id, play_id2]:
        await store.record_trajectory_snapshot(
            TrajectorySnapshotRecord(
                session_id=sid,
                play_id=pid,
                projected_alignment_at_budget_end=0.8,
                estimated_remaining_plays=5,
                estimated_remaining_cost=1.0,
                created_at="2026-04-27T00:05:00",
            )
        )
    snapshots = await store.list_trajectory_snapshots(sid)
    assert len(snapshots) == 2
    # Ordered by play_id ASC
    assert snapshots[0].play_id == play_id
    assert snapshots[1].play_id == play_id2


# ---------------------------------------------------------------------------
# list_review_patterns
# ---------------------------------------------------------------------------


async def test_list_review_patterns(store):
    sid, play_id = await _seed(store)
    for freq in [3, 7, 1]:
        await store.record_review_pattern(
            ReviewFeedbackPatternRecord(
                session_id=sid,
                play_id=play_id,
                pattern=f"pattern-freq-{freq}",
                category="style",
                frequency=freq,
                created_at="2026-04-27T00:05:00",
            )
        )
    patterns = await store.list_review_patterns(sid)
    assert len(patterns) == 3
    # Ordered by frequency DESC
    assert patterns[0].frequency == 7
    assert patterns[1].frequency == 3
    assert patterns[2].frequency == 1


async def test_list_review_patterns_empty(store):
    sid = "sess-no-patterns"
    await store.create_session(
        SessionRecord(session_id=sid, project_path="/tmp", started_at="2026-04-27T00:00:00")
    )
    patterns = await store.list_review_patterns(sid)
    assert patterns == []


async def test_mark_review_patterns_injected(store):
    sid, play_id = await _seed(store)
    await store.record_review_pattern(
        ReviewFeedbackPatternRecord(
            session_id=sid,
            play_id=play_id,
            pattern="require regression test",
            category="testing",
            frequency=2,
            injected=False,
            created_at="2026-04-27T00:05:00",
        )
    )
    await store.record_review_pattern(
        ReviewFeedbackPatternRecord(
            session_id=sid,
            play_id=play_id,
            pattern="simplify conditional branch",
            category="readability",
            frequency=1,
            injected=False,
            created_at="2026-04-27T00:05:00",
        )
    )
    before = await store.list_review_patterns(sid)
    target_id = before[0].pattern_id
    assert isinstance(target_id, int)
    await store.mark_review_patterns_injected(sid, [target_id])
    after = await store.list_review_patterns(sid)
    by_id = {p.pattern_id: p for p in after}
    assert by_id[target_id].injected is True


async def test_record_review_pattern_deduplicates_cross_play(store):
    """Same (session_id, pattern, category) from two plays accumulates frequency."""
    sid, play_id = await _seed(store)
    play_id2 = await store.record_play(
        PlayRecord(
            session_id=sid,
            play_type="code_review",
            started_at="2026-04-27T00:02:00",
            success=True,
        )
    )
    await store.record_review_pattern(
        ReviewFeedbackPatternRecord(
            session_id=sid,
            play_id=play_id,
            pattern="add type annotations",
            category="style",
            frequency=2,
            created_at="2026-04-27T00:05:00",
        )
    )
    await store.record_review_pattern(
        ReviewFeedbackPatternRecord(
            session_id=sid,
            play_id=play_id2,
            pattern="add type annotations",
            category="style",
            frequency=3,
            created_at="2026-04-27T00:10:00",
        )
    )
    patterns = await store.list_review_patterns(sid)
    assert len(patterns) == 1
    assert patterns[0].pattern == "add type annotations"
    assert patterns[0].frequency == 5


async def test_record_review_pattern_different_categories_not_merged(store):
    """Same pattern text with different categories produces two distinct rows."""
    sid, play_id = await _seed(store)
    await store.record_review_pattern(
        ReviewFeedbackPatternRecord(
            session_id=sid,
            play_id=play_id,
            pattern="missing error handling",
            category="correctness",
            frequency=1,
            created_at="2026-04-27T00:05:00",
        )
    )
    await store.record_review_pattern(
        ReviewFeedbackPatternRecord(
            session_id=sid,
            play_id=play_id,
            pattern="missing error handling",
            category="style",
            frequency=1,
            created_at="2026-04-27T00:05:00",
        )
    )
    patterns = await store.list_review_patterns(sid)
    assert len(patterns) == 2
    categories = {p.category for p in patterns}
    assert categories == {"correctness", "style"}


# ---------------------------------------------------------------------------
# create_archive / list_archives / get_archive
# ---------------------------------------------------------------------------


async def test_create_archive(store):
    sid, _ = await _seed(store)
    rec = ArchiveRecord(
        archive_id="arc-001",
        session_id=sid,
        archive_path="/tmp/archives/arc-001.tar.gz",
        total_cost=4.56,
        final_alignment=0.92,
        total_plays=12,
        created_at="2026-04-27T01:00:00",
        issues_closed=3,
        issues_created=1,
    )
    await store.create_archive(rec)
    fetched = await store.get_archive("arc-001")
    assert fetched is not None
    assert fetched.archive_id == "arc-001"
    assert fetched.session_id == sid
    assert fetched.archive_path == "/tmp/archives/arc-001.tar.gz"
    assert fetched.total_cost == pytest.approx(4.56)
    assert fetched.final_alignment == pytest.approx(0.92)
    assert fetched.total_plays == 12
    assert fetched.issues_closed == 3
    assert fetched.issues_created == 1


async def test_list_archives_ordered(store):
    sid, _ = await _seed(store)
    for aid, ts in [("arc-old", "2026-04-26T00:00:00"), ("arc-new", "2026-04-27T00:00:00")]:
        await store.create_archive(
            ArchiveRecord(
                archive_id=aid,
                session_id=sid,
                archive_path=f"/tmp/{aid}.tar.gz",
                total_cost=1.0,
                final_alignment=0.5,
                total_plays=5,
                created_at=ts,
            )
        )
    archives = await store.list_archives()
    assert len(archives) == 2
    # Most recent first
    assert archives[0].archive_id == "arc-new"
    assert archives[1].archive_id == "arc-old"


async def test_get_archive(store):
    sid, _ = await _seed(store)
    await store.create_archive(
        ArchiveRecord(
            archive_id="arc-fetch",
            session_id=sid,
            archive_path="/tmp/arc.tar.gz",
            total_cost=2.0,
            final_alignment=0.8,
            total_plays=10,
            created_at="2026-04-27T00:00:00",
        )
    )
    rec = await store.get_archive("arc-fetch")
    assert rec is not None
    assert rec.archive_id == "arc-fetch"


async def test_get_archive_not_found(store):
    rec = await store.get_archive("nonexistent-arc")
    assert rec is None


async def test_list_archives_empty(store):
    archives = await store.list_archives()
    assert archives == []
