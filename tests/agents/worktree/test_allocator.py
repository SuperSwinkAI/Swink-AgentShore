"""Unit tests for the allocator primitives (``ensure_worktree`` + helpers)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentshore.agents.worktree.allocator import (
    AllocateResult,
    WorktreeAllocationFailed,
    WorktreeBranchGone,
    _branch_checked_out_in_primary,
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


async def test_ensure_worktree_deletes_clean_orphan_target(
    main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    """A clean orphan dir at the target path is deleted and allocate proceeds.

    Previously raised ``WorktreeAllocationFailed(reason="target_path_dirty")``
    and permanently blocked the allocate — see #570 (example-project session
    c78d7074, 2026-05-22). The behaviour now deletes the rebuildable debris so
    forward progress is unblocked (orphans are never re-adopted).
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
    # No quarantine sibling dir is created.
    assert not worktree_root.with_name(worktree_root.name + "-orphan").exists()


async def test_ensure_worktree_raises_on_dirty_orphan_target(
    main_repo: Path,
    worktree_root: Path,
    remote_branch: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dirty orphan at the target path is preserved; allocate fails loudly.

    ``ensure_worktree`` delegates orphan disposition to ``_dispose_orphan``;
    when that returns ``"preserved"`` (the orphan has uncommitted work), the
    allocator must surface ``WorktreeAllocationFailed
    (reason="orphan_dirty_uncommitted")`` rather than destroy the work, so
    downstream play-verdict mapping categorises it and the dir is left intact.
    """
    from agentshore.agents.worktree import allocator as alloc_mod

    target = worktree_root / "dirty-target"
    target.mkdir()
    (target / "uncommitted.txt").write_text("unsaved\n")

    async def fake_dispose(*, main_repo: Path, path: Path) -> str:
        return "preserved"

    monkeypatch.setattr(alloc_mod, "_dispose_orphan", fake_dispose)

    with pytest.raises(WorktreeAllocationFailed) as exc:
        await ensure_worktree(
            main_repo=main_repo,
            worktree_path=target,
            branch_name=remote_branch,
            base_ref=f"origin/{remote_branch}",
            fetch=True,
        )
    assert exc.value.reason == "orphan_dirty_uncommitted"
    # Preserved dirty orphan stays in place.
    assert target.exists()
    assert (target / "uncommitted.txt").exists()


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


# --- Piece B: detached-HEAD fallback when the branch is checked out elsewhere -


def _head_is_detached(worktree: Path) -> bool:
    """Return True when ``worktree`` has a detached HEAD (no current branch)."""
    import subprocess

    rc = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"],
        cwd=str(worktree),
        capture_output=True,
    ).returncode
    return rc != 0


async def test_branch_checked_out_in_primary_detects_current_branch(main_repo: Path) -> None:
    # The primary working tree holds ``main``; a sibling feature branch does not.
    assert await _branch_checked_out_in_primary(main_repo, "main") is True
    assert await _branch_checked_out_in_primary(main_repo, "feature/x") is False


async def test_ensure_worktree_detaches_when_branch_held_by_primary(
    main_repo: Path, worktree_root: Path
) -> None:
    # A PR whose head branch is ``main`` (the repo default the primary tree
    # already has checked out): ``-B main`` would fail, so we must detach. Issue #60.
    target = worktree_root / "pr-from-main"
    result = await ensure_worktree(
        main_repo=main_repo,
        worktree_path=target,
        branch_name="main",
        base_ref="origin/main",
        fetch=True,
    )
    assert isinstance(result, AllocateResult)
    assert result.created is True
    assert result.detached is True
    assert target.exists()
    assert _head_is_detached(target) is True


async def test_ensure_worktree_uses_branch_when_not_checked_out_elsewhere(
    main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    # The normal case: the head branch is a feature branch nobody else holds, so
    # the worktree is created on that branch (not detached).
    target = worktree_root / "feature-x"
    result = await ensure_worktree(
        main_repo=main_repo,
        worktree_path=target,
        branch_name=remote_branch,
        base_ref=f"origin/{remote_branch}",
        fetch=True,
    )
    assert result.created is True
    assert result.detached is False
    assert _head_is_detached(target) is False


@pytest.mark.asyncio
async def test_run_git_pins_stdin_to_devnull(monkeypatch: pytest.MonkeyPatch) -> None:
    """A git child must never inherit the sidecar's stdin (the live Tauri
    JSON-RPC pipe): Git-for-Windows' MSYS2 runtime wedges at 0 CPU probing that
    contended pipe. Regression guard for the Windows worktree-reconcile hang
    (the fix is ``stdin=DEVNULL``, independent of async-vs-sync).
    """
    import asyncio

    from agentshore.agents.worktree import allocator

    captured: dict[str, object] = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:  # noqa: A002
            return (b"", b"")

        async def wait(self) -> int:
            return 0

        def kill(self) -> None:  # pragma: no cover - not reached on success
            pass

    async def fake_exec(*_args: object, **kwargs: object) -> _FakeProc:
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    rc, _out, _err = await allocator._run_git("worktree", "list", cwd=Path.cwd(), check=False)

    assert rc == 0
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("stdin") is asyncio.subprocess.DEVNULL
