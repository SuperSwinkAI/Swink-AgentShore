"""Unit tests for the allocator primitives (``ensure_worktree`` + helpers)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentshore.agents.worktree.allocator import (
    AllocateResult,
    WorktreeAllocationFailed,
    WorktreeBranchGone,
    ensure_worktree,
    remove_worktree,
    slug_for_branch,
)

# --- slug helper -------------------------------------------------------------


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        ("main", "main"),
        ("feature/x", "feature-x"),
        ("fix/issue-123_thing", "fix-issue-123_thing"),
        ("", "branch"),
        ("---", "branch"),
        ("foo!!!bar", "foo-bar"),
        ("dotted.name.is.ok", "dotted.name.is.ok"),
    ],
)
def test_slug_for_branch(branch: str, expected: str) -> None:
    assert slug_for_branch(branch) == expected


# --- ensure_worktree happy path ---------------------------------------------


async def test_ensure_worktree_creates_pr_scoped(
    main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    target = worktree_root / "feature-x"
    result = await ensure_worktree(
        main_repo=main_repo,
        worktree_path=target,
        branch_name=remote_branch,
        base_ref=f"origin/{remote_branch}",
        fetch=True,
    )
    assert isinstance(result, AllocateResult)
    assert result.created is True
    assert result.path == target
    assert result.head_sha
    assert (target / "feature.txt").exists()


async def test_ensure_worktree_idempotent(
    main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    target = worktree_root / "feature-x"
    first = await ensure_worktree(
        main_repo=main_repo,
        worktree_path=target,
        branch_name=remote_branch,
        base_ref=f"origin/{remote_branch}",
    )
    second = await ensure_worktree(
        main_repo=main_repo,
        worktree_path=target,
        branch_name=remote_branch,
        base_ref=f"origin/{remote_branch}",
    )
    assert first.created is True
    assert second.created is False
    assert second.path == target


async def test_ensure_worktree_branch_creating_detached(
    main_repo: Path, worktree_root: Path
) -> None:
    """``branch_name=None`` materialises a detached-HEAD worktree on origin/HEAD."""
    target = worktree_root / "pickup-bd-1"
    result = await ensure_worktree(
        main_repo=main_repo,
        worktree_path=target,
        branch_name=None,
        base_ref="origin/HEAD",
    )
    assert result.created is True
    assert (target / "README.md").exists()


# --- failure paths -----------------------------------------------------------


async def test_ensure_worktree_quarantines_dirty_target(
    main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    """An orphan dir at the target path is quarantined and allocate proceeds.

    Previously raised ``WorktreeAllocationFailed(reason="target_path_dirty")``
    and permanently blocked the allocate — see #570 (example-project session
    c78d7074, 2026-05-22). The new behaviour moves the orphan to
    ``agentshore-worktrees-orphan/`` so the original content is preserved
    without blocking forward progress.
    """
    target = worktree_root / "dirty-target"
    target.mkdir()
    (target / "stray.txt").write_text("not git\n")

    result = await ensure_worktree(
        main_repo=main_repo,
        worktree_path=target,
        branch_name=remote_branch,
        base_ref=f"origin/{remote_branch}",
        fetch=True,
    )
    assert result.created is True
    # The new worktree at ``target`` is a real git checkout, not the leftover dir.
    assert (target / ".git").exists()
    assert not (target / "stray.txt").exists()


async def test_ensure_worktree_quarantine_failure_raises_structured(
    main_repo: Path,
    worktree_root: Path,
    remote_branch: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the inline quarantine itself fails, surface a typed allocator error.

    ``ensure_worktree`` calls ``quarantine_orphan`` to move an unregistered
    target dir aside before ``git worktree add``. If ``shutil.move`` raises
    (cross-FS, read-only files, permission denied), the allocator must
    surface ``WorktreeAllocationFailed(reason="quarantine_orphan_failed")``
    rather than a bare ``OSError`` — downstream play-verdict mapping +
    metrics rely on the structured ``reason`` to categorise the failure.
    """
    from agentshore.agents.worktree import allocator as alloc_mod

    target = worktree_root / "dirty-target"
    target.mkdir()
    (target / "stray.txt").write_text("not git\n")

    def fake_move(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated cross-fs failure")

    monkeypatch.setattr(alloc_mod.shutil, "move", fake_move)

    with pytest.raises(WorktreeAllocationFailed) as exc:
        await ensure_worktree(
            main_repo=main_repo,
            worktree_path=target,
            branch_name=remote_branch,
            base_ref=f"origin/{remote_branch}",
            fetch=True,
        )
    assert exc.value.reason == "quarantine_orphan_failed"
    # Orphan stays in place (no cleanup obligation); next dispatch retries.
    assert target.exists()
    assert (target / "stray.txt").exists()


async def test_ensure_worktree_missing_main_repo(tmp_path: Path) -> None:
    bogus_main = tmp_path / "does-not-exist"
    with pytest.raises(WorktreeAllocationFailed) as exc:
        await ensure_worktree(
            main_repo=bogus_main,
            worktree_path=tmp_path / "wt",
            branch_name=None,
            base_ref="origin/HEAD",
            fetch=False,
        )
    assert exc.value.reason == "main_repo_missing"


async def test_ensure_worktree_branch_gone_when_remote_branch_missing(
    main_repo: Path, worktree_root: Path
) -> None:
    """``ls-remote`` reports the branch missing → ``WorktreeBranchGone``."""
    with pytest.raises(WorktreeBranchGone) as exc:
        await ensure_worktree(
            main_repo=main_repo,
            worktree_path=worktree_root / "nope",
            branch_name="never-pushed",
            base_ref="origin/never-pushed",
            fetch=True,
        )
    assert exc.value.branch == "never-pushed"


async def test_ensure_worktree_fetch_failure_proceeds_with_stale_local(
    main_repo: Path,
    worktree_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``git fetch`` fails, the allocator returns ``fetched=False``.

    Branch-creating allocations don't depend on remote refs (they're
    detached on ``origin/HEAD`` from the local clone) so a network outage
    is tolerable.
    """
    from agentshore.agents.worktree import allocator as alloc_mod

    real_run_git = alloc_mod._run_git

    async def fake_run_git(*args: str, **kwargs: object) -> tuple[int, str, str]:
        if args and args[0] == "fetch":
            return 128, "", "simulated network failure"
        return await real_run_git(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(alloc_mod, "_run_git", fake_run_git)
    target = worktree_root / "pickup-stale-fetch"
    result = await ensure_worktree(
        main_repo=main_repo,
        worktree_path=target,
        branch_name=None,
        base_ref="origin/HEAD",
        fetch=True,
    )
    assert result.fetched is False
    assert result.created is True


# --- remove_worktree ---------------------------------------------------------


async def test_remove_worktree_cleans_disk(
    main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    target = worktree_root / "feature-x"
    await ensure_worktree(
        main_repo=main_repo,
        worktree_path=target,
        branch_name=remote_branch,
        base_ref=f"origin/{remote_branch}",
    )
    ok = await remove_worktree(main_repo=main_repo, worktree_path=target)
    assert ok is True
    assert not target.exists()


async def test_remove_worktree_force_when_directory_missing(
    main_repo: Path, worktree_root: Path
) -> None:
    """Removing an already-gone worktree is a no-op success."""
    target = worktree_root / "phantom"
    ok = await remove_worktree(main_repo=main_repo, worktree_path=target)
    assert ok is True


# ---------------------------------------------------------------------------
# pickup-* collision retry (Phase 3)
# ---------------------------------------------------------------------------


async def test_allocate_retries_on_pickup_branch_collision(
    main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    """When a pickup-* worktree holds the target branch, allocate retries after force-remove.

    Reproduces example-project session c734b96f: pickup-535 orphan held
    branch ``agentshore/535-...`` and blocked subsequent code_review allocations.
    """
    # Create the orphaned pickup-* worktree holding the branch.
    pickup_path = worktree_root / "pickup-99"
    await ensure_worktree(
        main_repo=main_repo,
        worktree_path=pickup_path,
        branch_name=remote_branch,
        base_ref=f"origin/{remote_branch}",
    )
    assert pickup_path.exists()

    # Now try to allocate the same branch at a different path — should retry.
    new_path = worktree_root / "agentshore-x"
    result = await ensure_worktree(
        main_repo=main_repo,
        worktree_path=new_path,
        branch_name=remote_branch,
        base_ref=f"origin/{remote_branch}",
    )
    assert result.created is True
    assert new_path.exists()
    # The orphan pickup-* was force-removed during retry.
    assert not pickup_path.exists()


async def test_allocate_does_not_retry_on_non_pickup_collision(
    main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    """Branch held by a non-pickup-* worktree bubbles the original error.

    The pickup-* heuristic is the safe-to-force-remove signal; arbitrary
    paths holding a branch are operator-owned and must not be touched.
    """
    from agentshore.agents.worktree.allocator import WorktreeAllocationFailed

    held_path = worktree_root / "operator-owned"
    await ensure_worktree(
        main_repo=main_repo,
        worktree_path=held_path,
        branch_name=remote_branch,
        base_ref=f"origin/{remote_branch}",
    )
    assert held_path.exists()

    new_path = worktree_root / "new-attempt"
    with pytest.raises(WorktreeAllocationFailed):
        await ensure_worktree(
            main_repo=main_repo,
            worktree_path=new_path,
            branch_name=remote_branch,
            base_ref=f"origin/{remote_branch}",
        )
    # The operator-owned worktree is untouched.
    assert held_path.exists()
