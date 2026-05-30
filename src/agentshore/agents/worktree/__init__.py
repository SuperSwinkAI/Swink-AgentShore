"""AgentShore-managed git worktree lifecycle.

AgentShore owns one worktree per PR branch (lazy-created on first touch) and
fresh worktrees for branch-creating plays (Issue Pickup, Cleanup) that get
re-keyed by branch name after the play succeeds. Trunk-scoped plays use a
``TrunkAllocation`` sentinel pointing at the main checkout.

Public surface:

- ``WorktreeManager``         - orchestrator owned by ``AgentManager``
- ``WorktreeAllocation``      - per-PR / per-prebranch result
- ``TrunkAllocation``         - sentinel for trunk-scoped plays
- ``AllocateResult``          - raw allocator output (path / created / fetched)
- ``WorktreeStatus``          - row status literal
- ``WorktreeAllocationFailed``       - ``git worktree add`` (or similar) failed
- ``WorktreeAllocationConflict``     - concurrent allocate hit the unique index
- ``WorktreeBranchGone``             - upstream branch was deleted
- ``OrphanRecord`` / ``ReapReport``  - reaper outputs
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.agents.worktree.allocator import (
    AllocateResult,
    ReconcileReport,
    WorktreeAllocationFailed,
    WorktreeBranchGone,
    ensure_worktree,
    quarantine_orphan,
    reconcile_worktrees,
    remove_worktree,
)
from agentshore.agents.worktree.manager import (
    TRUNK_SCOPED_PLAYS,
    TrunkAllocation,
    WorktreeAllocation,
    WorktreeManager,
)
from agentshore.agents.worktree.reaper import OrphanRecord, ReapReport
from agentshore.agents.worktree.registry import (
    WorktreeAllocationConflict,
    WorktreeRow,
    WorktreeStatus,
)

if TYPE_CHECKING:
    from pathlib import Path


def default_worktree_root(repo_root: Path) -> Path:
    """Canonical worktree root for a given project.

    Worktrees live alongside the main checkout in a sibling directory so a
    ``git worktree add`` never escapes the parent filesystem and the path
    layout matches the plan's ``<repo>/../agentshore-worktrees/<project>/``
    convention.
    """
    repo_root = repo_root.resolve()
    return repo_root.parent / "agentshore-worktrees" / repo_root.name


__all__ = [
    "AllocateResult",
    "OrphanRecord",
    "ReapReport",
    "ReconcileReport",
    "TRUNK_SCOPED_PLAYS",
    "TrunkAllocation",
    "WorktreeAllocation",
    "WorktreeAllocationConflict",
    "WorktreeAllocationFailed",
    "WorktreeBranchGone",
    "WorktreeManager",
    "WorktreeRow",
    "WorktreeStatus",
    "default_worktree_root",
    "ensure_worktree",
    "quarantine_orphan",
    "reconcile_worktrees",
    "remove_worktree",
]
