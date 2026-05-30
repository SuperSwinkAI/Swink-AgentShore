"""Tests for worktree reconciliation + orphan quarantine.

Closes #570 — sessions used to permanently block any PR-scoped allocation
at a path where a prior session left an unregistered directory. The
``ensure_worktree`` raise on ``target_path_dirty`` would fire indefinitely;
93 consecutive code_review failures observed in example-project session
c78d7074 (2026-05-22). The new behaviour quarantines the orphan and
proceeds, so allocation converges to a clean registered worktree from any
starting state.

These tests exercise the **real** ``git`` binary against the per-test
``main_repo`` + ``worktree_root`` fixtures from ``conftest.py``. No mocks
of git or filesystem — schema/git bugs that mocks would hide are surfaced.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentshore.agents.worktree.allocator import (
    ReconcileReport,
    _quarantine_root,
    ensure_worktree,
    quarantine_orphan,
    reconcile_worktrees,
)

pytestmark = pytest.mark.asyncio


# --- quarantine_orphan --------------------------------------------------------


async def test_quarantine_orphan_moves_dir_into_sibling_root(
    worktree_root: Path,
) -> None:
    """An orphan dir under worktree_root is moved into the quarantine sibling."""
    orphan = worktree_root / "agentshore-194-schwab-market-data-expansion"
    orphan.mkdir()
    (orphan / "stale.txt").write_text("leftover from prior session\n")

    destination = await quarantine_orphan(orphan_path=orphan, worktree_root=worktree_root)

    assert not orphan.exists(), "source dir should be gone after the move"
    assert destination.exists(), "quarantine destination should exist"
    assert (destination / "stale.txt").read_text() == "leftover from prior session\n"
    # Quarantine layout: <worktree_root_parent>/agentshore-worktrees-orphan/<repo>/<name>-<ts>
    quarantine_root = _quarantine_root(worktree_root)
    assert destination.parent == quarantine_root
    assert destination.name.startswith("agentshore-194-schwab-market-data-expansion-")


async def test_quarantine_orphan_propagates_move_failure_with_structured_log(
    worktree_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``shutil.move`` failure raises after emitting a structured warning.

    Real-world failures: read-only files inside the orphan, cross-filesystem
    permission issues, or a destination collision the inner existence check
    didn't catch (parallel quarantines). Before the cleanup, the bare
    ``shutil.move`` could fail silently (or with an unhelpful traceback);
    now the warning carries ``orphan_path``/``destination``/``error`` and
    the caller chooses whether to abort.
    """
    from agentshore.agents.worktree import allocator as alloc_mod

    orphan = worktree_root / "doomed-orphan"
    orphan.mkdir()
    (orphan / "x.txt").write_text("content")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("simulated read-only filesystem")

    monkeypatch.setattr(alloc_mod.shutil, "move", boom)

    with pytest.raises(PermissionError):
        await quarantine_orphan(orphan_path=orphan, worktree_root=worktree_root)

    # The source orphan should still be on disk — failure left it where it was.
    assert orphan.exists()


async def test_quarantine_orphan_handles_name_collision(
    worktree_root: Path,
) -> None:
    """Two quarantines of the same name within one second still both land."""
    # Pre-create a quarantine entry with the same timestamp suffix to force collision.
    quarantine_root = _quarantine_root(worktree_root)
    quarantine_root.mkdir(parents=True, exist_ok=True)
    # First quarantine.
    orphan_a = worktree_root / "branch-a"
    orphan_a.mkdir()
    (orphan_a / "x").write_text("a\n")
    dest_a = await quarantine_orphan(orphan_path=orphan_a, worktree_root=worktree_root)

    # Second quarantine of an identically-named orphan (same branch, fresh session).
    orphan_a_again = worktree_root / "branch-a"
    orphan_a_again.mkdir()
    (orphan_a_again / "x").write_text("b\n")
    dest_b = await quarantine_orphan(
        orphan_path=orphan_a_again, worktree_root=worktree_root
    )

    assert dest_a != dest_b, "both quarantines should land in distinct dirs"
    assert dest_a.exists() and dest_b.exists()
    assert (dest_a / "x").read_text() == "a\n"
    assert (dest_b / "x").read_text() == "b\n"


# --- reconcile_worktrees ------------------------------------------------------


async def test_reconcile_quarantines_orphan_leaves_registered_alone(
    main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    """Registered worktrees stay; unregistered dirs get quarantined."""
    # One legitimately-registered worktree.
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
    assert len(report.quarantined) == 1
    assert report.quarantined[0].name.startswith("orphan-from-prior-session-")
    assert not orphan.exists(), "orphan should have been moved"
    assert registered.exists(), "registered worktree must not be touched"


async def test_reconcile_is_idempotent(
    main_repo: Path, worktree_root: Path
) -> None:
    """Running reconcile twice in a row: second run has nothing to do."""
    orphan = worktree_root / "lonely-orphan"
    orphan.mkdir()
    (orphan / "file").write_text("x")

    first = await reconcile_worktrees(main_repo=main_repo, worktree_root=worktree_root)
    second = await reconcile_worktrees(main_repo=main_repo, worktree_root=worktree_root)

    assert len(first.quarantined) == 1
    assert second.quarantined == []


async def test_reconcile_no_worktree_root_returns_empty(
    main_repo: Path, tmp_path: Path
) -> None:
    """Brand-new machine with no worktree_root yet → empty report, no crash."""
    nonexistent = tmp_path / "never-created" / "agentshore-worktrees" / "repo"
    report = await reconcile_worktrees(main_repo=main_repo, worktree_root=nonexistent)
    assert report.quarantined == []


async def test_reconcile_ignores_files_in_worktree_root(
    main_repo: Path, worktree_root: Path
) -> None:
    """A loose file at the root level isn't a directory; reconcile leaves it."""
    stray = worktree_root / "stray.txt"
    stray.write_text("not a worktree\n")
    report = await reconcile_worktrees(main_repo=main_repo, worktree_root=worktree_root)
    assert report.quarantined == []
    assert stray.exists()


# --- heal-on-allocate (ensure_worktree converges from orphan state) ----------


async def test_ensure_worktree_quarantines_orphan_at_target_path(
    main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    """An orphan dir at the target path is quarantined; allocate proceeds.

    Before the fix, ``ensure_worktree`` raised ``WorktreeAllocationFailed``
    with reason ``target_path_dirty`` — permanently blocking every retry.
    Now it moves the orphan and creates the registered worktree.
    """
    target = worktree_root / "feature-x"
    target.mkdir()
    (target / "old_file.txt").write_text("from a prior session\n")
    assert target.exists()

    result = await ensure_worktree(
        main_repo=main_repo,
        worktree_path=target,
        branch_name=remote_branch,
        base_ref=f"origin/{remote_branch}",
        fetch=True,
    )

    assert result.created is True
    assert target.exists()
    assert (target / ".git").exists(), "target should now be a git worktree"
    # The old content was quarantined, not present in the fresh worktree.
    assert not (target / "old_file.txt").exists()
    # And it lives under the quarantine sibling now.
    quarantine_root = _quarantine_root(worktree_root)
    quarantined = list(quarantine_root.iterdir())
    assert len(quarantined) == 1
    assert (quarantined[0] / "old_file.txt").read_text() == "from a prior session\n"


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
    # No quarantine should have happened; the dir was always a valid worktree.
    quarantine_root = _quarantine_root(worktree_root)
    assert not quarantine_root.exists() or list(quarantine_root.iterdir()) == []
