"""Detect the branch a branch-creating play landed on and re-key its row.

Branch-creating plays (Issue Pickup, Cleanup) allocate a fresh worktree
keyed by ``pre_branch_key`` (e.g. ``"pickup-bd-123"``). When the play
succeeds the agent has switched to a real branch — we move the row from
``pre_branch_key`` to ``branch_name`` and rename the directory so future
plays touching the same branch share the worktree.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from agentshore.agents.worktree.allocator import (
    WorktreeAllocationFailed,
    _run_git,
    remove_worktree,
    worktree_target_path,
)
from agentshore.agents.worktree.registry import (
    WorktreeAllocationConflict,
    WorktreeRow,
    mark_status,
    rekey_row,
)

if TYPE_CHECKING:
    from agentshore.data.store import DataStore

log = structlog.get_logger(__name__)


async def detect_branch_in_worktree(worktree_path: Path) -> str | None:
    """Return the current branch checked out in ``worktree_path``.

    Returns ``None`` if the worktree is in detached-HEAD mode (no branch
    was created), if the path is gone, or if ``git`` errors out.
    """
    if not worktree_path.exists():
        return None
    # stdin=DEVNULL: a git child must never inherit the sidecar's stdin (the live
    # Tauri JSON-RPC pipe) -- Git-for-Windows' MSYS2 runtime wedges at 0 CPU
    # probing that contended pipe.
    proc = await asyncio.create_subprocess_exec(
        "git",
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        cwd=str(worktree_path),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    branch = stdout_b.decode("utf-8", errors="replace").strip()
    return branch or None


async def rekey_worktree(
    store: DataStore,
    *,
    row: WorktreeRow,
    branch_name: str,
    worktree_root: Path,
) -> WorktreeRow:
    """Promote ``row`` from a pre-branch key to a real branch row.

    Order of operations is deliberate:

    1. **Rename the directory** to ``<worktree_root>/<slug>``. The on-disk
       move is the truly irreversible step; doing it first means a DB
       failure later leaves a row pointing at the new path on disk, which
       is correctable (rekey can be retried).
    2. **Update the row** to ``branch_name`` + the new path + clear
       ``pre_branch_key``.

    If the rename fails we leave the row unchanged. If the DB update fails
    after a successful rename, we mark the row ``stale`` (its path on disk
    is wrong) so the next session-start sweep removes it; the caller gets
    the original exception bubbling up.
    """
    current_path = Path(row.worktree_path)
    target_path = worktree_target_path(worktree_root, branch_name)

    if target_path == current_path:
        return await rekey_row(store, worktree_id=row.worktree_id, branch_name=branch_name)

    if target_path.exists():
        log.warning(
            "worktree_rekey_target_exists",
            worktree_id=row.worktree_id,
            target=str(target_path),
            current=str(current_path),
        )
        try:
            return await rekey_row(
                store,
                worktree_id=row.worktree_id,
                branch_name=branch_name,
            )
        except WorktreeAllocationConflict:
            await mark_status(
                store,
                worktree_id=row.worktree_id,
                status="stale",
                failure_reason="rekey_target_exists",
            )
            raise

    target_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(current_path), str(target_path))
    except OSError as exc:
        log.warning(
            "worktree_rekey_rename_failed",
            worktree_id=row.worktree_id,
            error=str(exc),
        )
        await mark_status(
            store,
            worktree_id=row.worktree_id,
            status="stale",
            failure_reason=f"rekey_rename_failed: {exc}",
        )
        # The old ``pickup-NNN`` directory still exists on disk and still
        # holds the agent-created branch checked out. Release the branch
        # lock immediately so the next allocation on that branch (e.g. a
        # code_review for the just-opened PR) doesn't hit a collision while
        # waiting for the next session-start sweep. Best-effort.
        await _release_orphaned_worktree(
            main_repo=(
                worktree_root.parent if worktree_root.parent.exists() else current_path.parent
            ),
            path=current_path,
            reason="rekey_rename_failed",
        )
        raise

    try:
        return await rekey_row(
            store,
            worktree_id=row.worktree_id,
            branch_name=branch_name,
            new_path=str(target_path),
        )
    except Exception as exc:
        log.warning(
            "worktree_rekey_db_update_failed",
            worktree_id=row.worktree_id,
            error=str(exc),
        )
        # The directory move already succeeded — pass the new on-disk path
        # to mark_status so the row points at it. The next reap pass can
        # then remove the actual worktree instead of looking at the stale
        # pre-rekey path that no longer exists. The row stays at status='stale'
        # with the new path so list_orphans picks it up at next session start.
        # Also repair git's internal metadata so any future ``worktree list``
        # against the moved path resolves correctly.
        await mark_status(
            store,
            worktree_id=row.worktree_id,
            status="stale",
            failure_reason=f"rekey_db_update_failed: {exc}",
            worktree_path=str(target_path),
        )
        await _repair_git_worktree_metadata(
            main_repo=worktree_root.parent if worktree_root.parent.exists() else target_path.parent,
            reason="rekey_db_update_failed",
        )
        raise


async def _release_orphaned_worktree(*, main_repo: Path, path: Path, reason: str) -> None:
    """Force-remove an on-disk worktree to release its branch lock.

    Best-effort. Failures are logged but never re-raised — the caller is
    already handling a rekey failure and the on-disk cleanup is defensive
    insurance against mid-session branch-collision wedges.
    """
    try:
        ok = await remove_worktree(main_repo=main_repo, worktree_path=path, force=True)
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning(
            "worktree_rekey_failed_on_disk_reap_raised",
            path=str(path),
            reason=reason,
            error=str(exc),
        )
        return
    log.warning(
        "worktree_rekey_failed_on_disk_reaped",
        path=str(path),
        reason=reason,
        ok=ok,
    )


async def _repair_git_worktree_metadata(*, main_repo: Path, reason: str) -> None:
    """Run ``git worktree repair`` so git resolves moved paths correctly.

    Used after the directory move during rekey succeeds but the DB update
    fails — git's metadata at ``.git/worktrees/<old-key>/`` still points
    at the pre-move path until repair updates it. Best-effort.
    """
    try:
        await _run_git("worktree", "repair", cwd=main_repo, check=False)
    except WorktreeAllocationFailed as exc:
        log.warning(
            "worktree_rekey_git_repair_failed",
            reason=reason,
            error=str(exc),
        )
        return
    log.warning("worktree_rekey_git_repair_ran", reason=reason)


__all__ = ["detect_branch_in_worktree", "rekey_worktree"]
