"""Tests for the #203 dup-path alias fix.

Root cause: the ``pickup-<N>`` directory is reused across attempts, so many
distinct ``worktree_id`` rows can share one on-disk path. In-flight protection
was ``worktree_id``-keyed while the reaper removes by ``worktree_path``, so the
closed-PR TTL reaper would reap a stale OLD-id row at a path and
``git worktree remove --force`` the directory a LIVE new-id row was using.

Covers:

- A stale OLD-id row sharing a LIVE in-flight path is NOT reaped (id differs,
  path protected) — both the manager wrapper and the bare reaper function.
- An unprotected stale row IS still reaped.
- Per-attempt allocation keys produce distinct on-disk paths once the canonical
  path is held by a live row (prebranch-key reuse preserved otherwise).
- ``_canon_path`` matching is separator/case-correct.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentshore.agents.worktree.reaper import (
    _canon_path,
    reap_for_closed_prs,
)
from agentshore.agents.worktree.registry import (
    insert_worktree,
    lookup_by_id,
)
from agentshore.data.store import DataStore


def _git(*args: str, cwd: Path | None = None) -> str:
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "AgentShore Test")
    env.setdefault("GIT_AUTHOR_EMAIL", "test@agentshore.example")
    env.setdefault("GIT_COMMITTER_NAME", "AgentShore Test")
    env.setdefault("GIT_COMMITTER_EMAIL", "test@agentshore.example")
    env.setdefault("GIT_CONFIG_GLOBAL", "/dev/null")
    env.setdefault("GIT_CONFIG_SYSTEM", "/dev/null")
    return subprocess.check_output(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        text=True,
        stderr=subprocess.STDOUT,
    )


async def _insert_row_at(
    store: DataStore,
    *,
    session_id: str,
    pre_branch_key: str,
    worktree_path: str,
    status: str,
    last_used_at: str | None = None,
) -> int:
    """Insert a worktree row pointing at ``worktree_path`` (no git side effect).

    Lets a test seed two DB rows that *share* one on-disk path — the alias
    class behind #203 — which ``_seed_worktree_row`` can't, since it runs
    ``git worktree add`` per call.
    """
    row = await insert_worktree(
        store,
        session_id=session_id,
        branch_name=None,
        pre_branch_key=pre_branch_key,
        worktree_path=worktree_path,
        original_play_type="issue_pickup",
        base_ref="origin/HEAD",
        head_sha=None,
        status=status,  # type: ignore[arg-type]
    )
    if last_used_at is not None:
        await store._conn.execute(
            "UPDATE worktrees SET last_used_at = ? WHERE worktree_id = ?",
            (last_used_at, row.worktree_id),
        )
        await store._conn.commit()
    return row.worktree_id


# --- bare reaper: path-aware protection --------------------------------------


async def test_reap_closed_prs_skips_stale_row_sharing_live_path(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """A stale OLD-id row whose path matches a LIVE path is NOT reaped (#203).

    The live row has a *different* id (not in any id-protected set), so only the
    path-keyed guard can save it. The on-disk ``pickup-7`` directory must
    survive because a live new-id row is mid-play in it.
    """
    target = worktree_root / "pickup-7"
    _git("worktree", "add", "-b", "pickup-7-wt", str(target), "HEAD", cwd=main_repo)

    old_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    # Stale OLD-id row at the shared path (a prior attempt's row).
    stale_old_id = await _insert_row_at(
        store,
        session_id="sess-1",
        pre_branch_key="pickup-7-old",
        worktree_path=str(target),
        status="stale",
        last_used_at=old_ts,
    )

    # The LIVE row uses the same directory under a different id — protected via
    # its canonical path, NOT its id.
    report = await reap_for_closed_prs(
        store,
        session_id="sess-1",
        main_repo=main_repo,
        ttl_seconds=3600,
        protected_paths={_canon_path(target)},
    )

    assert report.total == 0
    row = await lookup_by_id(store, worktree_id=stale_old_id)
    assert row is not None and row.status == "stale"
    assert target.exists(), "live in-flight directory must not be removed"


async def test_reap_closed_prs_reaps_unprotected_stale_row(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """An unprotected stale row IS reaped while a path-protected one is held."""
    protected = worktree_root / "pickup-7"
    reapable = worktree_root / "pickup-9"
    _git("worktree", "add", "-b", "pickup-7-wt", str(protected), "HEAD", cwd=main_repo)
    _git("worktree", "add", "-b", "pickup-9-wt", str(reapable), "HEAD", cwd=main_repo)

    old_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    protected_id = await _insert_row_at(
        store,
        session_id="sess-1",
        pre_branch_key="pickup-7-old",
        worktree_path=str(protected),
        status="stale",
        last_used_at=old_ts,
    )
    reapable_id = await _insert_row_at(
        store,
        session_id="sess-1",
        pre_branch_key="pickup-9",
        worktree_path=str(reapable),
        status="stale",
        last_used_at=old_ts,
    )

    report = await reap_for_closed_prs(
        store,
        session_id="sess-1",
        main_repo=main_repo,
        ttl_seconds=3600,
        protected_paths={_canon_path(protected)},
    )

    assert report.total == 1
    assert report.removed[0].worktree_id == reapable_id
    assert not reapable.exists()

    protected_row = await lookup_by_id(store, worktree_id=protected_id)
    assert protected_row is not None and protected_row.status == "stale"
    assert protected.exists()


# --- manager wrapper: id differs, path protected -----------------------------


async def test_manager_reap_closed_prs_skips_dup_path_alias(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """Manager-level: stale OLD-id row sharing a LIVE path is held back (#203).

    ``protected_ids`` does NOT contain the stale row's id (it's an old attempt),
    yet ``protected_paths`` does — the manager must skip it anyway.
    """
    from agentshore.agents.worktree import WorktreeManager
    from agentshore.agents.worktree.manager import WorktreeAllocation
    from agentshore.config import RuntimeConfig
    from agentshore.state import PlayType

    target = worktree_root / "pickup-7"
    _git("worktree", "add", "-b", "pickup-7-wt", str(target), "HEAD", cwd=main_repo)

    old_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    stale_old_id = await _insert_row_at(
        store,
        session_id="sess-1",
        pre_branch_key="pickup-7-old",
        worktree_path=str(target),
        status="stale",
        last_used_at=old_ts,
    )

    wm = WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=RuntimeConfig(),
    )
    # A LIVE new-id dispatch (id 9999) shares this path; registering it protects
    # the stale old-id row at the same path by its canonical path alone (#203).
    wm.register_dispatch(
        WorktreeAllocation(
            worktree_id=9999,
            path=target,
            branch_name=None,
            pre_branch_key="pickup-7-live",
            play_type=PlayType.ISSUE_PICKUP,
            scope="branch_creating",
        )
    )
    report = await wm.reap_closed_prs(ttl_seconds=3600)

    assert report.total == 0
    row = await lookup_by_id(store, worktree_id=stale_old_id)
    assert row is not None and row.status == "stale"
    assert target.exists()


async def test_manager_reap_closed_prs_skips_stale_alias_of_active_db_row(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """Stale alias of a LIVE *DB* row is held back even with an empty registry (#243).

    This is the production failure that the ``register_dispatch`` path-guard
    missed: the in-flight registry is empty (no ``register_dispatch`` call),
    yet an ``active`` row holds the canonical ``pickup-<N>`` path while a
    ``stale`` old-id row aliases the same directory. The reaper removes by
    path, so reaping the stale row force-removes the live checkout. The
    DB-truth ``_live_alias_paths`` defense must skip it from the active row
    alone — independent of the in-memory ``_inflight`` set.
    """
    from agentshore.agents.worktree import WorktreeManager
    from agentshore.config import RuntimeConfig

    target = worktree_root / "pickup-82"
    _git("worktree", "add", "-b", "pickup-82-wt", str(target), "HEAD", cwd=main_repo)

    old_ts = (datetime.now(UTC) - timedelta(hours=4)).isoformat()
    # Stale OLD-id row at the shared path (a prior attempt whose PR closed).
    stale_old_id = await _insert_row_at(
        store,
        session_id="sess-1",
        pre_branch_key="pickup-82-old",
        worktree_path=str(target),
        status="stale",
        last_used_at=old_ts,
    )
    # LIVE new-id row sharing the same directory — active, but NOT registered
    # in the in-flight registry (the exact #243 gap).
    await _insert_row_at(
        store,
        session_id="sess-1",
        pre_branch_key="pickup-82-live",
        worktree_path=str(target),
        status="active",
    )

    wm = WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=RuntimeConfig(),
    )
    # No register_dispatch — _inflight is empty; protection must come from DB truth.
    assert wm._protected_paths() == set()

    report = await wm.reap_closed_prs(ttl_seconds=3600)

    assert report.total == 0
    row = await lookup_by_id(store, worktree_id=stale_old_id)
    assert row is not None and row.status == "stale"
    assert target.exists(), "live active directory must not be removed"


# --- collision force-remove: live-DB-row protection (#250) -------------------


async def test_collision_predicate_protects_live_db_row_without_registry(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """The collision force-remove guard protects a live worktree from DB truth alone (#250).

    The #238 guard checked only the in-memory ``_inflight`` registry, which can
    miss a live dispatch (#243). The #250 recurrence force-removed a *running*
    ``pickup-<N>`` checkout whose path was absent from ``_inflight`` during a
    claim-lost repick branch collision — yielding "worktree reclaimed mid-play".
    The predicate must refuse to reclaim any path backing a live ``active`` /
    ``reaping`` DB row, independent of registry state.
    """
    from agentshore.agents.worktree import WorktreeManager
    from agentshore.config import RuntimeConfig

    target = worktree_root / "pickup-1264"
    _git("worktree", "add", "-b", "pickup-1264-wt", str(target), "HEAD", cwd=main_repo)

    # LIVE active row at the canonical path — but NOT registered in the in-flight
    # registry (the exact #243 gap that left #238 defeatable).
    await _insert_row_at(
        store,
        session_id="sess-1",
        pre_branch_key="pickup-1264-live",
        worktree_path=str(target),
        status="active",
    )

    wm = WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=RuntimeConfig(),
    )
    assert wm._protected_paths() == set(), "in-memory registry must be empty for this case"

    predicate = wm._build_reclaimable_collision_predicate(await wm._live_alias_paths())

    # ``pickup-1264`` matches the reclaimable name prefix, but it backs a live
    # DB row → must NOT be force-removable.
    assert predicate(target) is False


async def test_collision_predicate_allows_genuine_orphan(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """A crashed-session ``pickup-`` orphan with no live row is still reclaimable.

    The DB-truth backstop must not over-protect: a path with neither a live row
    nor a registry entry is a genuine orphan and stays force-removable so the
    collision retry can clear it.
    """
    from agentshore.agents.worktree import WorktreeManager
    from agentshore.config import RuntimeConfig

    wm = WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=RuntimeConfig(),
    )
    orphan = worktree_root / "pickup-555"  # no DB row, no registry entry

    predicate = wm._build_reclaimable_collision_predicate(await wm._live_alias_paths())

    assert predicate(orphan) is True


async def test_collision_predicate_protects_in_flight_registry_path(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """The in-memory ``_inflight`` protection still holds (the #238 path)."""
    from agentshore.agents.worktree import WorktreeManager
    from agentshore.agents.worktree.manager import WorktreeAllocation
    from agentshore.config import RuntimeConfig
    from agentshore.state import PlayType

    target = worktree_root / "pickup-77"

    wm = WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=RuntimeConfig(),
    )
    wm.register_dispatch(
        WorktreeAllocation(
            worktree_id=4242,
            path=target,
            branch_name=None,
            pre_branch_key="pickup-77",
            play_type=PlayType.ISSUE_PICKUP,
            scope="branch_creating",
        )
    )

    # No DB row this time — protection comes solely from the in-flight registry.
    predicate = wm._build_reclaimable_collision_predicate(await wm._live_alias_paths())

    assert predicate(target) is False


# --- per-attempt unique paths ------------------------------------------------


async def test_per_attempt_keys_produce_distinct_paths(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """A second pickup allocation at an occupied canonical path gets a unique dir.

    The first ISSUE_PICKUP allocation for issue 7 lands at ``pickup-7``. A live
    row holds it; a second fresh allocation (e.g. the old row never rekeyed) is
    routed to a unique ``pickup-7-<shortid>`` directory rather than aliasing the
    live one — killing the dup-path alias class at the source.
    """
    from agentshore.agents.worktree import WorktreeManager
    from agentshore.config import RuntimeConfig
    from agentshore.plays.base import PlayParams
    from agentshore.state import PlayType

    wm = WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=RuntimeConfig(),
    )

    # First allocation — canonical pickup-7 path.
    alloc1 = await wm.allocate_for_dispatch(
        play_type=PlayType.ISSUE_PICKUP,
        params=PlayParams(issue_number=7),
    )
    assert alloc1.path.name == "pickup-7"  # type: ignore[union-attr]

    # Force the prebranch-key reuse lookup to MISS so a fresh insert runs while
    # the live row still holds the canonical path: rekey-away the pre_branch_key
    # so ``lookup_by_prebranch_key`` returns None, but the row stays active at
    # the pickup-7 path (the exact OLD/NEW alias hazard).
    await store._conn.execute(
        "UPDATE worktrees SET pre_branch_key = 'pickup-7-resolved' WHERE worktree_id = ?",
        (alloc1.worktree_id,),  # type: ignore[union-attr]
    )
    await store._conn.commit()

    alloc2 = await wm.allocate_for_dispatch(
        play_type=PlayType.ISSUE_PICKUP,
        params=PlayParams(issue_number=7),
    )

    assert alloc2.path != alloc1.path  # type: ignore[union-attr]
    assert alloc2.path.name.startswith("pickup-7-")  # type: ignore[union-attr]
    assert _canon_path(alloc2.path) != _canon_path(alloc1.path)  # type: ignore[union-attr]


async def test_prebranch_key_reuse_preserved(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """A repeat pickup for the same issue REUSES the existing row (resumability).

    The uniquification must only kick in on a fresh insert against an occupied
    path; the normal same-key, same-attempt path must still share one worktree.
    """
    from agentshore.agents.worktree import WorktreeManager
    from agentshore.config import RuntimeConfig
    from agentshore.plays.base import PlayParams
    from agentshore.state import PlayType

    wm = WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=RuntimeConfig(),
    )
    params = PlayParams(issue_number=7)
    alloc1 = await wm.allocate_for_dispatch(play_type=PlayType.ISSUE_PICKUP, params=params)
    alloc2 = await wm.allocate_for_dispatch(play_type=PlayType.ISSUE_PICKUP, params=params)

    assert alloc1.worktree_id == alloc2.worktree_id  # type: ignore[union-attr]
    assert alloc1.path == alloc2.path  # type: ignore[union-attr]


# --- live_protected_rows / live_protected_paths (#311) ----------------------


async def test_live_protected_rows_unions_inflight_and_active_db_rows(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """Public accessor unions the in-memory registry with active/reaping DB rows.

    ``live_protected_rows``/``live_protected_paths`` are the public counterparts
    to the private ``_protected_paths``/``_live_alias_paths`` the reaper already
    trusts (#189/#203/#243/#250) — added so callers OUTSIDE the manager (the
    PRUNE/RECONCILE_STATE skill context injection and its post-hoc guard) can
    get the same hardened truth without reaching into private internals (#311).
    """
    from agentshore.agents.worktree import WorktreeManager
    from agentshore.agents.worktree.manager import WorktreeAllocation
    from agentshore.config import RuntimeConfig
    from agentshore.state import PlayType

    inflight_only = worktree_root / "pickup-inflight-only"
    db_active_only = worktree_root / "pickup-db-active-only"
    _git("worktree", "add", "-b", "inflight-only-wt", str(inflight_only), "HEAD", cwd=main_repo)
    _git("worktree", "add", "-b", "db-active-only-wt", str(db_active_only), "HEAD", cwd=main_repo)

    # A row backing the DB-active worktree, but never registered in the
    # in-memory registry (the #243-style gap).
    db_row_id = await _insert_row_at(
        store,
        session_id="sess-1",
        pre_branch_key="db-active-only",
        worktree_path=str(db_active_only),
        status="active",
    )

    wm = WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=RuntimeConfig(),
    )
    # An in-flight dispatch with no DB row (or a DB row not yet reflecting it).
    wm.register_dispatch(
        WorktreeAllocation(
            worktree_id=4242,
            path=inflight_only,
            branch_name=None,
            pre_branch_key="pickup-inflight-only",
            play_type=PlayType.ISSUE_PICKUP,
            scope="branch_creating",
        )
    )

    rows = await wm.live_protected_rows()
    assert rows[4242] == _canon_path(inflight_only)
    assert rows[db_row_id] == _canon_path(db_active_only)

    paths = await wm.live_protected_paths()
    assert paths == {_canon_path(inflight_only), _canon_path(db_active_only)}


async def test_live_protected_rows_includes_reaping_status(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """A ``status='reaping'`` row counts as live — the exact #311 gap.

    ``collect_active_worktree_paths`` (the narrower helper that fed the
    prune/reconcile skills' ``active_worktree_paths`` context before this fix)
    only queries ``status = 'active'``, so a worktree mid-transition to
    ``'reaping'`` was invisible to it even though the manager's own
    ``list_active``-backed truth (and the reaper) treats ``'reaping'`` as live.
    """
    from agentshore.agents.worktree import WorktreeManager
    from agentshore.config import RuntimeConfig

    reaping_target = worktree_root / "pickup-reaping"
    _git("worktree", "add", "-b", "reaping-wt", str(reaping_target), "HEAD", cwd=main_repo)

    await _insert_row_at(
        store,
        session_id="sess-1",
        pre_branch_key="pickup-reaping",
        worktree_path=str(reaping_target),
        status="reaping",
    )

    wm = WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=RuntimeConfig(),
    )

    paths = await wm.live_protected_paths()
    assert _canon_path(reaping_target) in paths


# --- reconcile_vanished_protected_rows (#360) --------------------------------


def _manager(store: DataStore, main_repo: Path, worktree_root: Path):  # type: ignore[no-untyped-def]
    from agentshore.agents.worktree import WorktreeManager
    from agentshore.config import RuntimeConfig

    return WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=RuntimeConfig(),
    )


async def test_vanished_row_for_finished_work_is_retired_not_flagged(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """#360: a stale row whose work is already done is bookkeeping, not a clobber.

    The issue-syncer only marks a worktree row ``stale`` when it re-fetches that
    exact PR in this session, so an already-merged worktree keeps an ``active``
    row indefinitely. A prune that removes its directory used to be reported as
    a destructive sweep (ERROR + forced play failure). With no in-flight
    dispatch and no active work claim behind it, the row must instead be retired
    to ``reaped`` and reported as bookkeeping.
    """
    gone = worktree_root / "agentshore-101-already-merged"  # never created on disk
    row_id = await _insert_row_at(
        store,
        session_id="sess-1",
        pre_branch_key="pickup-101",
        worktree_path=str(gone),
        status="active",
    )

    wm = _manager(store, main_repo, worktree_root)
    report = await wm.reconcile_vanished_protected_rows()

    assert report.in_flight == {}
    assert report.retired == {row_id: _canon_path(gone)}
    assert report.reasons[row_id] == "no_active_dispatch"

    retired_row = await lookup_by_id(store, worktree_id=row_id)
    assert retired_row is not None
    assert retired_row.status == "reaped"
    # And the row no longer counts as protected, so the next prune is quiet too.
    assert _canon_path(gone) not in await wm.live_protected_paths()
    assert (await wm.reconcile_vanished_protected_rows()).retired == {}


async def test_vanished_inflight_dispatch_is_still_flagged(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """#360 must not weaken #189/#195/#203/#238/#243/#250: live work still trips."""
    from agentshore.agents.worktree.manager import WorktreeAllocation
    from agentshore.state import PlayType

    gone = worktree_root / "pickup-42"  # registered in-flight, directory removed
    row_id = await _insert_row_at(
        store,
        session_id="sess-1",
        pre_branch_key="pickup-42",
        worktree_path=str(gone),
        status="active",
    )

    wm = _manager(store, main_repo, worktree_root)
    wm.register_dispatch(
        WorktreeAllocation(
            worktree_id=row_id,
            path=gone,
            branch_name=None,
            pre_branch_key="pickup-42",
            play_type=PlayType.ISSUE_PICKUP,
            scope="branch_creating",
        )
    )

    report = await wm.reconcile_vanished_protected_rows()

    assert report.in_flight == {row_id: _canon_path(gone)}
    assert report.retired == {}
    assert report.reasons[row_id] == "inflight_dispatch"
    # The live row is left exactly as it was — nothing retired out from under it.
    live_row = await lookup_by_id(store, worktree_id=row_id)
    assert live_row is not None
    assert live_row.status == "active"


async def test_vanished_row_with_active_work_claim_is_flagged(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """The #243 registry gap: a live dispatch missing from ``_inflight`` still trips.

    ``register_dispatch`` can fail to land (or be cleared early), which is what
    defeated the earlier in-flight-only guards. An active work claim on the
    row's issue is DB-truth evidence that a play is still working this worktree,
    so the vanished directory is a genuine clobber even with an empty registry.
    """
    gone = worktree_root / "pickup-77"
    row_id = await _insert_row_at(
        store,
        session_id="sess-1",
        pre_branch_key="pickup-77",
        worktree_path=str(gone),
        status="active",
    )
    claim_group = await store.acquire_work_claims(
        "sess-1", "issue_pickup", ["issue:77"], status="running"
    )
    assert claim_group is not None

    wm = _manager(store, main_repo, worktree_root)
    report = await wm.reconcile_vanished_protected_rows()

    assert report.in_flight == {row_id: _canon_path(gone)}
    assert report.retired == {}
    assert report.reasons[row_id] == "active_work_claim:issue:77"
    still_live = await lookup_by_id(store, worktree_id=row_id)
    assert still_live is not None
    assert still_live.status == "active"


async def test_vanished_reconcile_is_noop_when_every_directory_survives(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """Nothing missing on disk → nothing classified, nothing retired."""
    present = worktree_root / "pickup-present"
    _git("worktree", "add", "-b", "present-wt", str(present), "HEAD", cwd=main_repo)
    row_id = await _insert_row_at(
        store,
        session_id="sess-1",
        pre_branch_key="pickup-present",
        worktree_path=str(present),
        status="active",
    )

    wm = _manager(store, main_repo, worktree_root)
    report = await wm.reconcile_vanished_protected_rows()

    assert report.in_flight == {}
    assert report.retired == {}
    row = await lookup_by_id(store, worktree_id=row_id)
    assert row is not None
    assert row.status == "active"


# --- _canon_path correctness -------------------------------------------------


def test_canon_path_separator_and_case() -> None:
    """``_canon_path`` folds separators (and case on case-insensitive FS)."""
    # Forward vs native separators collapse to the same key.
    a = _canon_path("/tmp/agentshore-worktrees/pickup-7")
    b = _canon_path(Path("/tmp/agentshore-worktrees/pickup-7"))
    assert a == b

    # normpath collapses redundant components.
    assert _canon_path("/tmp/wt/./pickup-7") == _canon_path("/tmp/wt/pickup-7")
    assert _canon_path("/tmp/wt/sub/../pickup-7") == _canon_path("/tmp/wt/pickup-7")

    # On a case-insensitive filesystem (normcase lowercases), the two fold
    # together; on a case-sensitive one they don't. Assert the platform-correct
    # behaviour rather than a fixed answer.
    upper = _canon_path("/tmp/WT/Pickup-7")
    lower = _canon_path("/tmp/wt/pickup-7")
    if os.path.normcase("A") == "a":
        assert upper == lower
    else:
        assert upper != lower
