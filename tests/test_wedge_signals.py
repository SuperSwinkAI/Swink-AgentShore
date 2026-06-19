"""Tests for wedge-signals diagnostic helpers (agentshore/core/wedge_signals.py).

Covers:
- The AgentShore-owned-sidecar filter used to exclude ``.agentshore/``, ``.beads/``,
  ``.agents/``, ``agentshore.yaml``, and ``timelapse-runs/`` from dirty-trunk
  detection (these are runtime state, not user work — same allowlist as the
  fix needed for AgentShore #594).
- Best-effort behavior: every helper returns ``[]`` on missing files,
  unreadable DBs, or subprocess errors. The skill never blocks on a stale
  ``.agentshore/agentshore.db`` or a degraded git invocation.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentshore.core.wedge_signals import (
    SESSION_START_DIRTY_SIDECAR,
    _path_is_agentshore_owned,
    build_recent_wedge_signals,
    collect_dirty_trunk_paths,
    collect_orphan_worktree_paths,
    collect_recent_failed_plays,
    collect_recent_worktree_paths,
    write_session_start_dirty_baseline,
)
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    BudgetSnapshot,
    OrchestratorState,
    PlayType,
    SessionState,
)

# --- agentshore-owned filter ---------------------------------------------------


def test_agentshore_owned_filter_matches_exact_yaml() -> None:
    assert _path_is_agentshore_owned("agentshore.yaml") is True


def test_agentshore_owned_filter_matches_directory_prefixes() -> None:
    assert _path_is_agentshore_owned(".agentshore/context.json") is True
    assert _path_is_agentshore_owned(".beads/issues/1.json") is True
    assert _path_is_agentshore_owned(".agents/skills/agentshore-cleanup/SKILL.md") is True
    assert _path_is_agentshore_owned("timelapse-runs/2026-05-23/frame-0.png") is True


def test_agentshore_owned_filter_passes_user_paths() -> None:
    assert _path_is_agentshore_owned("src/foo.py") is False
    assert _path_is_agentshore_owned("tests/test_foo.py") is False
    assert _path_is_agentshore_owned("README.md") is False
    # ``.agentshoreX`` is not the same as ``.agentshore/`` — must require the slash.
    assert _path_is_agentshore_owned(".agentshoreX/foo") is False


# --- collect_dirty_trunk_paths: error paths ---------------------------------


def test_dirty_paths_returns_empty_for_non_git_dir(tmp_path: Path) -> None:
    """A non-git directory yields ``[]`` (git status fails) — never raises."""
    assert collect_dirty_trunk_paths(tmp_path) == []


# --- collect_orphan_worktree_paths: error paths -----------------------------


def test_orphan_worktrees_returns_empty_without_session(tmp_path: Path) -> None:
    """No session_id → can't query DB → returns ``[]``."""
    assert collect_orphan_worktree_paths(tmp_path, session_id=None) == []


def test_orphan_worktrees_returns_empty_without_db(tmp_path: Path) -> None:
    """Missing ``.agentshore/agentshore.db`` → returns ``[]``."""
    assert collect_orphan_worktree_paths(tmp_path, session_id="sess-x") == []


# --- collect_recent_failed_plays: error paths -------------------------------


def test_recent_fails_returns_empty_without_session(tmp_path: Path) -> None:
    assert collect_recent_failed_plays(tmp_path, session_id=None) == []


def test_recent_fails_returns_empty_without_db(tmp_path: Path) -> None:
    assert collect_recent_failed_plays(tmp_path, session_id="sess-x") == []


# --- collect_recent_worktree_paths -----------------------------------------


def test_recent_worktrees_returns_paths_created_inside_age_guard(tmp_path: Path) -> None:
    db_path = tmp_path / "agentshore.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE worktrees (
                worktree_id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                worktree_path TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
        rows = [
            (1, "sess", "/tmp/young", "active", (now - timedelta(hours=2)).isoformat()),
            (2, "sess", "/tmp/old", "active", (now - timedelta(hours=4)).isoformat()),
            (3, "sess", "/tmp/bad-ts", "active", "not-a-timestamp"),
            (4, "sess", "/tmp/reaped", "reaped", (now - timedelta(minutes=5)).isoformat()),
        ]
        conn.executemany(
            "INSERT INTO worktrees VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    assert collect_recent_worktree_paths(
        tmp_path,
        db_path=db_path,
        now=now,
        min_age_hours=3,
    ) == ["/tmp/young", "/tmp/bad-ts"]


def test_recent_worktrees_returns_empty_without_db(tmp_path: Path) -> None:
    assert collect_recent_worktree_paths(tmp_path, session_id="sess-x") == []


# --- build_recent_wedge_signals: structure ----------------------------------


def _bare_state(failure_streak: int = 0) -> OrchestratorState:
    return OrchestratorState(
        session_id="sess",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        budget=BudgetSnapshot(5.0, 0.0, 5.0, 0.1),
        same_type_failure_streak=failure_streak,
        last_play_type=PlayType.MERGE_PR,
    )


def test_build_signals_returns_complete_shape_on_empty_project(tmp_path: Path) -> None:
    """All keys are present even when nothing can be collected — skill prompt stable."""
    signals = build_recent_wedge_signals(
        _bare_state(failure_streak=5),
        tmp_path,
        session_id="sess-x",
    )
    assert signals["same_type_failure_streak"] == 5
    assert signals["last_play_type"] == "merge_pr"
    assert signals["last_failed_play_type"] is None
    assert signals["last_failed_error"] is None
    assert signals["recent_failed_plays"] == []
    assert signals["dirty_trunk_paths"] == []
    assert signals["orphan_worktree_paths"] == []


def test_build_signals_carries_failure_streak() -> None:
    """The state-derived counters flow through into the signal block."""
    signals = build_recent_wedge_signals(
        _bare_state(failure_streak=7),
        Path("/tmp/does-not-exist"),
        session_id="sess-x",
    )
    assert signals["same_type_failure_streak"] == 7


# --- collect_dirty_trunk_paths: live git filter (integration) ---------------


def _init_repo_with_dirty_state(tmp_path: Path) -> Path:
    """Create a real git repo with mixed user/agentshore-owned dirty paths."""
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=str(tmp_path),
        check=True,
        env={
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(tmp_path),
        },
    )
    # Configure committer locally so the seed commit succeeds without a global git config.
    for key, val in [("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")]:
        subprocess.run(["git", "config", "--local", key, val], cwd=str(tmp_path), check=True)
    (tmp_path / "src.py").write_text("a = 1\n")
    subprocess.run(["git", "add", "src.py"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=str(tmp_path), check=True)
    # Modify a tracked file + sprinkle AgentShore-owned untracked sidecars.
    (tmp_path / "src.py").write_text("a = 2\n")
    (tmp_path / ".agentshore").mkdir()
    (tmp_path / ".agentshore" / "context.json").write_text("{}")
    (tmp_path / "agentshore.yaml").write_text("agents: {}")
    return tmp_path


def test_dirty_paths_excludes_agentshore_owned_sidecars(tmp_path: Path) -> None:
    repo = _init_repo_with_dirty_state(tmp_path)
    entries = collect_dirty_trunk_paths(repo)
    paths = {e.path for e in entries}
    # The tracked-file modification stays.
    assert "src.py" in paths
    # The AgentShore-owned untracked sidecars are filtered out.
    assert ".agentshore/" not in paths
    assert ".agentshore/context.json" not in paths
    assert "agentshore.yaml" not in paths


# --- write_session_start_dirty_baseline -------------------------------------


def test_baseline_returns_none_without_agentshore_dir(tmp_path: Path) -> None:
    """No ``.agentshore/`` → no sidecar (graceful no-op)."""
    result = write_session_start_dirty_baseline(
        tmp_path,
        session_id="s1",
        session_start_utc="2026-05-23T23:00:00Z",
    )
    assert result is None


def test_baseline_writes_empty_modified_paths_on_clean_trunk(tmp_path: Path) -> None:
    """Clean trunk still writes the sidecar so the skill can confirm `baseline captured`."""
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=str(tmp_path),
        check=True,
        env={
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(tmp_path),
        },
    )
    (tmp_path / ".agentshore").mkdir()

    dest = write_session_start_dirty_baseline(
        tmp_path,
        session_id="s1",
        session_start_utc="2026-05-23T23:00:00Z",
    )
    assert dest is not None
    assert dest.name == SESSION_START_DIRTY_SIDECAR
    payload = json.loads(dest.read_text())
    assert payload["session_id"] == "s1"
    assert payload["modified_paths"] == []
    assert payload["summary"]["count"] == 0


def test_baseline_captures_pre_session_dirt_with_mtime_clustering(
    tmp_path: Path,
) -> None:
    """Pre-session dirty files are enriched with mtime + size + cluster stats."""
    repo = _init_repo_with_dirty_state(tmp_path)
    (repo / ".agentshore").exists()  # established by _init_repo_with_dirty_state

    # Backdate the modified file so mtime predates the session start timestamp.
    src_mtime = 1779570000  # 2026-05-23T20:20:00 UTC
    os.utime(repo / "src.py", (src_mtime, src_mtime))

    session_start = "2026-05-23T23:00:00Z"  # well after the file mtime
    dest = write_session_start_dirty_baseline(
        repo, session_id="s2", session_start_utc=session_start
    )
    assert dest is not None
    payload = json.loads(dest.read_text())

    assert payload["session_id"] == "s2"
    assert payload["session_start_utc"] == session_start

    paths = {e["path"]: e for e in payload["modified_paths"]}
    assert "src.py" in paths
    entry = paths["src.py"]
    assert "mtime_utc" in entry
    assert entry["mtime_utc"].endswith("Z")
    assert entry["size_bytes"] > 0
    assert entry["status"]

    summary = payload["summary"]
    assert summary["count"] == len(paths)
    assert summary["pre_session"] is True
    assert summary["mtime_cluster_span_seconds"] >= 0


def test_baseline_pre_session_false_when_file_modified_after_start(
    tmp_path: Path,
) -> None:
    """If a dirty file's mtime is newer than session_start, pre_session=False."""
    repo = _init_repo_with_dirty_state(tmp_path)
    # Touch the dirty file forward to simulate an in-session write.
    future_mtime = 1779600000  # 2026-05-24T04:40:00 UTC
    os.utime(repo / "src.py", (future_mtime, future_mtime))

    session_start = "2026-05-23T20:00:00Z"  # before the mtime
    dest = write_session_start_dirty_baseline(
        repo, session_id="s3", session_start_utc=session_start
    )
    assert dest is not None
    summary = json.loads(dest.read_text())["summary"]
    assert summary["pre_session"] is False


def test_baseline_overwrites_prior_sidecar(tmp_path: Path) -> None:
    """Re-running baseline writes a fresh file (no stale append)."""
    repo = _init_repo_with_dirty_state(tmp_path)
    write_session_start_dirty_baseline(
        repo, session_id="s4-old", session_start_utc="2026-05-23T01:00:00Z"
    )
    dest = write_session_start_dirty_baseline(
        repo, session_id="s4-new", session_start_utc="2026-05-23T02:00:00Z"
    )
    assert dest is not None
    payload = json.loads(dest.read_text())
    assert payload["session_id"] == "s4-new"
    assert payload["session_start_utc"] == "2026-05-23T02:00:00Z"


# --- owned_by_active_play annotation (#162) ---------------------------------


def _agent_running(play_type: PlayType, started_at: str) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id="a1",
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.BUSY,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
        current_play_type=play_type,
        current_play_id=99,
        current_play_started_at=started_at,
    )


def _state_with_agent(agent: AgentSnapshot) -> OrchestratorState:
    return OrchestratorState(
        session_id="sess",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        budget=BudgetSnapshot(5.0, 0.0, 5.0, 0.1),
        agents=[agent],
    )


def _dirty_entry(signals: dict, path: str) -> dict:
    return next(e for e in signals["dirty_trunk_paths"] if e["path"] == path)


def test_untracked_root_owned_by_active_trunk_play(tmp_path: Path) -> None:
    """An untracked root file newer than an active trunk play's start is in-flight work."""
    repo = _init_repo_with_dirty_state(tmp_path)
    artifact = repo / "in_flight.json"
    artifact.write_text("{}")
    os.utime(artifact, (2_000_000_000, 2_000_000_000))  # 2033 — newer than play start
    agent = _agent_running(PlayType.WRITE_IMPLEMENTATION_PLAN, "2030-01-01T00:00:00Z")
    signals = build_recent_wedge_signals(_state_with_agent(agent), repo, session_id="sess")
    assert _dirty_entry(signals, "in_flight.json")["owned_by_active_play"] is True


def test_untracked_root_not_owned_when_no_active_trunk_play(tmp_path: Path) -> None:
    """Same file, but the active play is not trunk-scoped -> not owned."""
    repo = _init_repo_with_dirty_state(tmp_path)
    artifact = repo / "orphan.json"
    artifact.write_text("{}")
    os.utime(artifact, (2_000_000_000, 2_000_000_000))
    agent = _agent_running(PlayType.ISSUE_PICKUP, "2030-01-01T00:00:00Z")  # not trunk-scoped
    signals = build_recent_wedge_signals(_state_with_agent(agent), repo, session_id="sess")
    assert _dirty_entry(signals, "orphan.json")["owned_by_active_play"] is False


def test_untracked_root_not_owned_when_predates_active_play(tmp_path: Path) -> None:
    """A file older than the active trunk play's start cannot be its in-flight output."""
    repo = _init_repo_with_dirty_state(tmp_path)
    artifact = repo / "older.json"
    artifact.write_text("{}")
    os.utime(artifact, (1_000_000_000, 1_000_000_000))  # 2001 — older than play start
    agent = _agent_running(PlayType.CLEANUP, "2030-01-01T00:00:00Z")
    signals = build_recent_wedge_signals(_state_with_agent(agent), repo, session_id="sess")
    assert _dirty_entry(signals, "older.json")["owned_by_active_play"] is False


def test_tracked_modification_owned_by_active_trunk_play(tmp_path: Path) -> None:
    """#224: a tracked (M) file newer than an active trunk play's start is in-flight
    work too — not just untracked root artifacts."""
    repo = _init_repo_with_dirty_state(tmp_path)  # src.py is tracked + modified (status M)
    os.utime(repo / "src.py", (2_000_000_000, 2_000_000_000))  # 2033 — newer than play start
    agent = _agent_running(PlayType.WRITE_IMPLEMENTATION_PLAN, "2030-01-01T00:00:00Z")
    signals = build_recent_wedge_signals(_state_with_agent(agent), repo, session_id="sess")
    assert _dirty_entry(signals, "src.py")["owned_by_active_play"] is True


def test_tracked_modification_not_owned_when_predates_active_play(tmp_path: Path) -> None:
    """A tracked file older than the active trunk play's start is not its in-flight output."""
    repo = _init_repo_with_dirty_state(tmp_path)
    os.utime(repo / "src.py", (1_000_000_000, 1_000_000_000))  # 2001 — older than play start
    agent = _agent_running(PlayType.WRITE_IMPLEMENTATION_PLAN, "2030-01-01T00:00:00Z")
    signals = build_recent_wedge_signals(_state_with_agent(agent), repo, session_id="sess")
    assert _dirty_entry(signals, "src.py")["owned_by_active_play"] is False
