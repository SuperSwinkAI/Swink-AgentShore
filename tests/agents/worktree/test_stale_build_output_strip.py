"""#522: stale worktrees drop gitignored build output but keep the checkout to TTL."""

from __future__ import annotations

from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.agents.worktree.conftest import _git_sync

from agentshore.agents.worktree import WorktreeManager
from agentshore.agents.worktree import reaper as reaper_mod
from agentshore.agents.worktree.manager import WorktreeAllocation
from agentshore.agents.worktree.reaper import resolve_high_water_mb, strip_ignored_build_output
from agentshore.agents.worktree.registry import insert_worktree, lookup_by_id
from agentshore.config import RuntimeConfig, load_config
from agentshore.data.store import DataStore
from agentshore.state import PlayType


def _stale_worktree_with_build(main_repo: Path, worktree_root: Path, name: str) -> Path:
    (main_repo / ".gitignore").write_text("target/\n")
    _git_sync("add", ".gitignore", cwd=main_repo)
    _git_sync("commit", "-m", "ignore target", cwd=main_repo)
    wt = worktree_root / name
    _git_sync("worktree", "add", "-b", f"{name}-wt", str(wt), "HEAD", cwd=main_repo)
    (wt / "target").mkdir()
    (wt / "target" / "big.bin").write_bytes(b"x" * 1024)
    (wt / "notes.txt").write_text("untracked, not ignored\n")
    return wt


async def _insert_stale(store: DataStore, path: Path, *, key: str, age_s: int = 60) -> int:
    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name=None,
        pre_branch_key=key,
        worktree_path=str(path),
        original_play_type="issue_pickup",
        base_ref="origin/HEAD",
        head_sha=None,
        status="stale",
    )
    ts = (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()
    await store._conn.execute(
        "UPDATE worktrees SET last_used_at = ? WHERE worktree_id = ?", (ts, row.worktree_id)
    )
    await store._conn.commit()
    return row.worktree_id


def _manager(store: DataStore, main_repo: Path, worktree_root: Path) -> WorktreeManager:
    return WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=RuntimeConfig(),
    )


async def test_stale_transition_strips_build_output_and_keeps_checkout_until_ttl(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    wt = _stale_worktree_with_build(main_repo, worktree_root, "pickup-1")
    wid = await _insert_stale(store, wt, key="pickup-1")
    wm = _manager(store, main_repo, worktree_root)

    assert await wm.strip_stale_build_output() == [wid]
    assert not (wt / "target").exists()
    assert (wt / "README.md").exists()
    assert (wt / ".git").is_file()
    assert (wt / "notes.txt").exists()
    # Cleaned once — not revisited on the next tick.
    assert await wm.strip_stale_build_output() == []

    # Within the TTL the checkout survives; past it the dir is reaped as before.
    assert (await wm.reap_closed_prs(ttl_seconds=3600)).total == 0
    assert wt.exists()
    row = await lookup_by_id(store, worktree_id=wid)
    assert row is not None and row.status == "stale"
    assert len((await wm.reap_closed_prs(ttl_seconds=0)).removed) == 1
    assert not wt.exists()


async def test_in_flight_stale_worktree_is_not_cleaned(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    wt = _stale_worktree_with_build(main_repo, worktree_root, "pickup-2")
    wid = await _insert_stale(store, wt, key="pickup-2")
    wm = _manager(store, main_repo, worktree_root)
    alloc = WorktreeAllocation(
        worktree_id=wid,
        path=wt,
        branch_name=None,
        pre_branch_key="pickup-2",
        play_type=PlayType.ISSUE_PICKUP,
        scope="branch_creating",
    )
    wm.register_dispatch(alloc)

    assert await wm.strip_stale_build_output() == []
    assert (wt / "target" / "big.bin").exists()

    # Retried once the dispatch releases.
    wm.release_dispatch(alloc)
    assert await wm.strip_stale_build_output() == [wid]
    assert not (wt / "target").exists()


async def test_stale_row_aliasing_active_db_row_is_not_cleaned(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """#243 alias: a live ``active`` row on the same path protects it (empty registry)."""
    wt = _stale_worktree_with_build(main_repo, worktree_root, "pickup-3")
    await _insert_stale(store, wt, key="pickup-3-old")
    await insert_worktree(
        store,
        session_id="sess-1",
        branch_name=None,
        pre_branch_key="pickup-3-live",
        worktree_path=str(wt),
        original_play_type="issue_pickup",
        base_ref="origin/HEAD",
        head_sha=None,
        status="active",
    )
    wm = _manager(store, main_repo, worktree_root)

    assert await wm.strip_stale_build_output() == []
    assert (wt / "target" / "big.bin").exists()


async def test_strip_refuses_without_worktree_git_link(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    (plain / "target").mkdir(parents=True)
    assert await strip_ignored_build_output(plain) is False
    assert (plain / "target").exists()


_Usage = namedtuple("_Usage", "total used free")


def test_high_water_default_is_relative_with_4gib_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mib = 1024 * 1024
    monkeypatch.setattr(reaper_mod.shutil, "disk_usage", lambda _p: _Usage(460_000 * mib, 0, 0))
    assert resolve_high_water_mb(None, tmp_path) == 46_000
    monkeypatch.setattr(reaper_mod.shutil, "disk_usage", lambda _p: _Usage(20_000 * mib, 0, 0))
    assert resolve_high_water_mb(None, tmp_path) == 4096
    # Explicit values (including 0 = disabled) are honored verbatim.
    assert resolve_high_water_mb(0, tmp_path) == 0
    assert resolve_high_water_mb(1234, tmp_path) == 1234


def test_high_water_omitted_in_yaml_resolves_to_relative_default(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text("project:\n  path: .\n", encoding="utf-8")
    assert load_config(yaml_path).worktrees.disk_high_water_mb is None
    yaml_path.write_text("worktrees:\n  disk_high_water_mb: 0\n", encoding="utf-8")
    assert load_config(yaml_path).worktrees.disk_high_water_mb == 0
