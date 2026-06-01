"""Worktree root location + orphan-TTL sweep.

Regression coverage for the directory-pollution fix: worktrees must live
project-local under ``<repo>/.agentshore/worktrees/`` (never the repo's parent,
which polluted shared workspaces like ``~/Development/``), be redirectable via
``worktrees.root``, and the orphan quarantine must be bounded by a retention
sweep so it can't grow unbounded again.
"""

from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import pytest

from agentshore.agents.worktree import default_worktree_root
from agentshore.agents.worktree.allocator import _quarantine_root
from agentshore.agents.worktree.reaper import sweep_orphans
from agentshore.config._parsers import _parse_worktrees
from agentshore.config.models import WorktreeConfig
from agentshore.errors import ConfigError


def test_default_worktree_root_is_project_local(tmp_path):
    """Default root is <repo>/.agentshore/worktrees — inside the gitignored home."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    root = default_worktree_root(repo)
    assert root == repo.resolve() / ".agentshore" / "worktrees"


def test_default_worktree_root_never_pollutes_repo_parent(tmp_path):
    """The root must NOT be a sibling of the repo (the old polluting layout)."""
    parent = tmp_path / "workspace"
    repo = parent / "myrepo"
    repo.mkdir(parents=True)
    root = default_worktree_root(repo)
    # The bug: root used to be the sibling parent/agentshore-worktrees/<repo>.
    # Now it lives INSIDE the repo, so the old polluting sibling is never used.
    assert repo.resolve() in root.parents
    assert (parent / "agentshore-worktrees") not in root.parents
    assert root.name == "worktrees"
    assert ".agentshore" in root.parts


def test_default_worktree_root_honors_configured_root(tmp_path):
    """worktrees.root centralizes worktrees under <root>/<repo>/worktrees."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    central = tmp_path / "central"
    cfg = SimpleNamespace(worktrees=WorktreeConfig(root=str(central)))
    root = default_worktree_root(repo, cfg)
    assert root == central / "myrepo" / "worktrees"


def test_quarantine_root_tracks_worktree_root(tmp_path):
    """Orphan dir is the ``-orphan`` sibling of the worktree root, same parent."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    root = default_worktree_root(repo)
    assert _quarantine_root(root) == root.with_name("worktrees-orphan")
    assert _quarantine_root(root).parent == root.parent


def test_sweep_orphans_removes_aged_keeps_fresh(tmp_path):
    """Orphans older than the retention window are deleted; fresh ones survive."""
    worktree_root = tmp_path / ".agentshore" / "worktrees"
    worktree_root.mkdir(parents=True)
    orphan_root = _quarantine_root(worktree_root)
    orphan_root.mkdir(parents=True)

    aged = orphan_root / "issue-1-20260101T000000Z"
    aged.mkdir()
    (aged / "file.txt").write_text("uncommitted")
    fresh = orphan_root / "issue-2-now"
    fresh.mkdir()

    # Backdate the aged orphan well past the retention window.
    old = time.time() - 10 * 24 * 3600
    os.utime(aged, (old, old))

    removed = asyncio.run(sweep_orphans(worktree_root, retention_seconds=7 * 24 * 3600))

    assert str(aged) in removed
    assert not aged.exists()
    assert fresh.exists()


def test_sweep_orphans_disabled_when_retention_non_positive(tmp_path):
    """retention_seconds <= 0 keeps orphans forever (sweep is a no-op)."""
    worktree_root = tmp_path / ".agentshore" / "worktrees"
    worktree_root.mkdir(parents=True)
    orphan_root = _quarantine_root(worktree_root)
    orphan_root.mkdir(parents=True)
    aged = orphan_root / "issue-1"
    aged.mkdir()
    old = time.time() - 365 * 24 * 3600
    os.utime(aged, (old, old))

    removed = asyncio.run(sweep_orphans(worktree_root, retention_seconds=0))

    assert removed == []
    assert aged.exists()


def test_parse_worktrees_accepts_root_and_orphan_ttl():
    cfg = _parse_worktrees({"root": "/data/wt", "orphan_retention_seconds": 3600})
    assert cfg.root == "/data/wt"
    assert cfg.orphan_retention_seconds == 3600


def test_parse_worktrees_defaults():
    cfg = _parse_worktrees({})
    assert cfg.root is None
    assert cfg.orphan_retention_seconds == 604800


def test_parse_worktrees_rejects_blank_root():
    with pytest.raises(ConfigError, match="worktrees.root"):
        _parse_worktrees({"root": "   "})


def test_parse_worktrees_rejects_negative_orphan_ttl():
    with pytest.raises(ConfigError, match="orphan_retention_seconds"):
        _parse_worktrees({"orphan_retention_seconds": -1})
