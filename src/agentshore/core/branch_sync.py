"""Deterministic fast-forward sync of a local branch to its remote tracking ref.

After a PR merges (on the remote), the *local* target branch in the primary
checkout does not advance on its own — only the remote moves. Nothing in the
per-worktree path needs the local branch (worktrees base off ``origin/<ref>``),
but the local target branch otherwise drifts arbitrarily far behind the remote,
which is a latent footgun for any code, dashboard, or human that reads it.

This module advances a local branch to match ``<remote>/<branch>`` using a
**fast-forward-only** update that works whether or not the branch is currently
checked out. It is purely deterministic housekeeping — a backstop, never a
driver — and it never raises: every failure is captured in the returned
``FFSyncResult`` so a caller inside a play's ``execute()`` can fire-and-forget.

Correctness note: the primary checkout is frequently on a *different* branch
than the target (e.g. ``main`` checked out while the session targets
``integration``). A naive ``git merge --ff-only origin/<target>`` in that
checkout would fast-forward the *wrong* branch. So when the target is not the
current checkout we advance the ref directly via ``git update-ref`` (an atomic
compare-and-swap, guarded by an explicit ancestor check) instead of merging.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from agentshore import command

if TYPE_CHECKING:
    from pathlib import Path

_logger = structlog.get_logger(__name__)


class FFSyncStatus(StrEnum):
    """Outcome of a fast-forward sync attempt."""

    SYNCED = "synced"  # local branch advanced to <remote>/<branch>
    ALREADY_CURRENT = "already_current"  # local already at <remote>/<branch>
    NO_LOCAL_BRANCH = "no_local_branch"  # no local ref to advance toward
    DIVERGED = "diverged"  # local not an ancestor of remote — left untouched
    FETCH_FAILED = "fetch_failed"  # could not update the remote-tracking ref
    ERROR = "error"  # unexpected failure (captured, never raised)


@dataclass(frozen=True, slots=True)
class FFSyncResult:
    """Structured, non-raising result of :func:`fast_forward_local_branch`."""

    status: FFSyncStatus
    branch: str
    detail: str = ""


async def _git(*args: str, cwd: Path, timeout: float = 120.0) -> tuple[int, str, str]:
    """Run ``git``; return ``(rc, stdout, stderr)``. rc=-1 on timeout/spawn error.

    Never raises — this is best-effort housekeeping. Runs the *synchronous* git
    off the event loop via ``asyncio.to_thread``: on Windows, spawning git with
    the async ``create_subprocess_exec`` from inside the loaded desktop sidecar
    wedges the child at 0 CPU. ``run_sync_command`` (not ``git_sync``) is used
    deliberately so the inherited sidecar env is preserved — these are network
    ``fetch``/``ls-remote`` operations that must keep the credential helper, and
    the sidecar already sets ``GIT_TERMINAL_PROMPT=0`` so a missing credential
    fails fast instead of prompting. It does add ``CREATE_NO_WINDOW`` and resolves
    the real git path.
    """
    try:
        result = await asyncio.to_thread(
            command.run_sync_command, "git", *args, cwd=cwd, timeout_seconds=timeout
        )
    except command.CommandTimeoutError:
        return -1, "", f"git {' '.join(args)} timed out after {timeout:.0f}s"
    except FileNotFoundError as exc:
        return -1, "", str(exc)
    return result.returncode, result.stdout, result.stderr


async def fast_forward_local_branch(
    repo: Path,
    branch: str,
    *,
    remote: str = "origin",
    timeout: float = 120.0,
) -> FFSyncResult:
    """Fast-forward ``refs/heads/<branch>`` to ``<remote>/<branch>`` in ``repo``.

    Fast-forward only: if the local branch has diverged from (is not an ancestor
    of) the remote, it is left untouched and ``DIVERGED`` is returned. Works
    whether or not ``branch`` is the current checkout. Never raises — all
    failures are reported via the returned :class:`FFSyncResult`.
    """
    try:
        return await _fast_forward_impl(repo, branch, remote=remote, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — best-effort housekeeping, never propagate
        _logger.warning("local_branch_ff_error", branch=branch, remote=remote, error=str(exc))
        return FFSyncResult(FFSyncStatus.ERROR, branch, str(exc))


async def _fast_forward_impl(
    repo: Path, branch: str, *, remote: str, timeout: float
) -> FFSyncResult:
    # 1. Update the remote-tracking ref (refs/remotes/<remote>/<branch>). This
    #    form never touches the local branch, so it is safe even when <branch>
    #    is the current checkout.
    rc, _, stderr = await _git("fetch", remote, branch, cwd=repo, timeout=timeout)
    if rc != 0:
        _logger.warning(
            "local_branch_ff_fetch_failed",
            branch=branch,
            remote=remote,
            stderr=stderr.strip()[:200],
        )
        return FFSyncResult(FFSyncStatus.FETCH_FAILED, branch, stderr.strip()[:200])

    remote_ref = f"{remote}/{branch}"
    rc, remote_sha, _ = await _git("rev-parse", "--verify", "--quiet", remote_ref, cwd=repo)
    remote_sha = remote_sha.strip()
    if rc != 0 or not remote_sha:
        # Remote branch vanished/renamed — nothing to advance toward.
        return FFSyncResult(FFSyncStatus.NO_LOCAL_BRANCH, branch, f"{remote_ref} missing")

    # 2. Resolve the local branch ref (may legitimately not exist).
    rc, local_sha, _ = await _git(
        "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=repo
    )
    local_sha = local_sha.strip()
    if rc != 0 or not local_sha:
        return FFSyncResult(FFSyncStatus.NO_LOCAL_BRANCH, branch)

    if local_sha == remote_sha:
        return FFSyncResult(FFSyncStatus.ALREADY_CURRENT, branch)

    # 3. Fast-forward safety: local must be an ancestor of the remote tip.
    rc, _, _ = await _git("merge-base", "--is-ancestor", local_sha, remote_sha, cwd=repo)
    if rc != 0:
        _logger.warning(
            "local_branch_ff_diverged",
            branch=branch,
            remote=remote,
            local_sha=local_sha[:12],
            remote_sha=remote_sha[:12],
        )
        return FFSyncResult(FFSyncStatus.DIVERGED, branch)

    # 4. Advance — pick the primitive by checkout state. ``merge --ff-only``
    #    updates the working tree when <branch> is checked out; ``update-ref``
    #    (atomic compare-and-swap, ff already verified) advances a branch that
    #    is *not* the current checkout — the case a plain merge gets wrong.
    rc, current, _ = await _git("branch", "--show-current", cwd=repo)
    current = current.strip()
    if current == branch:
        rc, _, stderr = await _git("merge", "--ff-only", remote_ref, cwd=repo, timeout=timeout)
    else:
        rc, _, stderr = await _git(
            "update-ref", f"refs/heads/{branch}", remote_sha, local_sha, cwd=repo
        )
    if rc != 0:
        _logger.warning(
            "local_branch_ff_advance_failed",
            branch=branch,
            remote=remote,
            stderr=stderr.strip()[:200],
        )
        return FFSyncResult(FFSyncStatus.ERROR, branch, stderr.strip()[:200])

    _logger.info(
        "local_branch_ff_synced",
        branch=branch,
        remote=remote,
        from_sha=local_sha[:12],
        to_sha=remote_sha[:12],
    )
    return FFSyncResult(FFSyncStatus.SYNCED, branch)
