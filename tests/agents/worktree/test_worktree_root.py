"""Worktree root location + legacy orphan-dir cleanup.

Regression coverage for the directory-pollution fix: worktrees must live
project-local under ``<repo>/.agentshore/worktrees/`` (never the repo's parent,
which polluted shared workspaces like ``~/Development/``) and be redirectable via
``worktrees.root``. Orphans are now deleted in reconcile (not quarantined), and a
pre-existing ``<root>-orphan`` quarantine dir is removed on session start.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentshore.agents.worktree import default_worktree_root
from agentshore.agents.worktree.manager import WorktreeManager
from agentshore.config import load_config
from agentshore.config._parsers import _parse_worktrees
from agentshore.config.models import WorktreeConfig
from agentshore.data.store import DataStore
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


# --- config parsing -----------------------------------------------------------


def test_parse_worktrees_accepts_root_and_ttl():
    cfg = _parse_worktrees({"root": "/data/wt", "reap_ttl_seconds": 1800})
    assert cfg.root == "/data/wt"
    assert cfg.reap_ttl_seconds == 1800


def test_parse_worktrees_defaults():
    cfg = _parse_worktrees({})
    assert cfg.root is None
    assert cfg.reap_ttl_seconds == 10800


def test_parse_worktrees_rejects_blank_root():
    with pytest.raises(ConfigError, match="worktrees.root"):
        _parse_worktrees({"root": "   "})


def test_parse_worktrees_rejects_negative_reap_ttl():
    with pytest.raises(ConfigError, match="reap_ttl_seconds"):
        _parse_worktrees({"reap_ttl_seconds": -1})


def test_parse_worktrees_ignores_removed_orphan_retention_key():
    """A stale ``orphan_retention_seconds`` key from an old config is ignored, not an error."""
    cfg = _parse_worktrees({"orphan_retention_seconds": 604800})  # type: ignore[typeddict-unknown-key]
    assert cfg.root is None


# --- legacy quarantine-dir cleanup -------------------------------------------


async def test_reap_session_start_removes_legacy_orphan_dir(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """A pre-existing ``<root>-orphan`` quarantine dir is deleted on session start."""
    worktree_root.mkdir(parents=True, exist_ok=True)
    legacy = worktree_root.with_name(worktree_root.name + "-orphan")
    aged = legacy / "agentshore-194-stale-20260101T000000Z"
    aged.mkdir(parents=True)
    (aged / "target").mkdir()  # rebuildable build-cache stand-in
    (aged / "leftover.txt").write_text("old quarantined work\n")

    manager = WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=load_config(None),
    )
    await manager.reap_session_start()

    assert not legacy.exists(), "legacy quarantine dir should be removed on session start"
