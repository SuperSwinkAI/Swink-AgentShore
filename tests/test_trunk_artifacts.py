"""Tests for deterministic trunk-artifact reclaim (agentshore/core/trunk_artifacts.py).

Covers the #162/#164 fix: untracked root-level files left by trunk-scoped plays
are attributed deterministically and quarantined (moved, never deleted) so they
stop wedging ``merge_pr`` / ``reconcile_state``.

- ``snapshot_untracked_root_artifacts`` sees only depth-1, non-AgentShore ``??``
  files (not tracked files, not subdirs, not sidecars, not quoted names).
- ``attribute_orphan_artifacts`` maps a file to the closed trunk play whose
  window brackets its mtime, skips files an active window could own, and leaves
  pre-window (user-WIP) files alone.
- ``reclaim_artifacts`` moves files into ``.agentshore/reclaimed/<play_id>/`` and
  leaves trunk clean; ``reap_quarantine`` honours the TTL.
- The store helpers (``count_running_trunk_plays``, ``list_trunk_play_windows``)
  and the per-play reclaim hook (``_reclaim_trunk_artifacts_for_play``).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agentshore.core.trunk_artifacts import (
    TRUNK_SCOPED_PLAY_TYPES,
    PlayWindow,
    attribute_orphan_artifacts,
    reap_quarantine,
    reclaim_artifacts,
    snapshot_untracked_root_artifacts,
)
from agentshore.data.store import DataStore, PlayRecord, SessionRecord
from agentshore.state import PlayType


def _init_repo(tmp_path: Path) -> Path:
    """Create a real git repo with one committed file. Portable across OSes."""
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "HOME": str(tmp_path),
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(tmp_path), check=True, env=env)
    for key, val in [("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")]:
        subprocess.run(["git", "config", "--local", key, val], cwd=str(tmp_path), check=True)
    (tmp_path / "src.py").write_text("a = 1\n")
    subprocess.run(["git", "add", "src.py"], cwd=str(tmp_path), check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=str(tmp_path), check=True, env=env)
    return tmp_path


# --- snapshot ----------------------------------------------------------------


def test_snapshot_catches_depth1_untracked_and_ignores_the_rest(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "scratch.json").write_text("{}")  # depth-1 untracked -> caught
    (repo / "src.py").write_text("a = 2\n")  # tracked modification -> ignored
    (repo / "sub").mkdir()
    (repo / "sub" / "deep.json").write_text("{}")  # subdir untracked -> ignored
    (repo / ".agentshore").mkdir()
    (repo / ".agentshore" / "context.json").write_text("{}")  # owned -> ignored

    assert snapshot_untracked_root_artifacts(repo) == {"scratch.json"}


def test_snapshot_empty_for_clean_repo(tmp_path: Path) -> None:
    assert snapshot_untracked_root_artifacts(_init_repo(tmp_path)) == set()


def test_snapshot_empty_for_non_git_dir(tmp_path: Path) -> None:
    # git status fails outside a repo -> empty, never raises.
    assert snapshot_untracked_root_artifacts(tmp_path) == set()


# --- attribute_orphan_artifacts ----------------------------------------------


def _set_mtime(path: Path, epoch: float) -> None:
    os.utime(path, (epoch, epoch))


def test_attribute_assigns_file_to_bracketing_closed_window(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    f = repo / "out.json"
    f.write_text("{}")
    _set_mtime(f, 1000.0)
    owners = [PlayWindow(play_id=42, started_at=990.0, ended_at=1010.0)]
    assert attribute_orphan_artifacts(repo, owner_windows=owners, active_windows=[]) == {
        "out.json": 42
    }


def test_attribute_skips_file_an_active_window_could_own(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    f = repo / "out.json"
    f.write_text("{}")
    _set_mtime(f, 1000.0)
    owners = [PlayWindow(play_id=42, started_at=990.0, ended_at=1010.0)]
    active = [PlayWindow(play_id=99, started_at=995.0, ended_at=None)]  # in-flight
    assert attribute_orphan_artifacts(repo, owner_windows=owners, active_windows=active) == {}


def test_attribute_leaves_pre_window_user_wip(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    f = repo / "user_notes.json"
    f.write_text("{}")
    _set_mtime(f, 500.0)  # older than every play window
    owners = [PlayWindow(play_id=42, started_at=990.0, ended_at=1010.0)]
    assert attribute_orphan_artifacts(repo, owner_windows=owners, active_windows=[]) == {}


def test_attribute_latest_starting_owner_wins(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    f = repo / "out.json"
    f.write_text("{}")
    _set_mtime(f, 1000.0)
    owners = [
        PlayWindow(play_id=1, started_at=900.0, ended_at=1100.0),
        PlayWindow(play_id=2, started_at=995.0, ended_at=1100.0),  # latest start that brackets
    ]
    assert attribute_orphan_artifacts(repo, owner_windows=owners, active_windows=[]) == {
        "out.json": 2
    }


def test_attribute_killed_play_open_window_brackets(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    f = repo / "killed.json"
    f.write_text("{}")
    _set_mtime(f, 2000.0)
    # ended_at None == killed/never-completed; open-ended [started, +inf).
    owners = [PlayWindow(play_id=7, started_at=1900.0, ended_at=None)]
    assert attribute_orphan_artifacts(repo, owner_windows=owners, active_windows=[]) == {
        "killed.json": 7
    }


# --- reclaim_artifacts / reap_quarantine -------------------------------------


def test_reclaim_moves_file_to_quarantine_and_cleans_trunk(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "debris.json").write_text("payload")
    moved = reclaim_artifacts(repo, ["debris.json"], play_id=5)
    assert moved == ["debris.json"]
    quarantined = repo / ".agentshore" / "reclaimed" / "5" / "debris.json"
    assert quarantined.read_text() == "payload"
    assert not (repo / "debris.json").exists()
    # Trunk is clean again (quarantine lives under the owned .agentshore/ prefix).
    assert snapshot_untracked_root_artifacts(repo) == set()


def test_reclaim_skips_missing_and_directories(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "adir").mkdir()
    assert reclaim_artifacts(repo, ["gone.json", "adir"], play_id=1) == []


def test_reap_quarantine_honours_ttl(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    reclaim_artifacts(repo, [], play_id=1)
    old = repo / ".agentshore" / "reclaimed" / "old"
    fresh = repo / ".agentshore" / "reclaimed" / "fresh"
    old.mkdir(parents=True)
    fresh.mkdir(parents=True)
    _set_mtime(old, 1000.0)  # far in the past -> reaped
    assert reap_quarantine(repo, ttl_seconds=3600) == 1
    assert not old.exists()
    assert fresh.exists()


def test_reap_quarantine_disabled_for_nonpositive_ttl(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / ".agentshore" / "reclaimed" / "x").mkdir(parents=True)
    assert reap_quarantine(repo, ttl_seconds=0) == 0


# --- store helpers -----------------------------------------------------------


@pytest.mark.asyncio
async def test_count_running_trunk_plays_counts_open_siblings(tmp_path: Path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await store.create_session(
            SessionRecord(
                session_id="s", project_path=str(tmp_path), started_at="2026-06-12T00:00:00+00:00"
            )
        )
        running = await store.record_play(
            PlayRecord(
                session_id="s",
                play_type=PlayType.MERGE_PR.value,
                started_at="2026-06-12T00:01:00+00:00",
                success=False,
            )
        )
        me = await store.record_play(
            PlayRecord(
                session_id="s",
                play_type=PlayType.CLEANUP.value,
                started_at="2026-06-12T00:02:00+00:00",
                success=False,
            )
        )
        closed = await store.record_play(
            PlayRecord(
                session_id="s",
                play_type=PlayType.RUN_QA.value,
                started_at="2026-06-12T00:00:30+00:00",
                ended_at="2026-06-12T00:00:45+00:00",
                success=True,
            )
        )
        types = [pt.value for pt in TRUNK_SCOPED_PLAY_TYPES]
        # Excluding myself, only the still-open MERGE_PR counts; the RUN_QA is closed.
        assert await store.count_running_trunk_plays("s", exclude_play_id=me, play_types=types) == 1
        # Sanity: the closed row really is closed.
        assert closed != running
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_list_trunk_play_windows_spans_sessions(tmp_path: Path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        for sid in ("s1", "s2"):
            await store.create_session(
                SessionRecord(
                    session_id=sid,
                    project_path=str(tmp_path),
                    started_at="2026-06-12T00:00:00+00:00",
                )
            )
        await store.record_play(
            PlayRecord(
                session_id="s1",
                play_type=PlayType.CLEANUP.value,
                started_at="2026-06-12T00:01:00+00:00",
                ended_at="2026-06-12T00:01:30+00:00",
                success=True,
            )
        )
        await store.record_play(
            PlayRecord(
                session_id="s2",
                play_type=PlayType.ISSUE_PICKUP.value,  # not trunk-scoped -> excluded
                started_at="2026-06-12T00:02:00+00:00",
                success=False,
            )
        )
        windows = await store.list_trunk_play_windows(
            play_types=[pt.value for pt in TRUNK_SCOPED_PLAY_TYPES]
        )
        assert len(windows) == 1
        play_id, started_at, ended_at = windows[0]
        assert started_at == "2026-06-12T00:01:00+00:00"
        assert ended_at == "2026-06-12T00:01:30+00:00"
    finally:
        await store.close()


# --- per-play reclaim hook ---------------------------------------------------


class _FakeCtx:
    """Minimal PlayExecutionContext stand-in backed by a real DataStore + repo."""

    def __init__(self, store: DataStore, project_path: Path, *, play_id: int) -> None:
        self.store = store
        self.project_path = project_path
        self.session_id = "s"
        self.play_id = play_id


@pytest.mark.asyncio
async def test_per_play_hook_reclaims_solo_completion(tmp_path: Path) -> None:
    from agentshore.plays.skill_backed.base import _reclaim_trunk_artifacts_for_play

    repo = _init_repo(tmp_path)
    store = DataStore(repo / ".agentshore" / "agentshore.db")
    (repo / ".agentshore").mkdir(exist_ok=True)
    await store.initialize()
    try:
        await store.create_session(
            SessionRecord(
                session_id="s", project_path=str(repo), started_at="2026-06-12T00:00:00+00:00"
            )
        )
        play_id = await store.record_play(
            PlayRecord(
                session_id="s",
                play_type=PlayType.CLEANUP.value,
                started_at="2026-06-12T00:01:00+00:00",
                success=False,
            )
        )
        ctx = _FakeCtx(store, repo, play_id=play_id)
        pre = snapshot_untracked_root_artifacts(repo)
        (repo / "agent_left_this.json").write_text("{}")  # the play's debris

        await _reclaim_trunk_artifacts_for_play(ctx, PlayType.CLEANUP, pre)

        assert not (repo / "agent_left_this.json").exists()
        quarantined = repo / ".agentshore" / "reclaimed" / str(play_id) / "agent_left_this.json"
        assert quarantined.exists()
        mutation = await store.get_external_mutation("s", f"reclaim:{play_id}:agent_left_this.json")
        assert mutation is not None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_per_play_hook_defers_when_concurrent_trunk_play(tmp_path: Path) -> None:
    from agentshore.plays.skill_backed.base import _reclaim_trunk_artifacts_for_play

    repo = _init_repo(tmp_path)
    (repo / ".agentshore").mkdir(exist_ok=True)
    store = DataStore(repo / ".agentshore" / "agentshore.db")
    await store.initialize()
    try:
        await store.create_session(
            SessionRecord(
                session_id="s", project_path=str(repo), started_at="2026-06-12T00:00:00+00:00"
            )
        )
        # A sibling trunk-scoped play is still running (ended_at NULL).
        await store.record_play(
            PlayRecord(
                session_id="s",
                play_type=PlayType.MERGE_PR.value,
                started_at="2026-06-12T00:00:30+00:00",
                success=False,
            )
        )
        me = await store.record_play(
            PlayRecord(
                session_id="s",
                play_type=PlayType.CLEANUP.value,
                started_at="2026-06-12T00:01:00+00:00",
                success=False,
            )
        )
        ctx = _FakeCtx(store, repo, play_id=me)
        pre = snapshot_untracked_root_artifacts(repo)
        (repo / "ambiguous.json").write_text("{}")

        await _reclaim_trunk_artifacts_for_play(ctx, PlayType.CLEANUP, pre)

        # Deferred: file stays put (the sweep resolves it deterministically later).
        assert (repo / "ambiguous.json").exists()
    finally:
        await store.close()
