"""Tests for ``strip_non_origin_remotes`` in the reaper."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentshore.agents.worktree.reaper import strip_non_origin_remotes


def _git(*args: str, cwd: Path) -> str:
    import os

    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "AgentShore Test")
    env.setdefault("GIT_AUTHOR_EMAIL", "test@agentshore.example")
    env.setdefault("GIT_COMMITTER_NAME", "AgentShore Test")
    env.setdefault("GIT_COMMITTER_EMAIL", "test@agentshore.example")
    env.setdefault("GIT_CONFIG_GLOBAL", "/dev/null")
    env.setdefault("GIT_CONFIG_SYSTEM", "/dev/null")
    return subprocess.check_output(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        text=True,
        stderr=subprocess.STDOUT,
    )


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """A minimal git repo with a fake ``origin`` remote."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "--initial-branch=main", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    (repo / "README.md").write_text("# test\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    # Add a fake origin URL (doesn't need to be reachable for remote list/remove).
    _git("remote", "add", "origin", "https://github.com/example/repo.git", cwd=repo)
    return repo


async def test_strip_non_origin_remotes_removes_fork(bare_repo: Path) -> None:
    """A ``fork`` remote is removed; only ``origin`` remains."""
    _git("remote", "add", "fork", "https://github.com/fork/repo.git", cwd=bare_repo)

    # Sanity: both remotes present before.
    before = _git("remote", cwd=bare_repo).split()
    assert set(before) == {"origin", "fork"}

    removed = await strip_non_origin_remotes(bare_repo)

    assert removed == ["fork"]
    after = _git("remote", cwd=bare_repo).split()
    assert after == ["origin"]


async def test_strip_non_origin_remotes_noop_when_only_origin(bare_repo: Path) -> None:
    """No-op when the only remote is ``origin``."""
    removed = await strip_non_origin_remotes(bare_repo)
    assert removed == []
    after = _git("remote", cwd=bare_repo).split()
    assert after == ["origin"]


async def test_strip_non_origin_remotes_noop_when_no_remotes(tmp_path: Path) -> None:
    """No-op when the repo has no remotes at all."""
    repo = tmp_path / "empty"
    repo.mkdir()
    _git("init", "--initial-branch=main", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    (repo / "f.txt").write_text("x\n")
    _git("add", "f.txt", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)

    removed = await strip_non_origin_remotes(repo)
    assert removed == []


async def test_strip_non_origin_remotes_removes_multiple(bare_repo: Path) -> None:
    """Multiple extra remotes are all removed."""
    _git("remote", "add", "fork", "https://github.com/fork/repo.git", cwd=bare_repo)
    _git("remote", "add", "upstream", "https://github.com/upstream/repo.git", cwd=bare_repo)

    removed = await strip_non_origin_remotes(bare_repo)

    assert set(removed) == {"fork", "upstream"}
    after = _git("remote", cwd=bare_repo).split()
    assert after == ["origin"]


async def test_strip_non_origin_remotes_returns_empty_on_missing_repo(
    tmp_path: Path,
) -> None:
    """A non-existent path doesn't raise — returns ``[]`` (best-effort)."""
    missing = tmp_path / "does-not-exist"
    removed = await strip_non_origin_remotes(missing)
    assert removed == []
