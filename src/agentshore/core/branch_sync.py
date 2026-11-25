"""Fast-forward a local branch to its remote tracking ref.

Works for checked-out and other branches. Failures are captured in
FFSyncResult; this helper never raises. Non-current branches use an atomic
git update-ref compare-and-swap guarded by an ancestor check."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from agentshore import command, subprocess_env

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from agentshore.config.models import RuntimeConfig

_logger = structlog.get_logger(__name__)


def resolve_ff_fetch_overlay(cfg: RuntimeConfig) -> Mapping[str, str] | None:
    """Git-auth overlay for the local-branch fast-forward fetch, or ``None``.

    Mirrors the worktree fetch (see ``worktree.manager._resolve_fetch_overlay``):
    the FF-sync fetch is a read-only, agent-agnostic housekeeping op (no
    authorship/push/PR), so it uses a single default git identity
    (gh-OAuth-preferred, see :func:`select_default_git_identity`). Without auth
    the fetch hits the non-interactive credential prompt and fails on private
    HTTPS remotes (``fatal: unable to get password from user``), silently
    stranding the local target ref behind the remote (#178). Any miss (no
    identities, no token) yields ``None`` — the fetch stays unauthenticated, its
    historical best-effort behavior.
    """
    from agentshore.agents.identity import (
        resolve_identity_env_by_name,
        select_default_git_identity,
    )

    name = select_default_git_identity(cfg)
    if not name:
        return None
    ident_env = resolve_identity_env_by_name(cfg, name)
    token = ident_env.get("GH_TOKEN") or ident_env.get("GITHUB_TOKEN")
    if not token:
        _logger.info("local_branch_ff_fetch_unauthenticated", identity=name)
        return None
    overlay = dict(ident_env)
    overlay.update(subprocess_env.git_auth_config_overlay(token))
    _logger.info("local_branch_ff_fetch_identity_selected", identity=name)
    return overlay


class FFSyncStatus(StrEnum):
    """Outcome of a fast-forward sync attempt."""

    SYNCED = "synced"
    ALREADY_CURRENT = "already_current"
    NO_LOCAL_BRANCH = "no_local_branch"
    DIVERGED = "diverged"
    FETCH_FAILED = "fetch_failed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class FFSyncResult:
    """Structured, non-raising result of :func:`fast_forward_local_branch`."""

    status: FFSyncStatus
    branch: str
    detail: str = ""


async def _git(
    *args: str,
    cwd: Path,
    timeout: float = 120.0,
    env_overlay: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run ``git``; return ``(rc, stdout, stderr)``. rc=-1 on timeout/spawn error.

    Never raises — this is best-effort housekeeping.
    """
    result = await command.git(*args, cwd=cwd, timeout_seconds=timeout, env_overlay=env_overlay)
    return result.returncode, result.stdout, result.stderr


async def fast_forward_local_branch(
    repo: Path,
    branch: str,
    *,
    remote: str = "origin",
    timeout: float = 120.0,
    fetch_env_overlay: Mapping[str, str] | None = None,
) -> FFSyncResult:
    """Fast-forward ``refs/heads/<branch>`` to ``<remote>/<branch>`` in ``repo``.

    Fast-forward only: if the local branch has diverged from (is not an ancestor
    of) the remote, it is left untouched and ``DIVERGED`` is returned. Works
    whether or not ``branch`` is the current checkout. Never raises — all
    failures are reported via the returned :class:`FFSyncResult`.

    ``fetch_env_overlay`` (from :func:`resolve_ff_fetch_overlay`) carries the
    git-auth header for the single remote fetch; without it the fetch is
    unauthenticated and fails on private HTTPS remotes (#178). Only the fetch
    needs it — every other step is a local ref operation.
    """
    try:
        return await _fast_forward_impl(
            repo, branch, remote=remote, timeout=timeout, fetch_env_overlay=fetch_env_overlay
        )
    except Exception as exc:  # noqa: BLE001 — best-effort housekeeping, never propagate
        _logger.warning("local_branch_ff_error", branch=branch, remote=remote, error=str(exc))
        return FFSyncResult(FFSyncStatus.ERROR, branch, str(exc))


async def _fast_forward_impl(
    repo: Path,
    branch: str,
    *,
    remote: str,
    timeout: float,
    fetch_env_overlay: Mapping[str, str] | None = None,
) -> FFSyncResult:
    rc, _, stderr = await _git(
        "fetch", remote, branch, cwd=repo, timeout=timeout, env_overlay=fetch_env_overlay
    )
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
        return FFSyncResult(FFSyncStatus.NO_LOCAL_BRANCH, branch, f"{remote_ref} missing")

    rc, local_sha, _ = await _git(
        "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=repo
    )
    local_sha = local_sha.strip()
    if rc != 0 or not local_sha:
        return FFSyncResult(FFSyncStatus.NO_LOCAL_BRANCH, branch)

    if local_sha == remote_sha:
        return FFSyncResult(FFSyncStatus.ALREADY_CURRENT, branch)

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
