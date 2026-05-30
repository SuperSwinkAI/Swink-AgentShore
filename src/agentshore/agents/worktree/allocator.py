"""Git primitives behind ``WorktreeManager``.

Pure async wrappers around ``git`` subprocesses; never blocks the event loop.
Errors come back as typed exceptions the manager / dispatcher can map onto
play verdicts.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog

from agentshore.errors import OrchestratorError

log = structlog.get_logger(__name__)


class WorktreeAllocationFailed(OrchestratorError):
    """``git worktree add`` (or a precursor command) failed unrecoverably."""

    error_type = "worktree_allocation_failed"
    recoverable = True
    recovery_action = "drop play, surface worktree_create_failed verdict"

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class WorktreeBranchGone(OrchestratorError):
    """Remote branch has been deleted upstream."""

    error_type = "worktree_branch_gone"
    recoverable = True
    recovery_action = "mark worktree stale, drop PR-scoped play"

    def __init__(self, message: str, *, branch: str) -> None:
        super().__init__(message)
        self.branch = branch


@dataclass(frozen=True, slots=True)
class AllocateResult:
    """Outcome of ``ensure_worktree``."""

    path: Path
    created: bool
    fetched: bool
    head_sha: str


_SLUG_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slug_for_branch(branch: str) -> str:
    """Convert a branch name into a filesystem-safe directory slug.

    Idempotent; collapses runs of unsafe characters into a single ``-`` and
    trims leading/trailing dashes. Empty inputs return ``"branch"`` to
    guarantee a non-empty path component.
    """
    slug = _SLUG_SAFE.sub("-", branch).strip("-")
    return slug or "branch"


def worktree_target_path(worktree_root: Path, key: str) -> Path:
    """Assemble the on-disk path for a worktree keyed by ``key``.

    Single canonical place to derive ``<worktree_root>/<slug>`` so the PR
    branch path and the prebranch-key path can't diverge in formatting. The
    manager calls this from both ``_allocate_pr_scoped`` (key=branch) and
    ``_allocate_branch_creating`` (key=pre_branch_key).
    """
    return worktree_root / slug_for_branch(key)


async def _run_git(
    *args: str,
    cwd: Path,
    check: bool = True,
    timeout: float = 60.0,
) -> tuple[int, str, str]:
    """Run a ``git`` subprocess asynchronously and capture stdout/stderr.

    Returns ``(returncode, stdout, stderr)``. If ``check`` is true and the
    return code is non-zero, raises ``WorktreeAllocationFailed`` with the
    failing command embedded in the reason.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise WorktreeAllocationFailed(
            f"git {' '.join(args)} timed out after {timeout:.0f}s",
            reason="git_timeout",
        ) from exc
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    rc = proc.returncode if proc.returncode is not None else -1
    if check and rc != 0:
        raise WorktreeAllocationFailed(
            f"git {' '.join(args)} failed (rc={rc}): {stderr.strip()}",
            reason=f"git_{args[0]}_failed",
        )
    return rc, stdout, stderr


_PICKUP_COLLISION_RE = re.compile(
    r"already used by worktree at\s+['\"]?([^'\"\n]+)['\"]?", re.IGNORECASE
)


async def _add_worktree_with_collision_retry(
    args: list[str],
    *,
    main_repo: Path,
    branch_name: str | None,
) -> None:
    """Run ``git worktree add ...`` with one-shot retry on pickup-* collisions.

    The original failure mode: a prior
    ``pickup-NNN`` worktree from a crashed session holds the target branch,
    so ``git worktree add -B <branch> <new_path>`` errors with::

        fatal: 'agentshore/535-...' is already used by worktree at 'pickup-535'

    When that pattern is detected AND the colliding path is a ``pickup-*``
    directory (i.e. an orphaned branch-creating allocation that never
    rekeyed), force-remove it and retry the original add exactly once.
    Anything else bubbles through the existing failure path.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(main_repo),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=180.0)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise WorktreeAllocationFailed(
            f"git {' '.join(args)} timed out after 180s",
            reason="git_timeout",
        ) from exc
    rc = proc.returncode if proc.returncode is not None else -1
    if rc == 0:
        return
    stderr = stderr_b.decode("utf-8", errors="replace")

    collision = _PICKUP_COLLISION_RE.search(stderr)
    is_pickup = collision is not None and Path(collision.group(1)).name.startswith(
        "pickup-"
    )
    if not is_pickup or branch_name is None:
        # Not a pickup-* collision (or detached-HEAD path) — bubble the
        # original error via the standard reason classifier.
        raise WorktreeAllocationFailed(
            f"git {' '.join(args)} failed (rc={rc}): {stderr.strip()}",
            reason=f"git_{args[0]}_failed",
        )

    assert collision is not None  # guarded by `is_pickup` check above
    colliding_path = Path(collision.group(1))
    log.warning(
        "worktree_allocate_collision_detected",
        branch=branch_name,
        colliding_path=str(colliding_path),
        attempted_args=args,
    )
    removed_ok = await remove_worktree(
        main_repo=main_repo, worktree_path=colliding_path, force=True
    )
    if not removed_ok:
        raise WorktreeAllocationFailed(
            f"failed to remove colliding pickup worktree at {colliding_path}",
            reason="pickup_collision_remove_failed",
        )

    # Retry once. Any second failure bubbles via the normal path with the
    # standard reason classifier so it surfaces in the orchestrator logs the
    # way other allocation failures do.
    rc2, _, stderr2 = await _run_git(
        *args, cwd=main_repo, check=False, timeout=180.0
    )
    if rc2 != 0:
        raise WorktreeAllocationFailed(
            f"git {' '.join(args)} failed after pickup-collision retry (rc={rc2}): "
            f"{stderr2.strip()}",
            reason="git_worktree_failed_after_retry",
        )
    log.warning(
        "worktree_allocate_collision_resolved",
        branch=branch_name,
        colliding_path=str(colliding_path),
    )


async def _fetch(main_repo: Path, *, remote: str = "origin") -> bool:
    """Best-effort ``git fetch`` against ``remote``.

    Returns ``True`` on success, ``False`` on any failure (network, auth,
    etc.). Failures are logged but do not raise — callers proceed with
    whatever local refs they have, marking the result as ``fetched=False``.
    """
    try:
        rc, _, stderr = await _run_git(
            "fetch", "--prune", remote, cwd=main_repo, check=False, timeout=120.0
        )
    except WorktreeAllocationFailed as exc:
        log.warning("worktree_fetch_failed", remote=remote, reason=exc.reason)
        return False
    if rc != 0:
        log.warning("worktree_fetch_nonzero", remote=remote, stderr=stderr.strip())
        return False
    return True


async def _remote_branch_exists(main_repo: Path, branch: str, *, remote: str = "origin") -> bool:
    """Return True when ``remote/branch`` resolves via ``git ls-remote``."""
    try:
        rc, stdout, _ = await _run_git(
            "ls-remote",
            "--heads",
            remote,
            branch,
            cwd=main_repo,
            check=False,
            timeout=30.0,
        )
    except WorktreeAllocationFailed:
        return False
    return rc == 0 and bool(stdout.strip())


async def _head_sha(path: Path) -> str:
    """Resolve HEAD SHA of a working tree (returns empty string on failure)."""
    try:
        _, stdout, _ = await _run_git("rev-parse", "HEAD", cwd=path, check=False)
    except WorktreeAllocationFailed:
        return ""
    return stdout.strip()


async def _existing_worktree_for_path(main_repo: Path, target: Path) -> bool:
    """Check whether ``target`` already appears in ``git worktree list``."""
    try:
        _, stdout, _ = await _run_git("worktree", "list", "--porcelain", cwd=main_repo)
    except WorktreeAllocationFailed:
        return False
    target_resolved = str(target.resolve())
    for line in stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        listed = line[len("worktree ") :].strip()
        try:
            if str(Path(listed).resolve()) == target_resolved:
                return True
        except OSError:
            continue
    return False


async def _list_worktrees_porcelain(main_repo: Path) -> list[str]:
    """Return resolved paths from ``git worktree list --porcelain``.

    Used by the manager's post-add verification to confirm a worktree we
    just created is actually tracked in git's registry. Returns an empty
    list when the ``git`` invocation fails — callers treat that as
    "registration not visible" and clean up accordingly.
    """
    try:
        _, stdout, _ = await _run_git("worktree", "list", "--porcelain", cwd=main_repo)
    except WorktreeAllocationFailed:
        return []
    paths: list[str] = []
    for line in stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        listed = line[len("worktree ") :].strip()
        try:
            paths.append(str(Path(listed).resolve()))
        except OSError:
            continue
    return paths


async def ensure_worktree(
    *,
    main_repo: Path,
    worktree_path: Path,
    branch_name: str | None,
    base_ref: str,
    fetch: bool = True,
    remote: str = "origin",
) -> AllocateResult:
    """Idempotently materialise a worktree at ``worktree_path``.

    Routing rules:

    - When the directory already exists and ``git`` lists it as a worktree,
      fast-forward to the remote tip (best-effort) and return ``created=False``.
    - When ``branch_name`` is provided, the worktree is checked out on that
      branch; if the local branch is missing, we create it tracking
      ``<remote>/<branch_name>``.
    - When ``branch_name`` is None (branch-creating allocate), the worktree
      is created in detached-HEAD mode pointing at ``base_ref``. The caller
      is expected to ``git switch -c`` from inside the worktree.

    ``base_ref`` is the resolved git ref to base off of (typically
    ``"origin/main"`` or ``"origin/<pr-branch>"``).

    Raises:
        WorktreeAllocationFailed: ``git worktree add`` or another git
            command returned non-zero.
        WorktreeBranchGone: A specific ``branch_name`` was requested but
            ``ls-remote`` reports it missing upstream.
    """
    if not main_repo.exists():
        raise WorktreeAllocationFailed(
            f"main repo path does not exist: {main_repo}", reason="main_repo_missing"
        )

    fetched = await _fetch(main_repo, remote=remote) if fetch else False

    if (
        branch_name is not None
        and fetched
        and not await _remote_branch_exists(main_repo, branch_name, remote=remote)
    ):
        raise WorktreeBranchGone(
            f"remote branch {remote}/{branch_name} is gone",
            branch=branch_name,
        )

    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    already_registered = await _existing_worktree_for_path(main_repo, worktree_path)
    if already_registered and worktree_path.exists():
        if branch_name is not None and fetched:
            await _run_git(
                "fetch",
                remote,
                branch_name,
                cwd=worktree_path,
                check=False,
                timeout=120.0,
            )
            await _run_git(
                "merge",
                "--ff-only",
                f"{remote}/{branch_name}",
                cwd=worktree_path,
                check=False,
            )
        head = await _head_sha(worktree_path)
        return AllocateResult(path=worktree_path, created=False, fetched=fetched, head_sha=head)

    if already_registered and not worktree_path.exists():
        # Stale registration: git thinks the worktree exists but the directory
        # is gone (killed mid-session, manual cleanup, etc.). Prune the stale
        # entry so the subsequent ``git worktree add`` succeeds. Without this,
        # the allocator retries the same path every tick and never recovers.
        log.warning(
            "worktree_stale_registration_pruned",
            worktree_path=str(worktree_path),
        )
        await _run_git("worktree", "prune", cwd=main_repo, check=False)

    if worktree_path.exists():
        # Orphan directory: path exists but git doesn't know it as a worktree.
        # Common causes: a prior session was force-killed mid-allocate, or
        # the skill exited without ``git worktree remove`` cleanup. Raising
        # here used to permanently block any future play that needed the same
        # branch — 93 consecutive code_review dispatches can fail against orphan dirs.
        # Quarantine the orphan so the allocate can proceed; the moved
        # content is preserved under ``agentshore-worktrees-orphan/`` for the
        # user to inspect (no automatic cleanup).
        try:
            destination = await quarantine_orphan(
                orphan_path=worktree_path, worktree_root=worktree_path.parent
            )
        except (OSError, shutil.Error) as exc:
            # ``quarantine_orphan`` already logged structured context; surface
            # a typed allocator failure so downstream play-verdict mapping
            # and metrics categorise this as a quarantine issue rather than
            # a generic OSError. Next dispatch retries the same path.
            raise WorktreeAllocationFailed(
                f"quarantine of orphan {worktree_path} failed: {exc}",
                reason="quarantine_orphan_failed",
            ) from exc
        log.warning(
            "worktree_orphan_quarantined_on_allocate",
            orphan_path=str(worktree_path),
            quarantine_path=str(destination),
        )

    args: list[str] = ["worktree", "add"]
    if branch_name is not None:
        args.extend(["-B", branch_name, str(worktree_path), base_ref])
    else:
        args.extend(["--detach", str(worktree_path), base_ref])

    try:
        await _add_worktree_with_collision_retry(
            args, main_repo=main_repo, branch_name=branch_name
        )
    except WorktreeAllocationFailed:
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)
        raise

    if branch_name is not None and fetched:
        await _run_git(
            "merge",
            "--ff-only",
            f"{remote}/{branch_name}",
            cwd=worktree_path,
            check=False,
        )

    head = await _head_sha(worktree_path)
    return AllocateResult(path=worktree_path, created=True, fetched=fetched, head_sha=head)


async def remove_worktree(
    *,
    main_repo: Path,
    worktree_path: Path,
    force: bool = True,
) -> bool:
    """Remove a worktree both via ``git worktree remove`` and on disk.

    Returns ``True`` when the worktree no longer exists at the end of the
    call. Best-effort: filesystem cleanup runs even if ``git`` complains
    about an unknown worktree (post-crash reaping path).
    """
    git_args: list[str] = ["worktree", "remove"]
    if force:
        git_args.append("--force")
    git_args.append(str(worktree_path))
    if main_repo.exists():
        try:
            await _run_git(*git_args, cwd=main_repo, check=False)
        except WorktreeAllocationFailed as exc:
            log.warning(
                "worktree_remove_git_failed",
                path=str(worktree_path),
                reason=exc.reason,
            )
    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)
    if main_repo.exists():
        await _run_git("worktree", "prune", cwd=main_repo, check=False)
    return not worktree_path.exists()


# --- Orphan quarantine + on-disk reconciliation -----------------------------
#
# The worktree allocator and ``git worktree list`` are the source of truth for
# "this path is a valid worktree". Reality can diverge: a prior session may
# have been force-killed mid-allocate, leaving a directory on disk without a
# matching registration; or git may carry a stale registration pointing at a
# directory the user manually deleted. Both states block future allocations
# at the same path (``git worktree add`` refuses to overwrite).
#
# The functions below converge from any starting state to a clean one:
#   - ``quarantine_orphan``      : atomically move one orphan dir aside
#   - ``reconcile_worktrees``    : sweep the whole worktree_root + prune
# ``ensure_worktree`` calls ``quarantine_orphan`` inline whenever it hits a
# target_path_dirty case; the manager's ``reap_session_start`` calls
# ``reconcile_worktrees`` once at session boot for the big-picture sweep.


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Outcome of ``reconcile_worktrees``.

    ``quarantined`` paths are the new locations of dirs that were on disk
    but not registered with git. Empty list = nothing diverged.
    """

    quarantined: list[Path] = field(default_factory=list)


def _quarantine_root(worktree_root: Path) -> Path:
    """Sibling-of-worktree-root layout: ``<parent>/agentshore-worktrees-orphan/<repo>``.

    For a typical bootstrap layout where ``worktree_root`` is
    ``<workspace>/agentshore-worktrees/<repo>``, this returns
    ``<workspace>/agentshore-worktrees-orphan/<repo>``.
    """
    parent = worktree_root.parent
    return parent.with_name(parent.name + "-orphan") / worktree_root.name


async def quarantine_orphan(*, orphan_path: Path, worktree_root: Path) -> Path:
    """Atomically move an orphan worktree directory into the quarantine sibling.

    Used when the allocator finds a non-registered directory at the target
    path, or when reconciliation finds an on-disk dir without a registered
    worktree. The orphan content is preserved (not deleted) so any
    uncommitted agent work survives — the user sweeps the quarantine
    manually when ready.

    Returns the quarantine destination path. Falls back to copy+remove if
    the move crosses filesystems (``shutil.move`` behaviour); on the same
    filesystem the move is atomic at the directory level.
    """
    quarantine_dir = _quarantine_root(worktree_root)
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    # ISO8601 suffix avoids collisions when the same orphan name is
    # quarantined multiple times in quick succession.
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = quarantine_dir / f"{orphan_path.name}-{timestamp}"
    # Bump the suffix if (improbably) two quarantines collide in the same second.
    counter = 1
    while destination.exists():
        destination = quarantine_dir / f"{orphan_path.name}-{timestamp}-{counter}"
        counter += 1
    try:
        await asyncio.to_thread(shutil.move, str(orphan_path), str(destination))
    except (OSError, shutil.Error) as exc:
        # ``shutil.move`` can fail when crossing filesystems with read-only
        # files, when the destination is unwritable, or when a parallel run
        # collided on the same destination after our existence check. Surface
        # the failure with structured context so the operator can intervene;
        # re-raise so the caller (``ensure_worktree`` or
        # ``reconcile_worktrees``) decides whether to abort or skip.
        log.warning(
            "quarantine_orphan_failed",
            orphan_path=str(orphan_path),
            destination=str(destination),
            error=str(exc),
        )
        raise
    return destination


@dataclass(frozen=True, slots=True)
class WorktreeRootScan:
    """Single-pass classification of ``worktree_root`` against ``git worktree list``.

    Produced by ``_walk_worktree_root_once`` so that reconciliation (orphan
    quarantine) and session-start sweep (DB-row reap) can share one
    filesystem traversal and one git-registry snapshot. Coalescing the two
    used-to-be-separate passes removes the window where they could disagree
    about which paths are registered vs orphaned.
    """

    registered_paths: set[str] = field(default_factory=set)
    orphan_dirs: list[Path] = field(default_factory=list)
    git_list_ok: bool = True


async def _walk_worktree_root_once(
    *, main_repo: Path, worktree_root: Path
) -> WorktreeRootScan:
    """Single filesystem + ``git worktree list`` pass over ``worktree_root``.

    Returns the resolved registered-paths set and the list of on-disk dirs
    that are not registered with git (candidates for quarantine). Both
    ``reconcile_worktrees`` and the session-start coalesced flow consume
    this — guarantees neither sees a different git registry view than the
    other.

    Behaviour when ``worktree_root`` doesn't exist: returns an empty scan
    (no orphans, empty registered set, git_list_ok=True).
    Behaviour when ``git worktree list`` fails: returns ``git_list_ok=False``
    with no orphans so callers can no-op safely instead of quarantining
    legitimate worktrees.
    """
    if not worktree_root.exists():
        return WorktreeRootScan(registered_paths=set(), orphan_dirs=[], git_list_ok=True)

    try:
        _, stdout, _ = await _run_git(
            "worktree", "list", "--porcelain", cwd=main_repo, check=False
        )
    except WorktreeAllocationFailed:
        log.warning("worktree_reconcile_git_list_failed", worktree_root=str(worktree_root))
        return WorktreeRootScan(registered_paths=set(), orphan_dirs=[], git_list_ok=False)

    registered_paths: set[str] = set()
    for line in stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        listed = line[len("worktree ") :].strip()
        try:
            registered_paths.add(str(Path(listed).resolve()))
        except OSError:
            continue

    orphan_dirs: list[Path] = []
    for entry in sorted(worktree_root.iterdir()):
        if not entry.is_dir():
            continue
        try:
            resolved = str(entry.resolve())
        except OSError:
            continue
        if resolved in registered_paths:
            continue
        orphan_dirs.append(entry)
    return WorktreeRootScan(
        registered_paths=registered_paths,
        orphan_dirs=orphan_dirs,
        git_list_ok=True,
    )


async def reconcile_worktrees(
    *,
    main_repo: Path,
    worktree_root: Path,
    scan: WorktreeRootScan | None = None,
) -> ReconcileReport:
    """Sweep ``worktree_root`` against ``git worktree list``; quarantine divergence.

    Three on-disk vs git states:
      1. Dir exists + registered with git → leave it alone (healthy)
      2. Dir exists + NOT registered → quarantine to the sibling root
      3. Registered with git + dir absent → ``git worktree prune`` cleans the
         registration (no on-disk action needed)

    Idempotent: a second consecutive call should produce
    ``ReconcileReport(quarantined=[])``. Safe to call from session bootstrap
    on a fresh machine (no worktree_root yet) — returns an empty report.

    Pass ``scan`` to share a single ``_walk_worktree_root_once`` result with
    other session-start work (e.g. the reaper's sweep) so both stages see
    the same git registry snapshot.
    """
    if scan is None:
        scan = await _walk_worktree_root_once(
            main_repo=main_repo, worktree_root=worktree_root
        )
    if not scan.git_list_ok:
        # Cannot enumerate git's view → can't safely identify orphans.
        return ReconcileReport(quarantined=[])

    quarantined: list[Path] = []
    for entry in scan.orphan_dirs:
        try:
            destination = await quarantine_orphan(
                orphan_path=entry, worktree_root=worktree_root
            )
        except (OSError, shutil.Error) as exc:
            log.warning(
                "worktree_orphan_quarantine_failed",
                orphan_path=str(entry),
                error=str(exc),
            )
            continue
        log.info(
            "worktree_orphan_quarantined",
            orphan_path=str(entry),
            quarantine_path=str(destination),
        )
        quarantined.append(destination)

    # Prune any stale registrations whose on-disk dir is gone.
    if main_repo.exists():
        await _run_git("worktree", "prune", cwd=main_repo, check=False)

    return ReconcileReport(quarantined=quarantined)
