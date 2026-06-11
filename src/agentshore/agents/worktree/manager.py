"""``WorktreeManager`` — AgentShore's worktree lifecycle owner.

The manager is the only thing the rest of AgentShore talks to directly:

- ``allocate_for_dispatch``  returns either a ``WorktreeAllocation`` (PR /
  branch-creating) or a ``TrunkAllocation`` (trunk-scoped plays running in
  the main checkout). Routing is driven by the play-to-worktree matrix in
  ``docs/design/HLD.md``.
- ``finalize_after_dispatch``  is called from ``PlayExecutor`` after the
  play has run. For branch-creating plays it inspects ``SkillResult.branch``
  and re-keys the row; for failed allocations it transitions to ``stale``;
  for successful PR-scoped plays it just touches ``last_used_at``.
- ``reap_session_start`` / ``reap_closed_prs``  delegate to the reaper.

The manager does NOT mutate the dispatcher's working_dir / cwd — it
returns a path and the dispatcher applies it via ``cwd_override``. This
matters for thread safety: a single ``AgentHandle`` can be dispatched
concurrently on multiple plays.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog

from agentshore.agents.worktree.allocator import (
    AllocateResult,
    WorktreeAllocationFailed,
    _list_worktrees_porcelain,
    _walk_worktree_root_once,
    ensure_worktree,
    reconcile_worktrees,
    remove_worktree,
    worktree_target_path,
)
from agentshore.agents.worktree.reaper import (
    ReapReport,
    reap_for_closed_prs,
    sweep_session_start,
)
from agentshore.agents.worktree.registry import (
    WorktreeAllocationConflict,
    WorktreeRow,
    insert_worktree,
    lookup_by_branch,
    lookup_by_prebranch_key,
    mark_status,
    touch,
)
from agentshore.agents.worktree.rekey import (
    detect_branch_in_worktree,
    rekey_worktree,
)
from agentshore.state import PlayType
from agentshore.utils import now_iso

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agentshore.config.models import RuntimeConfig
    from agentshore.data.store import DataStore
    from agentshore.plays.base import PlayParams
    from agentshore.state import PlayOutcome, SkillResult

log = structlog.get_logger(__name__)


# --- Play-to-worktree routing -------------------------------------------------

_PR_SCOPED_PLAYS: frozenset[PlayType] = frozenset(
    {
        PlayType.CODE_REVIEW,
        PlayType.UNBLOCK_PR,
    }
)
_BRANCH_CREATING_PLAYS: frozenset[PlayType] = frozenset(
    {
        PlayType.ISSUE_PICKUP,
    }
)
_TRUNK_SCOPED_PLAYS: frozenset[PlayType] = frozenset(
    {
        PlayType.RUN_QA,
        PlayType.MERGE_PR,
        PlayType.SEED_PROJECT,
        PlayType.CALIBRATE_ALIGNMENT,
        PlayType.DESIGN_AUDIT,
        PlayType.GROOM_BACKLOG,
        PlayType.REFINE_TASK_BREAKDOWN,
        PlayType.WRITE_IMPLEMENTATION_PLAN,
        # CLEANUP runs as a project-wide quality sweep against the target
        # branch. It creates its own ``chore/cleanup-*`` branch + PR
        # inside the skill (see ``agentshore-cleanup`` Step 5), so AgentShore
        # should not pre-allocate a worktree+branch for it. Pre-allocating
        # produced a ``WorktreeAllocation`` carrying ``play_type:
        # PlayType.CLEANUP``, which leaked an enum through the dispatch
        # JSON serializer and failed the play.
        PlayType.CLEANUP,
        # RECONCILE_STATE inspects + repairs project state (dirty trunk,
        # orphan worktrees, zombie subprocesses) — runs in the main checkout
        # so it sees the state it's trying to fix. Pure trunk-scoped, no
        # branch creation.
        PlayType.RECONCILE_STATE,
    }
)


# Plays that actually *mutate* the trunk working tree (merge into the default
# branch, rewrite/clean the checkout, repair a dirty trunk). These — and only
# these — take the exclusive ``trunk:main_repo`` work-claim so concurrent
# trunk mutations can't race each other into a dirty/half-merged checkout.
#
# This is DELIBERATELY narrower than ``_TRUNK_SCOPED_PLAYS``. Membership there
# means "execute in the main checkout, don't allocate a per-PR worktree" — an
# *allocation* concern. Membership here means "needs an exclusive writer lock
# on trunk" — a *serialization* concern. Read-only / metadata-only trunk plays
# (RUN_QA, DESIGN_AUDIT, CALIBRATE_ALIGNMENT, GROOM_BACKLOG, SEED_PROJECT, and
# the planning plays) run in the main checkout but only read it / update beads
# + GitHub issues, so they must NOT hold the writer lock — holding it for their
# full (multi-minute) duration starved MERGE_PR for 10–20 min at a stretch and
# left approved+mergeable PRs unmerged (issue #17).
_TRUNK_MUTATING_PLAYS: frozenset[PlayType] = frozenset(
    {
        PlayType.MERGE_PR,
        PlayType.CLEANUP,
        PlayType.RECONCILE_STATE,
    }
)


# --- Public allocation results -----------------------------------------------


@dataclass(frozen=True, slots=True)
class TrunkAllocation:
    """Sentinel returned for trunk-scoped plays — dispatch runs in ``path``."""

    path: Path


@dataclass(frozen=True, slots=True)
class WorktreeAllocation:
    """Per-PR / per-prebranch allocation; pinned to a ``worktrees`` row."""

    worktree_id: int
    path: Path
    branch_name: str | None
    pre_branch_key: str | None
    play_type: PlayType
    scope: Literal["pr", "branch_creating"]


def _systematic_debugging_is_pr_scoped(params: PlayParams) -> bool:
    """``SYSTEMATIC_DEBUGGING`` runs PR-scoped when a PR number is in scope."""
    return params.pr_number is not None


class WorktreeManager:
    """Lifecycle owner for AgentShore-managed git worktrees.

    Construction is cheap; the heavy I/O happens inside
    ``allocate_for_dispatch`` and ``finalize_after_dispatch``. The manager
    is intended to be a singleton owned by ``AgentManager``.
    """

    def __init__(
        self,
        *,
        session_id: str,
        store: DataStore,
        main_repo: Path,
        worktree_root: Path,
        cfg: RuntimeConfig,
    ) -> None:
        self._session_id = session_id
        self._store = store
        self._main_repo = main_repo.resolve()
        self._worktree_root = worktree_root.resolve()
        self._cfg = cfg
        # Per-(scope, key) locks serialize lookup → materialize → insert
        # so two concurrent dispatches against the same branch / prebranch
        # share one worktree instead of racing through the unique index.
        # _locks_guard protects the dict itself; the per-key locks are
        # held by ``async with`` blocks during allocation.
        self._alloc_locks: dict[str, asyncio.Lock] = {}
        self._alloc_locks_guard = asyncio.Lock()

    async def _get_alloc_lock(self, scope: str, key: str) -> asyncio.Lock:
        """Return the lock for ``(scope, key)``, creating it on first use."""
        lock_key = f"{scope}:{key}"
        async with self._alloc_locks_guard:
            lock = self._alloc_locks.get(lock_key)
            if lock is None:
                lock = asyncio.Lock()
                self._alloc_locks[lock_key] = lock
            return lock

    async def _evict_lock(self, scope: str, key: str) -> None:
        """Drop the lock for ``(scope, key)`` if present, no-op if not.

        Used after a worktree row transitions to a terminal status
        (reaped / failed) or the key becomes stale (prebranch → real branch
        after rekey) — keeps ``_alloc_locks`` from growing unbounded over a
        long-lived session (desktop-kdl5).
        """
        lock_key = f"{scope}:{key}"
        async with self._alloc_locks_guard:
            self._alloc_locks.pop(lock_key, None)

    async def _prune_locks(self) -> None:
        """Drop every lock whose (scope, key) has no matching active row.

        Called at the end of each reap pass. Scans the current session's
        active+reaping rows and rebuilds the live key set, then removes
        every dict entry not in that set. Cheap: lock dict is small, row
        list is bounded by the sum of each tier's ``max`` across cells.
        """
        from agentshore.agents.worktree.registry import list_active

        live_rows = await list_active(self._store, session_id=self._session_id)
        live_keys: set[str] = set()
        for row in live_rows:
            if row.branch_name is not None:
                live_keys.add(f"branch:{row.branch_name}")
            if row.pre_branch_key is not None:
                live_keys.add(f"prebranch:{row.pre_branch_key}")
        async with self._alloc_locks_guard:
            stale_keys = [k for k in self._alloc_locks if k not in live_keys]
            for k in stale_keys:
                del self._alloc_locks[k]

    @property
    def main_repo(self) -> Path:
        return self._main_repo

    @property
    def worktree_root(self) -> Path:
        return self._worktree_root

    # -- routing -------------------------------------------------------------

    def _classify(
        self, play_type: PlayType, params: PlayParams
    ) -> Literal["pr", "branch_creating", "trunk", "internal"]:
        if play_type in _PR_SCOPED_PLAYS:
            return "pr"
        if play_type in _BRANCH_CREATING_PLAYS:
            return "branch_creating"
        # SYSTEMATIC_DEBUGGING is the only play whose scope is routed
        # dynamically: every other play has a static scope membership in the
        # _PR_SCOPED_PLAYS / _BRANCH_CREATING_PLAYS / _TRUNK_SCOPED_PLAYS
        # frozensets above. Debugging splits because a dispatch carrying a PR
        # number is continuing-debug work against an existing PR worktree
        # (pr-scoped), while a dispatch without a PR is a fresh pickup-style
        # investigation against trunk (trunk-scoped).
        if play_type == PlayType.SYSTEMATIC_DEBUGGING:
            return "pr" if _systematic_debugging_is_pr_scoped(params) else "trunk"
        if play_type in _TRUNK_SCOPED_PLAYS:
            return "trunk"
        return "internal"

    # -- public surface ------------------------------------------------------

    async def allocate_for_dispatch(
        self, *, play_type: PlayType, params: PlayParams
    ) -> WorktreeAllocation | TrunkAllocation:
        """Materialise the right worktree (or return ``TrunkAllocation``)."""
        kind = self._classify(play_type, params)
        if kind == "trunk" or kind == "internal":
            return TrunkAllocation(path=self._main_repo)
        if kind == "pr":
            return await self._allocate_pr_scoped(play_type, params)
        if kind == "branch_creating":
            return await self._allocate_branch_creating(play_type, params)
        raise AssertionError(f"unhandled classification: {kind!r}")

    async def finalize_after_dispatch(
        self,
        allocation: WorktreeAllocation,
        *,
        result: SkillResult | None,
        play_outcome: PlayOutcome,
    ) -> str | None:
        """Touch / rekey the row after the play has run.

        Returns the discovered branch name on successful branch-creating
        rekey, so the caller can back-fill PR records that were persisted
        before the branch was known (desktop-edtl).

        - **PR-scoped:** bump ``last_used_at``. Failure leaves the row
          ``active`` so the next dispatch retries against the same
          worktree.
        - **Branch-creating success with ``result.branch``:** rekey to the
          real branch + rename directory.
        - **Branch-creating success without a branch:** remove the
          worktree and mark ``stale`` — the play "succeeded" without
          producing a branch (e.g. issue_pickup declined a non-actionable
          issue), so the ``pickup-<N>`` checkout has nothing worth keeping.
        - **Branch-creating failure:** remove the worktree and mark
          ``stale``.

        Branch-creating worktrees that produced no branch are removed inline
        (``git worktree remove --force`` + prune) rather than left for the
        TTL reaper: a declined/failed pickup otherwise leaks a git-registered
        ``pickup-<N>`` worktree that ``reconcile_state`` can't clear and that
        accumulates disk across short sessions (#33).
        """
        if allocation.scope == "pr":
            await touch(self._store, worktree_id=allocation.worktree_id)
            if not play_outcome.success:
                log.info(
                    "worktree_pr_play_failed_kept_active",
                    worktree_id=allocation.worktree_id,
                    play_type=allocation.play_type.value,
                )
            return None

        # branch_creating
        branch = result.branch if result is not None else None
        if branch is None and play_outcome.success:
            branch = await detect_branch_in_worktree(allocation.path)
        if play_outcome.success and branch is not None:
            try:
                await rekey_worktree(
                    self._store,
                    row=await _require_row(self._store, allocation.worktree_id),
                    branch_name=branch,
                    worktree_root=self._worktree_root,
                )
            except WorktreeAllocationConflict as exc:
                log.warning(
                    "worktree_rekey_conflict",
                    worktree_id=allocation.worktree_id,
                    branch=branch,
                    error=str(exc),
                )
            else:
                if allocation.pre_branch_key is not None:
                    await self._evict_lock("prebranch", allocation.pre_branch_key)
                return branch
            return None
        # Row moved to stale (no branch or play failure) — evict the
        # prebranch lock; the row is on its way out (desktop-kdl5).
        if allocation.pre_branch_key is not None:
            await self._evict_lock("prebranch", allocation.pre_branch_key)
        # Remove the leaked checkout now (git + disk + prune) so a declined or
        # failed pickup doesn't leave a git-registered pickup-<N> worktree
        # behind (#33). Never raises.
        await _best_effort_remove(self._main_repo, allocation.path)
        await mark_status(
            self._store,
            worktree_id=allocation.worktree_id,
            status="stale",
            failure_reason=(
                "branch_creating_no_branch"
                if play_outcome.success
                else f"branch_creating_failed: {play_outcome.error or 'unknown'}"
            ),
        )
        return None

    async def reap_session_start(self) -> ReapReport:
        """Reap leftovers from prior sessions.

        Coalesced single-pass:
          1. ``_walk_worktree_root_once`` builds a unified snapshot of
             ``worktree_root`` vs ``git worktree list`` — one filesystem
             traversal, one git invocation, so the reconcile and DB-sweep
             stages can't disagree about which paths are registered.
          2. ``reconcile_worktrees`` consumes the scan to delete
             unregistered on-disk dirs (preserving any with uncommitted
             work) (closes #570).
          3. ``sweep_session_start`` reaps DB rows from prior sessions
             (existing behaviour).
        """
        scan = await _walk_worktree_root_once(
            main_repo=self._main_repo, worktree_root=self._worktree_root
        )
        reconcile = await reconcile_worktrees(
            main_repo=self._main_repo,
            worktree_root=self._worktree_root,
            scan=scan,
        )
        if reconcile.deleted or reconcile.preserved_dirty:
            log.info(
                "worktree_reconcile_summary",
                deleted_count=len(reconcile.deleted),
                deleted_paths=[str(p) for p in reconcile.deleted],
                preserved_dirty_count=len(reconcile.preserved_dirty),
                preserved_dirty_paths=[str(p) for p in reconcile.preserved_dirty],
            )
        report = await sweep_session_start(
            self._store,
            current_session_id=self._session_id,
            main_repo=self._main_repo,
        )
        # One-shot migration: AgentShore used to quarantine orphans into a
        # ``<root>-orphan`` sibling that was never reliably reaped (a monitored
        # machine accumulated 116 GB of Rust build caches there). Orphans are
        # now deleted in reconcile, so remove any pre-existing quarantine dir.
        await self._remove_legacy_orphan_dir()
        # Prune locks whose (scope, key) no longer maps to a live row in
        # the current session (desktop-kdl5).
        await self._prune_locks()
        return report

    async def _remove_legacy_orphan_dir(self) -> None:
        """Delete a pre-existing ``<worktree_root>-orphan`` quarantine dir (best-effort)."""
        import asyncio
        import shutil

        legacy = self._worktree_root.with_name(self._worktree_root.name + "-orphan")
        if not legacy.exists():
            return
        await asyncio.to_thread(shutil.rmtree, legacy, ignore_errors=True)
        log.info("worktree_legacy_orphan_dir_removed", path=str(legacy))

    async def reap_closed_prs(self, *, ttl_seconds: int) -> ReapReport:
        """Reap ``stale`` rows older than ``ttl_seconds`` in this session."""
        report = await reap_for_closed_prs(
            self._store,
            session_id=self._session_id,
            main_repo=self._main_repo,
            ttl_seconds=ttl_seconds,
        )
        await self._prune_locks()
        return report

    # -- internals -----------------------------------------------------------

    async def _verify_worktree_registered(self, allocate: AllocateResult, *, scope: str) -> None:
        """Confirm ``git worktree add`` registration actually landed.

        After ``ensure_worktree`` reports ``created=True`` we expect the path
        to appear in ``git worktree list --porcelain``. If it doesn't, git
        either crashed mid-add or the on-disk dir was created without the
        matching admin entry — both leak unowned dirs on subsequent
        allocations. Best-effort cleanup the on-disk path and raise
        ``WorktreeAllocationFailed`` so the caller doesn't insert a row
        pointing at a phantom worktree. Reuse (``created=False``) is a
        no-op: the existing-worktree code path already confirmed
        registration via ``_existing_worktree_for_path``.
        """
        if not allocate.created:
            return
        registered = await _list_worktrees_porcelain(self._main_repo)
        try:
            target_resolved = str(allocate.path.resolve())
        except OSError:
            target_resolved = str(allocate.path)
        if target_resolved in registered:
            return
        log.warning(
            "worktree_add_mismatch_after_success",
            path=str(allocate.path),
            scope=scope,
            registered=registered,
        )
        await _best_effort_remove(self._main_repo, allocate.path)
        raise WorktreeAllocationFailed(
            f"git worktree add reported success for {allocate.path} but the "
            "path is not registered in `git worktree list --porcelain`",
            reason="git_add_mismatch",
        )

    async def _allocate_pr_scoped(
        self, play_type: PlayType, params: PlayParams
    ) -> WorktreeAllocation:
        branch = params.branch
        if not branch:
            raise WorktreeAllocationFailed(
                f"PR-scoped play {play_type.value} dispatched without params.branch",
                reason="missing_branch",
            )
        lock = await self._get_alloc_lock("branch", branch)
        async with lock:
            return await self._allocate_pr_scoped_locked(play_type, branch)

    async def _allocate_pr_scoped_locked(
        self, play_type: PlayType, branch: str
    ) -> WorktreeAllocation:
        return await self._allocate_locked(
            play_type=play_type,
            lookup=lambda: lookup_by_branch(
                self._store, session_id=self._session_id, branch_name=branch
            ),
            branch_name=branch,
            pre_branch_key=None,
            target_key=branch,
            base_ref=f"origin/{branch}",
            scope="pr",
        )

    async def _allocate_branch_creating(
        self, play_type: PlayType, params: PlayParams
    ) -> WorktreeAllocation:
        pre_branch_key = _make_prebranch_key(play_type, params)
        lock = await self._get_alloc_lock("prebranch", pre_branch_key)
        async with lock:
            return await self._allocate_branch_creating_locked(play_type, pre_branch_key)

    async def _allocate_branch_creating_locked(
        self, play_type: PlayType, pre_branch_key: str
    ) -> WorktreeAllocation:
        return await self._allocate_locked(
            play_type=play_type,
            lookup=lambda: lookup_by_prebranch_key(
                self._store, session_id=self._session_id, pre_branch_key=pre_branch_key
            ),
            branch_name=None,
            pre_branch_key=pre_branch_key,
            target_key=pre_branch_key,
            base_ref="origin/HEAD",
            scope="branch_creating",
        )

    async def _allocate_locked(
        self,
        *,
        play_type: PlayType,
        lookup: Callable[[], Awaitable[WorktreeRow | None]],
        branch_name: str | None,
        pre_branch_key: str | None,
        target_key: str,
        base_ref: str,
        scope: Literal["pr", "branch_creating"],
    ) -> WorktreeAllocation:
        """Shared allocate body for PR-scoped and branch-creating plays.

        Callers supply the distinct lookup, key fields, and ``base_ref``; the
        reuse-existing → ensure → touch → return and insert → conflict-relookup
        → best-effort-remove → return skeletons are identical between the two.
        """
        existing = await lookup()
        if existing is not None:
            try:
                allocate = await ensure_worktree(
                    main_repo=self._main_repo,
                    worktree_path=Path(existing.worktree_path),
                    branch_name=branch_name,
                    base_ref=base_ref,
                    fetch=True,
                )
            except WorktreeAllocationFailed as exc:
                # Existing-row reuse failed (disk gone, target dirty, etc).
                # Mark the row stale so the next reap pass can drop it
                # instead of leaving an "active" row pointing at nothing.
                await mark_status(
                    self._store,
                    worktree_id=existing.worktree_id,
                    status="stale",
                    failure_reason=f"reuse_ensure_failed: {exc.reason}",
                )
                raise
            await touch(self._store, worktree_id=existing.worktree_id, head_sha=allocate.head_sha)
            return WorktreeAllocation(
                worktree_id=existing.worktree_id,
                path=allocate.path,
                branch_name=branch_name,
                pre_branch_key=pre_branch_key,
                play_type=play_type,
                scope=scope,
            )

        target = worktree_target_path(self._worktree_root, target_key)
        allocate = await ensure_worktree(
            main_repo=self._main_repo,
            worktree_path=target,
            branch_name=branch_name,
            base_ref=base_ref,
            fetch=True,
        )
        await self._verify_worktree_registered(allocate, scope=scope)
        try:
            row = await insert_worktree(
                self._store,
                session_id=self._session_id,
                branch_name=branch_name,
                pre_branch_key=pre_branch_key,
                worktree_path=str(allocate.path),
                original_play_type=play_type.value,
                base_ref=base_ref,
                head_sha=allocate.head_sha,
            )
        except WorktreeAllocationConflict:
            existing = await lookup()
            if existing is None:
                raise
            await touch(
                self._store,
                worktree_id=existing.worktree_id,
                head_sha=allocate.head_sha,
            )
            return WorktreeAllocation(
                worktree_id=existing.worktree_id,
                path=Path(existing.worktree_path),
                branch_name=branch_name,
                pre_branch_key=pre_branch_key,
                play_type=play_type,
                scope=scope,
            )
        except Exception:
            # Insert failed for a non-conflict reason (DB connection,
            # operational error, ...). The on-disk worktree we just
            # materialised has no owning row and would leak — drop it.
            await _best_effort_remove(self._main_repo, allocate.path)
            raise
        return WorktreeAllocation(
            worktree_id=row.worktree_id,
            path=allocate.path,
            branch_name=branch_name,
            pre_branch_key=pre_branch_key,
            play_type=play_type,
            scope=scope,
        )


def _make_prebranch_key(play_type: PlayType, params: PlayParams) -> str:
    """Stable key for a branch-creating allocation prior to branch resolution."""
    if play_type == PlayType.ISSUE_PICKUP:
        if params.issue_number is not None:
            return f"pickup-{params.issue_number}"
        bead = params.extras.get("bead_id") if params.extras else None
        if isinstance(bead, str) and bead:
            return f"pickup-{bead}"
        return f"pickup-unknown-{now_iso()}"
    if play_type == PlayType.CLEANUP:
        return f"cleanup-{now_iso()}"
    return f"{play_type.value}-{now_iso()}"


async def _require_row(store: DataStore, worktree_id: int) -> WorktreeRow:
    """Fetch the row by id, raising if it's missing — used during finalize."""
    from agentshore.agents.worktree.registry import lookup_by_id

    row = await lookup_by_id(store, worktree_id=worktree_id)
    if row is None:
        msg = f"worktree row {worktree_id} missing during finalize"
        raise RuntimeError(msg)
    return row


async def _best_effort_remove(main_repo: Path, worktree_path: Path) -> None:
    """Drop an on-disk worktree with no owning row. Never raises."""
    try:
        await remove_worktree(main_repo=main_repo, worktree_path=worktree_path, force=True)
    except Exception as exc:
        log.warning(
            "worktree_orphan_cleanup_failed",
            path=str(worktree_path),
            error=str(exc),
        )


__all__ = [
    "TRUNK_MUTATING_PLAYS",
    "TRUNK_SCOPED_PLAYS",
    "TrunkAllocation",
    "WorktreeAllocation",
    "WorktreeManager",
]


# Public re-exports so other modules can ask "is this play type trunk-scoped?"
# (allocation) or "does this play mutate trunk?" (serialization) without
# reaching into the private constants.
TRUNK_SCOPED_PLAYS: frozenset[PlayType] = _TRUNK_SCOPED_PLAYS
TRUNK_MUTATING_PLAYS: frozenset[PlayType] = _TRUNK_MUTATING_PLAYS
