"""Tests for worktree reconciliation + orphan deletion.

Closes #570 — sessions used to permanently block any PR-scoped allocation
at a path where a prior session left an unregistered directory. The
``ensure_worktree`` raise on ``target_path_dirty`` would fire indefinitely;
93 consecutive code_review failures observed in example-project session
c78d7074 (2026-05-22). The behaviour now **deletes** a clean orphan and
proceeds, so allocation converges to a clean registered worktree from any
starting state. An orphan that still holds uncommitted work is **quarantined**
— its working tree is moved under ``.agentshore/reclaimed/`` (recoverable) and
the orphan removed so allocation never wedges (#164); only a quarantine failure
leaves it in place for manual resolution.

These tests exercise the **real** ``git`` binary against the per-test
``main_repo`` + ``worktree_root`` fixtures from ``conftest.py``. No mocks
of git or filesystem — schema/git bugs that mocks would hide are surfaced.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agentshore.agents.worktree import allocator as alloc_mod
from agentshore.agents.worktree.allocator import (
    ReconcileReport,
    WorktreeAllocationFailed,
    _dispose_orphan,
    _unique_reclaim_dest,
    ensure_worktree,
    reconcile_worktrees,
)

pytestmark = pytest.mark.asyncio


async def test_unique_reclaim_dest_suffix_bumps_on_collision(tmp_path: Path) -> None:
    """A second orphan with the same dir name lands at ``<name>-1``, not on top."""
    root = tmp_path / "reclaimed"
    root.mkdir()
    assert _unique_reclaim_dest(root, "feature-x") == root / "feature-x"
    (root / "feature-x").mkdir()
    assert _unique_reclaim_dest(root, "feature-x") == root / "feature-x-1"
    (root / "feature-x-1").mkdir()
    assert _unique_reclaim_dest(root, "feature-x") == root / "feature-x-2"


# --- _dispose_orphan ----------------------------------------------------------


async def test_dispose_orphan_deletes_clean_worktree(
    main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    """A clean (introspectable) worktree is deleted outright."""
    target = worktree_root / "clean-feature"
    await ensure_worktree(
        main_repo=main_repo,
        worktree_path=target,
        branch_name=remote_branch,
        base_ref=f"origin/{remote_branch}",
        fetch=True,
    )
    assert target.exists()

    result = await _dispose_orphan(
        main_repo=main_repo, path=target, reclaimed_root=worktree_root.parent / "reclaimed"
    )

    assert result == "deleted"
    assert not target.exists()


async def test_dispose_orphan_quarantines_tracked_uncommitted_changes(
    main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    """An orphan with uncommitted changes to a TRACKED file is quarantined, not
    blocked: its working tree is moved under ``reclaimed/`` (recoverable) and the
    orphan is removed so allocation can proceed (#164)."""
    target = worktree_root / "dirty-feature"
    await ensure_worktree(
        main_repo=main_repo,
        worktree_path=target,
        branch_name=remote_branch,
        base_ref=f"origin/{remote_branch}",
        fetch=True,
    )
    # Modify a tracked file (README.md is committed on the base branch). Only
    # changes to tracked files count as uncommitted work worth recovering.
    readme = target / "README.md"
    readme.write_text(readme.read_text() + "locally modified, uncommitted\n")

    reclaimed_root = worktree_root.parent / "reclaimed"
    result = await _dispose_orphan(main_repo=main_repo, path=target, reclaimed_root=reclaimed_root)

    assert result == "quarantined"
    assert not target.exists(), "orphan must be removed so allocation unblocks"
    salvaged = reclaimed_root / "dirty-feature" / "README.md"
    assert salvaged.exists(), "uncommitted work must be recoverable under reclaimed/"
    assert "locally modified, uncommitted" in salvaged.read_text()


async def test_dispose_orphan_preserves_when_quarantine_fails(
    main_repo: Path, worktree_root: Path, remote_branch: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If quarantine itself fails, the dirty orphan is left in place (never lost)."""
    target = worktree_root / "dirty-feature"
    await ensure_worktree(
        main_repo=main_repo,
        worktree_path=target,
        branch_name=remote_branch,
        base_ref=f"origin/{remote_branch}",
        fetch=True,
    )
    readme = target / "README.md"
    readme.write_text(readme.read_text() + "locally modified, uncommitted\n")

    async def fail_quarantine(*, main_repo: Path, path: Path, reclaimed_root: Path) -> bool:
        return False

    monkeypatch.setattr(alloc_mod, "_quarantine_dirty_orphan", fail_quarantine)

    result = await _dispose_orphan(
        main_repo=main_repo, path=target, reclaimed_root=worktree_root.parent / "reclaimed"
    )

    assert result == "preserved"
    assert target.exists(), "work must be left in place when quarantine fails"
    assert "locally modified, uncommitted" in readme.read_text()


async def test_dispose_orphan_deletes_untracked_only_worktree(
    main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    """An orphan whose only "dirt" is UNTRACKED files is deleted, not preserved.

    Every worktree carries untracked agent-harness scaffolding (the files the
    agent CLIs create at dispatch). Counting those as uncommitted work used to
    preserve every orphan forever and permanently wedge any play needing that
    branch. Untracked files never block disposal -- committed work is safe in
    git, and only changes to *tracked* files count. This is filename-agnostic:
    arbitrary untracked files (no scaffolding names assumed) must not block.
    """
    target = worktree_root / "scaffolded-feature"
    await ensure_worktree(
        main_repo=main_repo,
        worktree_path=target,
        branch_name=remote_branch,
        base_ref=f"origin/{remote_branch}",
        fetch=True,
    )
    # Stand-ins for untracked agent-harness scaffolding (.claude/, AGENTS.md,
    # CLAUDE.md seen in the wild) -- arbitrary untracked paths, nothing special.
    (target / "AGENTS.md").write_text("agent harness scaffolding\n")
    (target / ".claude").mkdir()
    (target / ".claude" / "settings.json").write_text("{}\n")

    result = await _dispose_orphan(
        main_repo=main_repo, path=target, reclaimed_root=worktree_root.parent / "reclaimed"
    )

    assert result == "deleted"
    assert not target.exists()


async def test_dispose_orphan_deletes_non_git_debris(worktree_root: Path, main_repo: Path) -> None:
    """A dir git can't introspect (no valid worktree linkage) is treated as debris and deleted."""
    debris = worktree_root / "detached-debris"
    debris.mkdir()
    (debris / "target").mkdir()  # rebuildable build-cache stand-in
    (debris / "stale.txt").write_text("leftover\n")

    result = await _dispose_orphan(
        main_repo=main_repo, path=debris, reclaimed_root=worktree_root.parent / "reclaimed"
    )

    assert result == "deleted"
    assert not debris.exists()


# --- reconcile_worktrees ------------------------------------------------------


async def test_reconcile_deletes_orphan_leaves_registered_alone(
    main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    """Registered worktrees stay; unregistered (clean) dirs get deleted."""
    registered = worktree_root / "registered-feature-x"
    await ensure_worktree(
        main_repo=main_repo,
        worktree_path=registered,
        branch_name=remote_branch,
        base_ref=f"origin/{remote_branch}",
        fetch=True,
    )
    assert registered.exists()
    # One orphan dir not registered with git.
    orphan = worktree_root / "orphan-from-prior-session"
    orphan.mkdir()
    (orphan / "junk.txt").write_text("hi\n")

    report = await reconcile_worktrees(main_repo=main_repo, worktree_root=worktree_root)

    assert isinstance(report, ReconcileReport)
    assert orphan in report.deleted
    assert report.preserved_dirty == []
    assert not orphan.exists(), "clean orphan should have been deleted"
    assert registered.exists(), "registered worktree must not be touched"
    # No quarantine sibling dir is created.
    sibling = worktree_root.with_name(worktree_root.name + "-orphan")
    assert not sibling.exists()


async def test_reconcile_is_idempotent(main_repo: Path, worktree_root: Path) -> None:
    """Running reconcile twice: second run has nothing to do (clean orphan gone)."""
    orphan = worktree_root / "lonely-orphan"
    orphan.mkdir()
    (orphan / "file").write_text("x")

    first = await reconcile_worktrees(main_repo=main_repo, worktree_root=worktree_root)
    second = await reconcile_worktrees(main_repo=main_repo, worktree_root=worktree_root)

    assert orphan in first.deleted
    assert second.deleted == []
    assert second.preserved_dirty == []


async def test_reconcile_no_worktree_root_returns_empty(main_repo: Path, tmp_path: Path) -> None:
    """Brand-new machine with no worktree_root yet → empty report, no crash."""
    nonexistent = tmp_path / "never-created" / "agentshore-worktrees" / "repo"
    report = await reconcile_worktrees(main_repo=main_repo, worktree_root=nonexistent)
    assert report.deleted == []
    assert report.preserved_dirty == []


async def test_reconcile_ignores_files_in_worktree_root(
    main_repo: Path, worktree_root: Path
) -> None:
    """A loose file at the root level isn't a directory; reconcile leaves it."""
    stray = worktree_root / "stray.txt"
    stray.write_text("not a worktree\n")
    report = await reconcile_worktrees(main_repo=main_repo, worktree_root=worktree_root)
    assert report.deleted == []
    assert report.preserved_dirty == []
    assert stray.exists()


# --- heal-on-allocate (ensure_worktree converges from orphan state) ----------


async def test_ensure_worktree_deletes_clean_orphan_at_target_path(
    main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    """A clean orphan dir at the target path is deleted; allocate proceeds.

    Before the fix, ``ensure_worktree`` raised ``WorktreeAllocationFailed``
    with reason ``target_path_dirty`` — permanently blocking every retry.
    Now it deletes the rebuildable debris and creates the registered worktree.
    """
    target = worktree_root / "feature-x"
    target.mkdir()
    (target / "old_file.txt").write_text("from a prior session\n")

    result = await ensure_worktree(
        main_repo=main_repo,
        worktree_path=target,
        branch_name=remote_branch,
        base_ref=f"origin/{remote_branch}",
        fetch=True,
    )

    assert result.created is True
    assert (target / ".git").exists(), "target should now be a git worktree"
    assert not (target / "old_file.txt").exists(), "debris should be gone, not preserved"
    # No quarantine sibling dir is created.
    sibling = worktree_root.with_name(worktree_root.name + "-orphan")
    assert not sibling.exists()


async def test_ensure_worktree_quarantines_dirty_orphan_and_proceeds(
    main_repo: Path,
    worktree_root: Path,
    remote_branch: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a dirty orphan at the target is quarantined, allocation proceeds —
    no more "resolve manually" wedge (#164). The real move-then-remove is
    covered by ``test_dispose_orphan_quarantines_tracked_uncommitted_changes``;
    here we assert ``ensure_worktree`` treats ``"quarantined"`` as success.
    """
    target = worktree_root / "feature-x"
    target.mkdir()
    (target / "uncommitted.txt").write_text("unsaved\n")

    captured: dict[str, Path] = {}

    async def fake_dispose(*, main_repo: Path, path: Path, reclaimed_root: Path) -> str:
        captured["reclaimed_root"] = reclaimed_root
        # Stand in for the real quarantine: remove the orphan dir so the
        # subsequent ``git worktree add`` has a clean target path.
        shutil.rmtree(path, ignore_errors=True)
        return "quarantined"

    monkeypatch.setattr(alloc_mod, "_dispose_orphan", fake_dispose)

    result = await ensure_worktree(
        main_repo=main_repo,
        worktree_path=target,
        branch_name=remote_branch,
        base_ref=f"origin/{remote_branch}",
        fetch=True,
    )

    assert result.created is True
    assert (target / ".git").exists(), "target should now be a fresh registered worktree"
    # ensure_worktree derives the reclaimed root as a sibling of worktrees/.
    assert captured["reclaimed_root"] == worktree_root.parent / "reclaimed"


async def test_ensure_worktree_raises_when_quarantine_fails(
    main_repo: Path,
    worktree_root: Path,
    remote_branch: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If quarantine fails, the dirty orphan is left in place and allocate fails
    loudly with ``orphan_dirty_uncommitted`` (the rare last-resort path)."""
    target = worktree_root / "feature-x"
    target.mkdir()
    (target / "uncommitted.txt").write_text("unsaved\n")

    async def fake_dispose(*, main_repo: Path, path: Path, reclaimed_root: Path) -> str:
        return "preserved"

    monkeypatch.setattr(alloc_mod, "_dispose_orphan", fake_dispose)

    with pytest.raises(WorktreeAllocationFailed) as excinfo:
        await ensure_worktree(
            main_repo=main_repo,
            worktree_path=target,
            branch_name=remote_branch,
            base_ref=f"origin/{remote_branch}",
            fetch=True,
        )
    assert excinfo.value.reason == "orphan_dirty_uncommitted"
    assert target.exists(), "dirty orphan must be left in place when quarantine fails"


async def test_ensure_worktree_reuses_registered_worktree(
    main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    """A second ensure_worktree against the same path is a no-op (idempotent)."""
    target = worktree_root / "feature-x"
    first = await ensure_worktree(
        main_repo=main_repo,
        worktree_path=target,
        branch_name=remote_branch,
        base_ref=f"origin/{remote_branch}",
        fetch=True,
    )
    second = await ensure_worktree(
        main_repo=main_repo,
        worktree_path=target,
        branch_name=remote_branch,
        base_ref=f"origin/{remote_branch}",
        fetch=True,
    )
    assert first.created is True
    assert second.created is False, "second allocate must reuse, not recreate"
    sibling = worktree_root.with_name(worktree_root.name + "-orphan")
    assert not sibling.exists()
