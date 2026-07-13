"""Deterministic reclaim of untracked root-level artifacts left on trunk.

Trunk-scoped skill plays run their agent in the **main checkout**. When an agent
writes a scratch/output file at the repo root and never cleans it up, the file
lingers as untracked (``??``) state that:

- blocks ``merge_pr`` (needs a clean working tree), and
- cannot be remediated by ``reconcile_state`` — an untracked file can't be
  ``git checkout -- ``'d, ``git clean`` is forbidden by the skill, and there is
  no *killed* mutator to attribute it to when the owner is a still-active or
  cleanly-completed play. So reconcile returns ``success: false`` (#162/#164).

This module gives the orchestrator deterministic ground-truth ownership so it
can reclaim that debris safely:

- :func:`snapshot_untracked_root_artifacts` captures the depth-1, non-AgentShore
  untracked files at a point in time. The per-play hook diffs a pre/post pair to
  attribute exactly the files one play introduced.
- :func:`attribute_orphan_artifacts` attributes leftover files to a *closed*
  trunk-scoped play window by mtime (the session-start sweep path — it covers a
  killed play that never reached its post-snapshot, which is #164's actual case).
- :func:`reclaim_artifacts` **moves** (never deletes) attributed files into
  ``.agentshore/reclaimed/<play_id>/`` so trunk goes clean while content stays
  recoverable.
- :func:`reap_quarantine` TTL-reaps the quarantine dir.

All helpers are best-effort and never raise — reclaim must never change a play
outcome or block session start.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from agentshore import command
from agentshore.core.wedge_signals import _path_is_agentshore_owned, parse_porcelain_lines
from agentshore.data.models import ExternalMutationRecord
from agentshore.state import PlayType
from agentshore.utils import now_iso

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from agentshore.data.store import DataStore

_logger = structlog.get_logger(__name__)

#: Subdirectory under ``.agentshore/`` where reclaimed artifacts are quarantined.
#: ``.agentshore/`` is already an AgentShore-owned untracked prefix, so the
#: quarantine never re-flags as dirty trunk.
QUARANTINE_DIRNAME = "reclaimed"

#: Plays that dispatch their agent into the main checkout (``TrunkAllocation``)
#: rather than an isolated worktree — the only plays that can leave untracked
#: root artifacts on trunk. Single source of truth: the per-play reclaim hook,
#: the session-start sweep, and the ``agentshore-reconcile-state`` skill prose
#: all reference this same set.
TRUNK_SCOPED_PLAY_TYPES: frozenset[PlayType] = frozenset(
    {
        PlayType.CLEANUP,
        PlayType.MERGE_PR,
        PlayType.DESIGN_AUDIT,
        PlayType.GROOM_BACKLOG,
        PlayType.RUN_QA,
        PlayType.WRITE_IMPLEMENTATION_PLAN,
        PlayType.REFINE_TASK_BREAKDOWN,
        PlayType.SEED_PROJECT,
        PlayType.CALIBRATE_ALIGNMENT,
    }
)


@dataclass(frozen=True, slots=True)
class PlayWindow:
    """A play's execution window in epoch seconds.

    ``ended_at`` is ``None`` for a play that never recorded completion (still
    running, or killed mid-flight) — treated as an open-ended ``[started, +inf)``
    window by :func:`attribute_orphan_artifacts`.
    """

    play_id: int
    started_at: float
    ended_at: float | None


def snapshot_untracked_root_artifacts(project_path: Path) -> set[str]:
    """Return depth-1 untracked files at the repo root, excluding AgentShore state.

    An *untracked root artifact* is a porcelain ``??`` entry whose path has no
    ``/`` (a file directly in the repo root) and is not one of the
    AgentShore/beads runtime sidecars (see
    :func:`agentshore.core.wedge_signals._path_is_agentshore_owned`).
    Subdirectories and tracked files are never included, so reclaim can only
    ever touch top-level scratch files. Quoted porcelain paths (names with
    spaces / specials) are skipped rather than risk an unescape mismatch.

    Errors return an empty set — a missing snapshot simply means nothing is
    reclaimed.
    """
    result = command.git_sync(
        "status",
        "--porcelain",
        "--untracked-files=all",
        cwd=project_path,
        timeout_seconds=10.0,
    )
    if result.tool_missing or result.returncode != 0:
        return set()
    artifacts: set[str] = set()
    for status, path in parse_porcelain_lines(result.stdout):
        # Only depth-1 untracked files that AgentShore doesn't own. (Quoted /
        # blank paths are already dropped by the shared parser.)
        if status != "??" or "/" in path or _path_is_agentshore_owned(path):
            continue
        artifacts.add(path)
    return artifacts


def attribute_orphan_artifacts(
    project_path: Path,
    *,
    owner_windows: Sequence[PlayWindow],
    active_windows: Sequence[PlayWindow],
) -> dict[str, int]:
    """Map each untracked root artifact to the closed trunk play that owns it.

    A file is attributed to the ``owner_windows`` entry with the **latest**
    ``started_at`` that still brackets the file's mtime (``started_at <= mtime``
    and, when ``ended_at`` is set, ``mtime <= ended_at``). A file bracketed by
    **any** ``active_windows`` entry is left unattributed — it may be in-flight
    work of a still-running play, so it must not be reclaimed. Files older than
    every owner window (e.g. pre-session user WIP) are also left out.

    Returns ``{root_relative_path: owner_play_id}``. Best-effort; never raises.
    """
    attributed: dict[str, int] = {}
    for rel in snapshot_untracked_root_artifacts(project_path):
        try:
            mtime = (project_path / rel).stat().st_mtime
        except OSError:
            continue
        if _window_brackets_any(mtime, active_windows):
            continue
        owner = _latest_owner(mtime, owner_windows)
        if owner is not None:
            attributed[rel] = owner
    return attributed


def _window_brackets_any(mtime: float, windows: Iterable[PlayWindow]) -> bool:
    """True if *mtime* falls inside any window (open-ended when ``ended_at`` is None)."""
    return any(
        w.started_at <= mtime and (w.ended_at is None or mtime <= w.ended_at) for w in windows
    )


def _latest_owner(mtime: float, windows: Iterable[PlayWindow]) -> int | None:
    """Return the play_id of the latest-starting window that brackets *mtime*."""
    best: PlayWindow | None = None
    for w in windows:
        brackets = w.started_at <= mtime and (w.ended_at is None or mtime <= w.ended_at)
        if brackets and (best is None or w.started_at > best.started_at):
            best = w
    return best.play_id if best is not None else None


def reclaim_artifacts(project_path: Path, paths: Iterable[str], *, play_id: int) -> list[str]:
    """Move attributed untracked root files into the quarantine dir. Best-effort.

    Each file is *moved* (``os.replace``, same filesystem) into
    ``.agentshore/reclaimed/<play_id>/`` so trunk goes clean while content stays
    recoverable. A file that vanished, became a directory, or is no longer a
    plain file is skipped. Returns the root-relative paths actually moved; never
    raises.
    """
    moved: list[str] = []
    rels = sorted(paths)
    if not rels:
        return moved
    quarantine = project_path / ".agentshore" / QUARANTINE_DIRNAME / str(play_id)
    for rel in rels:
        src = project_path / rel
        try:
            if not src.is_file():
                continue
            quarantine.mkdir(parents=True, exist_ok=True)
            os.replace(src, quarantine / rel)
            moved.append(rel)
        except OSError as exc:
            _logger.warning(
                "trunk_artifact_reclaim_failed", path=rel, play_id=play_id, error=str(exc)
            )
    return moved


async def sweep_and_reclaim_orphans(
    project_path: Path,
    *,
    store: DataStore,
    session_id: str,
    owner_windows: Sequence[PlayWindow],
    active_windows: Sequence[PlayWindow],
    status: str,
) -> int:
    """Attribute, reclaim, and record orphaned untracked root artifacts.

    Shared core used by both the session-start sweep (``active_windows=[]``,
    since dispatch hasn't opened yet) and the mid-session ``reconcile_state``
    sweep (``active_windows`` built from live in-flight trunk-scoped plays, so
    their in-progress work is never clobbered). ``status`` distinguishes the
    caller in the recorded ``ExternalMutationRecord`` rows (e.g.
    ``"reclaimed_sweep"`` vs ``"reclaimed_reconcile"``).

    Returns the number of files reclaimed. Best-effort like every other
    function in this module — never raises.
    """
    attributed = attribute_orphan_artifacts(
        project_path, owner_windows=owner_windows, active_windows=active_windows
    )
    by_owner: dict[int, list[str]] = {}
    for rel, owner in attributed.items():
        by_owner.setdefault(owner, []).append(rel)
    reclaimed_total = 0
    for owner, rels in by_owner.items():
        moved = reclaim_artifacts(project_path, rels, play_id=owner)
        reclaimed_total += len(moved)
        for rel in moved:
            await store.record_external_mutation(
                ExternalMutationRecord(
                    session_id=session_id,
                    play_id=owner,
                    idempotency_key=f"reclaim:{owner}:{rel}",
                    mutation_type="trunk_artifact_reclaim",
                    target=rel,
                    status=status,
                    created_at=now_iso(),
                )
            )
    return reclaimed_total


def reap_quarantine(project_path: Path, *, ttl_seconds: int) -> int:
    """Delete quarantine subdirs whose mtime is older than *ttl_seconds*.

    Returns the number of ``<play_id>`` subdirs removed. Best-effort; never
    raises. A non-positive TTL reaps nothing (disabled).
    """
    if ttl_seconds <= 0:
        return 0
    root = project_path / ".agentshore" / QUARANTINE_DIRNAME
    if not root.is_dir():
        return 0
    cutoff = time.time() - ttl_seconds
    removed = 0
    try:
        children = list(root.iterdir())
    except OSError:
        return 0
    for child in children:
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed
