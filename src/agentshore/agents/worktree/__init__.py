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
    TRUNK_MUTATING_PLAYS,
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

    from agentshore.config.models import RuntimeConfig


def default_worktree_root(repo_root: Path, cfg: RuntimeConfig | None = None) -> Path:
    """Canonical worktree root for a given project.

    Default (``cfg.worktrees.root`` unset): worktrees live project-local under
    ``<repo>/.agentshore/worktrees/`` — inside the gitignored AgentShore home,
    on the same filesystem as the repo, and crucially NOT in the repo's parent
    directory (the old ``<repo>/../agentshore-worktrees/`` layout polluted any
    shared workspace that held repos directly, e.g. ``~/Development/``).

    When ``cfg.worktrees.root`` is set, worktrees are centralized under
    ``<root>/<repo-name>/worktrees/`` (per-repo subdir disambiguates names).
    """
    from agentshore.paths import project_dir

    repo_root = repo_root.resolve()
    configured = getattr(getattr(cfg, "worktrees", None), "root", None)
    if configured:
        from pathlib import Path as _Path

        return _Path(configured).expanduser() / repo_root.name / "worktrees"
    return project_dir(repo_root) / "worktrees"


__all__ = [
    "AllocateResult",
    "OrphanRecord",
    "ReapReport",
    "ReconcileReport",
    "TRUNK_MUTATING_PLAYS",
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
