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
from uuid import uuid4

import structlog

from agentshore import subprocess_env
from agentshore.agents.identity import (
    resolve_identity_env_by_name,
    select_default_git_identity,
)
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
    _canon_path,
    free_disk_mb,
    reap_for_closed_prs,
    reap_for_disk_pressure,
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
    from collections.abc import Awaitable, Callable, Mapping

    from agentshore.config.models import RuntimeConfig
    from agentshore.data.store import DataStore
    from agentshore.plays.base import PlayParams
    from agentshore.state import PlayOutcome, SkillResult

log = structlog.get_logger(__name__)


def _resolve_fetch_overlay(cfg: RuntimeConfig) -> dict[str, str] | None:
    """Build the git-auth overlay for the shared worktree fetch, or ``None``.

    Selects the default git identity (gh-OAuth-preferred, see
    :func:`select_default_git_identity`), resolves its credential env, and pairs
    the token with the HTTPS auth header via
    :func:`subprocess_env.git_auth_config_overlay`. Any miss (no identities, no
    token) yields ``None`` so the fetch falls back to unauthenticated — never
    breaking allocation.
    """
    name = select_default_git_identity(cfg)
    if not name:
        return None
    ident_env = resolve_identity_env_by_name(cfg, name)
    token = ident_env.get("GH_TOKEN") or ident_env.get("GITHUB_TOKEN")
    if not token:
        log.info("worktree_fetch_identity_unauthenticated", identity=name)
        return None
    overlay = dict(ident_env)
    overlay.update(subprocess_env.git_auth_config_overlay(token))
    log.info("worktree_fetch_identity_selected", identity=name)
    return overlay


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


@dataclass(frozen=True, slots=True)
class VanishedProtectedWorktrees:
    """Classification of protected rows whose on-disk directory has vanished.

    ``in_flight`` maps ``worktree_id`` → canonical path for rows backed by
    positive evidence of a live dispatch (in-flight registry entry or an active
    work claim on the row's issue/PR) — losing one of those directories is a
    genuine clobber of running work. ``retired`` holds the rows that were only
    stale bookkeeping: the work is finished (merged/closed/finalized), nothing
    is dispatched against them, so their rows were transitioned to ``reaped``
    instead of lingering "protected" (#360). ``reasons`` records why each id
    landed where it did, for the caller's log line.
    """

    in_flight: dict[int, str]
    retired: dict[int, str]
    reasons: dict[int, str]


def _issue_number_from_prebranch_key(pre_branch_key: str | None) -> int | None:
    """Extract the issue number from a ``pickup-<N>`` pre-branch key, if numeric."""
    if not pre_branch_key:
        return None
    prefix = _RECLAIMABLE_COLLISION_PREFIX
    if not pre_branch_key.startswith(prefix):
        return None
    tail = pre_branch_key[len(prefix) :]
    return int(tail) if tail.isdigit() else None


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
        # Memoized git-auth overlay for the shared (agent-agnostic) worktree
        # fetch; resolved once on first allocation. ``None`` == unauthenticated.
        self._fetch_overlay: dict[str, str] | None = None
        self._fetch_overlay_resolved = False
        # Consecutive PR-play failures per worktree_id, for the retention cap
        # (``worktrees.reap_failed_pr_after_n``). A successful finalize clears
        # the entry; once the count crosses the cap the row is dropped to
        # ``stale`` instead of kept warm (#180).
        self._pr_failure_counts: dict[int, int] = {}
        # worktree_id → canonical on-disk path for every dispatch currently in
        # flight. The manager is the SOLE owner of "is this worktree live?":
        # reap paths read this internally (``_protected_ids``/``_protected_paths``),
        # so no caller can reap without protection. Storing BOTH id and path holds
        # back the #203 dup-path alias (a stale old-id row sharing the live path).
        # Replaces the silently-failing ``getattr(ctx.params._runtime_allocation…)``
        # chains that reconstructed this set at every reap site.
        self._inflight: dict[int, str] = {}

    # ------------------------------------------------------------------
    # In-flight dispatch registry (single owner of the reap-protection invariant)
    # ------------------------------------------------------------------

    def register_dispatch(self, allocation: WorktreeAllocation) -> None:
        """Mark a worktree in-flight for the duration of a dispatch.

        Called by the dispatcher immediately after ``allocate_for_dispatch``
        returns a ``WorktreeAllocation`` (``TrunkAllocation`` is a no-op — no
        row, nothing to protect). Stores the id and the canonical path so reaps
        skip both the live row and any stale row aliasing its path (#203).
        """
        self._inflight[allocation.worktree_id] = _canon_path(allocation.path)

    def release_dispatch(self, allocation: WorktreeAllocation) -> None:
        """Clear the in-flight mark. Folded into ``finalize_after_dispatch``; a
        leaked entry only ever *over*-protects (never under-protects), so the
        backstop release at the completion-pop path is belt-and-suspenders."""
        self._inflight.pop(allocation.worktree_id, None)

    def _protected_ids(self) -> set[int]:
        return set(self._inflight)

    def _protected_paths(self) -> set[str]:
        return set(self._inflight.values())

    async def _live_alias_paths(self) -> set[str]:
        """Canon paths of every ``active``/``reaping`` row in this session.

        DB-truth complement to the in-memory ``_inflight`` path set for the
        closed-PR TTL reaper. The reaper removes a ``stale`` row *by path*; the
        uniquifier only diverts a fresh allocation when an **active** row holds
        the canonical ``pickup-<N>`` path, so a row that has since gone
        ``stale`` can still share a directory with a live re-dispatch on the
        same path. ``_inflight`` is supposed to hold that path back, but it
        depends on ``register_dispatch`` having run and stayed live — a single
        gap reaps the live checkout out from under the agent (#243; the
        in-flight skip never fired across a long production run). Reading the
        live rows straight from the DB removes that dependency: any stale row
        aliasing a live directory is skipped regardless of registry state.
        """
        from agentshore.agents.worktree.registry import list_active  # noqa: PLC0415

        rows = await list_active(self._store, session_id=self._session_id)
        return {_canon_path(r.worktree_path) for r in rows}

    async def live_protected_rows(self) -> dict[int, str]:
        """Map every currently-protected ``worktree_id`` to its canonical path.

        Public counterpart to ``_protected_ids``/``_protected_paths``/
        ``_live_alias_paths`` for callers OUTSIDE the manager that need the same
        "is this worktree currently live" truth the reaper already trusts,
        without reaching into private internals. Union of the in-memory
        in-flight dispatch registry and every ``active``/``reaping`` DB row in
        this session — the reaper regime's exact protection set (#311).

        Used by the PRUNE/RECONCILE_STATE skill context injection (to widen the
        advisory ``active_worktree_paths`` list beyond the narrower DB-only
        ``collect_active_worktree_paths`` query, which misses ``reaping`` rows)
        and by the post-hoc destructive-sweep guard in
        ``plays/skill_backed/base.py`` (to detect a skill that removed a live
        worktree despite the advisory list).
        """
        from agentshore.agents.worktree.registry import list_active  # noqa: PLC0415

        protected: dict[int, str] = dict(self._inflight)
        rows = await list_active(self._store, session_id=self._session_id)
        for row in rows:
            protected[row.worktree_id] = _canon_path(row.worktree_path)
        return protected

    async def live_protected_paths(self) -> set[str]:
        """Canonical paths of every currently-protected worktree.

        Convenience wrapper around :meth:`live_protected_rows` for callers that
        only need the path set, not the id→path mapping.
        """
        return set((await self.live_protected_rows()).values())

    async def reconcile_vanished_protected_rows(self) -> VanishedProtectedWorktrees:
        """Split protected rows whose directory is gone into live work vs stale rows.

        ``live_protected_rows`` is a *union* of the in-flight dispatch registry
        and every ``active``/``reaping`` DB row. The DB half is bookkeeping, and
        that bookkeeping goes stale: the issue-syncer only marks a row ``stale``
        when it re-fetches that exact PR in this session, so a worktree whose PR
        merged in an earlier session (or whose branch never matched a mirrored
        PR) keeps an ``active`` row long after its work is dead. Treating such a
        row's missing directory as a destructive clobber turns a *correct* prune
        into a failed play with an ERROR-level "protected worktree removed"
        alarm (#360) — the noodle incident flagged nine already-merged worktrees.

        This method reconciles each vanished row against actual liveness before
        anyone calls it a clobber:

        - **Live** (violation): the row is in the in-memory in-flight registry
          (by id or by canonical path), or an active/claimed/running work claim
          still exists for the row's issue (``pickup-<N>``) or its PR. That is
          real running work whose checkout disappeared — exactly the
          #189/#195/#203/#238/#243/#250 family the guard exists for.
        - **Stale** (bookkeeping): nothing is dispatched against it. The
          directory is already gone, so retiring the row destroys nothing — it
          just stops the row from lingering "protected" and re-tripping the
          guard on every later prune. Rows are transitioned to ``reaped`` and
          their allocation locks evicted.

        Never removes anything from disk. Callers treat only ``in_flight`` as an
        incident; ``retired`` is a warning-level bookkeeping repair.
        """
        from agentshore.agents.worktree.registry import list_active  # noqa: PLC0415

        protected = await self.live_protected_rows()
        missing = {wid: path for wid, path in protected.items() if not Path(path).exists()}
        if not missing:
            return VanishedProtectedWorktrees(in_flight={}, retired={}, reasons={})

        inflight_ids = set(self._inflight)
        inflight_paths = set(self._inflight.values())
        rows = {
            row.worktree_id: row
            for row in await list_active(self._store, session_id=self._session_id)
        }
        # branch → (pr_number, linked issue numbers) for every PR still live in
        # the mirror. A row whose branch is absent here has no open/approved PR
        # to be working on — merged, closed, or never mirrored.
        active_prs: dict[str, tuple[int, tuple[int, ...]]] = {}
        if any(rows.get(wid) is not None and rows[wid].branch_name is not None for wid in missing):
            for pr in await self._store.list_active_pull_requests(self._session_id):
                if pr.branch:
                    active_prs[pr.branch] = (pr.pr_number, pr.linked_issue_numbers)

        in_flight: dict[int, str] = {}
        retired: dict[int, str] = {}
        reasons: dict[int, str] = {}
        for wid, path in sorted(missing.items()):
            if wid in inflight_ids or path in inflight_paths:
                in_flight[wid] = path
                reasons[wid] = "inflight_dispatch"
                continue
            row = rows.get(wid)
            claim_key = (
                None if row is None else await self._active_claim_key_for_row(row, active_prs)
            )
            if claim_key is not None:
                in_flight[wid] = path
                reasons[wid] = f"active_work_claim:{claim_key}"
                continue
            if row is None:
                reason = "row_not_live"
            elif row.status == "reaping":
                reason = "reaping_row"
            elif row.branch_name is not None and row.branch_name in active_prs:
                reason = "no_active_dispatch_pr_open"
            else:
                reason = "no_active_dispatch"
            reasons[wid] = reason
            retired[wid] = path
            if row is not None:
                await self._retire_vanished_row(row, reason)

        if in_flight or retired:
            log.info(
                "worktree_vanished_rows_reconciled",
                in_flight=sorted(in_flight.values()),
                retired=sorted(retired.values()),
                reasons={str(wid): reason for wid, reason in sorted(reasons.items())},
            )
        return VanishedProtectedWorktrees(in_flight=in_flight, retired=retired, reasons=reasons)

    async def _active_claim_key_for_row(
        self,
        row: WorktreeRow,
        active_prs: Mapping[str, tuple[int, tuple[int, ...]]],
    ) -> str | None:
        """Return the resource key of an active work claim covering *row*, if any.

        DB-truth liveness signal that does not depend on ``register_dispatch``
        having landed (the #243 registry gap). A branch-creating row carries the
        issue in its ``pickup-<N>`` pre-branch key; a PR-scoped row resolves via
        the PR mirror to ``pr:<N>`` plus its linked issues. Any claim in an
        active status (queued/claimed/running/retrying) on one of those keys
        means a play is still working this worktree's subject.
        """
        keys: set[str] = set()
        issue_number = _issue_number_from_prebranch_key(row.pre_branch_key)
        if issue_number is not None:
            keys.add(f"issue:{issue_number}")
        if row.branch_name is not None:
            entry = active_prs.get(row.branch_name)
            if entry is not None:
                pr_number, linked_issues = entry
                keys.add(f"pr:{pr_number}")
                keys.update(f"issue:{n}" for n in linked_issues)
        if not keys:
            return None
        claims = await self._store.find_active_work_claims(self._session_id, sorted(keys))
        return claims[0].resource_key if claims else None

    async def _retire_vanished_row(self, row: WorktreeRow, reason: str) -> None:
        """Transition a stale, directory-less row to ``reaped``. Never raises."""
        try:
            await mark_status(
                self._store,
                worktree_id=row.worktree_id,
                status="reaped",
                failure_reason=f"vanished_dir:{reason}",
            )
        except Exception as exc:  # noqa: BLE001 — bookkeeping repair is best-effort
            log.warning(
                "worktree_vanished_row_retire_failed",
                worktree_id=row.worktree_id,
                path=row.worktree_path,
                error=str(exc),
            )
            return
        self._pr_failure_counts.pop(row.worktree_id, None)
        if row.branch_name is not None:
            await self._evict_lock("branch", row.branch_name)
        if row.pre_branch_key is not None:
            await self._evict_lock("prebranch", row.pre_branch_key)
        log.info(
            "worktree_vanished_row_retired",
            worktree_id=row.worktree_id,
            path=row.worktree_path,
            reason=reason,
        )

    def _build_reclaimable_collision_predicate(
        self, live_alias_paths: set[str]
    ) -> Callable[[Path], bool]:
        """Build the ``safe_to_force_remove`` predicate for one allocate call.

        A colliding ``pickup-*`` worktree is a reclaimable crashed-session orphan
        ONLY when it is not a live in-flight dispatch. The branch-collision retry
        (``_add_worktree_with_collision_retry``) force-removes the worktree git
        names as the collision holder; when two dispatches contend for the same
        ``pickup-<issue>`` branch (e.g. a claim-lost repick of the same issue),
        that holder can be a *running* agent's checkout. Force-removing it deletes
        the cwd out from under the agent — the #238 "worktree reclaimed mid-play"
        failure. Refusing protected paths here makes the collision bubble as a
        normal allocation failure (→ the contending dispatch backs off / repicks)
        instead of clobbering the live one.

        The protected set is the union of the in-memory in-flight registry
        (``_protected_paths``) and ``live_alias_paths`` — a DB-truth snapshot of
        every ``active``/``reaping`` row's canonical path, taken just before the
        allocate. The in-memory registry can miss a live dispatch whose
        ``register_dispatch`` never landed or was cleared early (#243), which left
        the #238 collision guard defeatable: the recurrence in #250 force-removed a
        live ``pickup-<N>`` whose path was absent from ``_inflight``. Unioning the
        DB snapshot closes that gap — a path backing any live row in this session
        is never a reclaimable orphan, regardless of registry state. The
        name-prefix check still gates genuine crashed-session orphans.
        """
        protected = self._protected_paths() | live_alias_paths

        def _predicate(path: Path) -> bool:
            if _canon_path(path) in protected:
                return False
            return _is_reclaimable_collision(path)

        return _predicate

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

    def _fetch_auth_overlay(self) -> Mapping[str, str] | None:
        """Memoized git-auth env for the shared, agent-agnostic worktree fetch.

        The shared main-repo fetch runs before any agent is bound and is
        read-only (no authorship/push/PR), so a single read-capable identity is
        correct and carries no write/attribution semantics — the per-agent write
        path stays each agent's own identity. Resolves the default git identity
        (gh-OAuth-preferred) once; returns ``None`` when none resolves, leaving
        the fetch unauthenticated (its historical best-effort behavior).
        """
        if not self._fetch_overlay_resolved:
            self._fetch_overlay_resolved = True
            self._fetch_overlay = _resolve_fetch_overlay(self._cfg)
        return self._fetch_overlay

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
        # The dispatch is finalizing — clear its in-flight protection mark. The
        # row transitions below (touch / rekey / stale) set its terminal
        # disposition; from here it is eligible for reaping like any other row.
        self.release_dispatch(allocation)
        if allocation.scope == "pr":
            wt_id = allocation.worktree_id
            if play_outcome.success:
                self._pr_failure_counts.pop(wt_id, None)
                await touch(self._store, worktree_id=wt_id)
                return None
            # Failure path. Keep the worktree warm for a cheap retry on the
            # first failures; after the configured cap, a worktree that keeps
            # failing is not a useful cache — drop it to ``stale`` so the TTL
            # reaper reclaims its disk instead of holding it until PR close
            # (the worktree-sprawl contributor in #180).
            fails = self._pr_failure_counts.get(wt_id, 0) + 1
            self._pr_failure_counts[wt_id] = fails
            cap = self._cfg.worktrees.reap_failed_pr_after_n
            if cap > 0 and fails >= cap:
                self._pr_failure_counts.pop(wt_id, None)
                await mark_status(
                    self._store,
                    worktree_id=wt_id,
                    status="stale",
                    failure_reason=f"pr_play_failed_x{fails}",
                )
                log.info(
                    "worktree_pr_play_failed_retired",
                    worktree_id=wt_id,
                    play_type=allocation.play_type.value,
                    consecutive_failures=fails,
                )
            else:
                await touch(self._store, worktree_id=wt_id)
                log.info(
                    "worktree_pr_play_failed_kept_active",
                    worktree_id=wt_id,
                    play_type=allocation.play_type.value,
                    consecutive_failures=fails,
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
        if reconcile.deleted or reconcile.quarantined or reconcile.preserved_dirty:
            log.info(
                "worktree_reconcile_summary",
                deleted_count=len(reconcile.deleted),
                deleted_paths=[str(p) for p in reconcile.deleted],
                quarantined_count=len(reconcile.quarantined),
                quarantined_paths=[str(p) for p in reconcile.quarantined],
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
        """Reap ``stale`` rows older than ``ttl_seconds`` in this session.

        In-flight worktrees are held back from the manager-owned registry
        (``_protected_ids``/``_protected_paths``) so a caller cannot reap
        without protection. A PR can close while its worktree is mid-play, and
        reclaiming it out from under the running play is the "worktree
        reclaimed mid-play" failure (#189); the path set additionally holds
        back the #203 dup-path alias (a stale old-id row sharing the live
        path). The protected paths are unioned with ``_live_alias_paths`` —
        DB truth for every active/reaping row — so a stale row aliasing a live
        ``pickup-<N>`` directory is skipped even when the in-memory in-flight
        registry missed it (#243). Both are passed to the shared
        ``reap_for_closed_prs`` loop.
        """
        protected_paths = self._protected_paths() | await self._live_alias_paths()
        report = await reap_for_closed_prs(
            self._store,
            session_id=self._session_id,
            main_repo=self._main_repo,
            ttl_seconds=ttl_seconds,
            protected_ids=self._protected_ids(),
            protected_paths=protected_paths,
        )
        await self._prune_locks()
        return report

    def free_disk_mb(self) -> int:
        """Free space (MiB) on the filesystem backing the worktree root."""
        return free_disk_mb(self._worktree_root)

    async def reap_for_disk_pressure(self, *, target_free_mb: int) -> ReapReport:
        """Reap idle worktrees LRU until free disk reaches ``target_free_mb``.

        The build-agnostic disk governor (#180). In-flight worktrees are held
        back from the manager-owned registry (by id, and by path for the #203
        dup-path alias) so a caller cannot reap without protection.
        """
        report = await reap_for_disk_pressure(
            self._store,
            session_id=self._session_id,
            main_repo=self._main_repo,
            worktree_root=self._worktree_root,
            target_free_mb=target_free_mb,
            protected_ids=self._protected_ids(),
            protected_paths=self._protected_paths(),
        )
        if report.total:
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
                    fetch_env_overlay=self._fetch_auth_overlay(),
                    safe_to_force_remove=self._build_reclaimable_collision_predicate(
                        await self._live_alias_paths()
                    ),
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
        # Per-attempt unique path (#203). The prebranch key (e.g. ``pickup-<N>``)
        # is intentionally STABLE so ``lookup_by_prebranch_key`` reuse /
        # resumability keeps working: a second dispatch for the same issue
        # before the branch is cut shares the existing row (handled above). But
        # the canonical directory ``pickup-<N>`` is reused across *attempts* —
        # many distinct worktree_id rows can share one path — and a stale
        # OLD-id row at that path lets the closed-PR TTL reaper remove the
        # directory a LIVE new-id row is using. So when the canonical path is
        # already held by a live row in this session, give this fresh
        # allocation a unique sibling directory instead of aliasing it.
        if await self._canon_path_live_in_session(target):
            target = self._uniquify_target_path(target)
        allocate = await ensure_worktree(
            main_repo=self._main_repo,
            worktree_path=target,
            branch_name=branch_name,
            base_ref=base_ref,
            fetch=True,
            fetch_env_overlay=self._fetch_auth_overlay(),
            safe_to_force_remove=self._build_reclaimable_collision_predicate(
                await self._live_alias_paths()
            ),
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
        # Coalesce any lingering non-terminal row that still points at the same
        # stored path (#203). The unique-path guard above keeps NEW allocations
        # off a live row's path, but pre-existing dup-path rows from before this
        # fix (or from a reused canonical path whose old row was never retired)
        # would still alias the new directory. Retiring them to ``stale`` here
        # stops dup-path rows from accumulating; the path-aware reap skip
        # (below) guards the live row in the meantime.
        await self._coalesce_dup_path_rows(row)
        return WorktreeAllocation(
            worktree_id=row.worktree_id,
            path=allocate.path,
            branch_name=branch_name,
            pre_branch_key=pre_branch_key,
            play_type=play_type,
            scope=scope,
        )

    async def _canon_path_live_in_session(self, target: Path) -> bool:
        """True when an active/reaping row in this session holds ``target``'s canon path."""
        from agentshore.agents.worktree.registry import list_active

        target_canon = _canon_path(target)
        live_rows = await list_active(self._store, session_id=self._session_id)
        return any(_canon_path(r.worktree_path) == target_canon for r in live_rows)

    @staticmethod
    def _uniquify_target_path(target: Path) -> Path:
        """Append a short unique suffix to ``target``'s directory name.

        Keeps the reclaimable ``pickup-`` prefix (so a crashed-session orphan
        is still force-removable via ``_is_reclaimable_collision``) and the
        slug-safe charset, while making each attempt's directory distinct.
        """
        short = uuid4().hex[:8]
        return target.with_name(f"{target.name}-{short}")

    async def _coalesce_dup_path_rows(self, row: WorktreeRow) -> None:
        """Retire stale dup-path rows aliasing ``row``'s on-disk path (#203, best-effort)."""
        try:
            retired = await self._store.retire_stale_worktrees_at_path(
                session_id=self._session_id,
                worktree_path=row.worktree_path,
                keep_worktree_id=row.worktree_id,
            )
        except Exception as exc:  # noqa: BLE001 — coalesce is best-effort, never block allocation
            log.warning(
                "worktree_dup_path_coalesce_failed",
                worktree_id=row.worktree_id,
                path=row.worktree_path,
                error=str(exc),
            )
            return
        if retired:
            log.info(
                "worktree_dup_path_coalesced",
                kept_worktree_id=row.worktree_id,
                retired_worktree_ids=retired,
                path=row.worktree_path,
            )


#: Directory-name prefix the manager keys a branch-creating ISSUE_PICKUP
#: allocation with (see :func:`_make_prebranch_key`). A *colliding* worktree
#: with this prefix is a crashed-session branch-creating orphan that never
#: rekeyed, so it is safe to force-remove and retry the add. The convention
#: lives here, in the manager, because the manager is the only layer that
#: assigns these keys — the allocator delegates the decision via the
#: ``safe_to_force_remove`` predicate it is handed.
_RECLAIMABLE_COLLISION_PREFIX = "pickup-"


def _is_reclaimable_collision(path: Path) -> bool:
    """Return True when a colliding worktree path is a reclaimable pickup orphan."""
    return path.name.startswith(_RECLAIMABLE_COLLISION_PREFIX)


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
    "VanishedProtectedWorktrees",
    "WorktreeAllocation",
    "WorktreeManager",
]


# Public re-exports so other modules can ask "is this play type trunk-scoped?"
# (allocation) or "does this play mutate trunk?" (serialization) without
# reaching into the private constants.
TRUNK_SCOPED_PLAYS: frozenset[PlayType] = _TRUNK_SCOPED_PLAYS
TRUNK_MUTATING_PLAYS: frozenset[PlayType] = _TRUNK_MUTATING_PLAYS


def requires_isolated_worktree(play_type: PlayType) -> bool:
    """True when *play_type* MUST run in an isolated worktree, never the main checkout.

    PR-scoped (``CODE_REVIEW``, ``UNBLOCK_PR``) and branch-creating
    (``ISSUE_PICKUP``) plays have their agent create/switch branches. If such a
    play is dispatched into the main checkout — because its
    ``_runtime_allocation`` was lost before dispatch (e.g. a replay/retry path
    rebuilt ``PlayParams`` without the dispatcher's allocator stamp) — the
    agent's ``git switch -c`` moves the **main checkout's** HEAD onto a feature
    branch and wedges the trunk-dispatch guard. The skill-backed dispatcher
    asserts isolation against this predicate before launching the agent.
    """
    return play_type in _PR_SCOPED_PLAYS or play_type in _BRANCH_CREATING_PLAYS
