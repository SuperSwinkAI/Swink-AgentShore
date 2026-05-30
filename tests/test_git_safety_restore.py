"""Tests for restore_default_branch conflict recovery (desktop-kqo5 wedge fix).

A killed/errant merge_pr can leave the orchestrator's main checkout mid-merge
with unresolved conflicts. A bare ``git checkout`` cannot proceed, so the old
code latched a permanent trunk-dispatch pause. restore_default_branch now aborts
the in-progress merge and recovers.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentshore.core.git_safety import restore_default_branch


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


@pytest.fixture
def conflicted_repo(tmp_path: Path) -> Path:
    """A repo left on ``main`` with an in-progress conflicted merge (UU + MERGE_HEAD)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main", str(repo)], tmp_path)
    _git(["config", "user.email", "t@e.com"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "commit.gpgsign", "false"], repo)
    f = repo / "agent.py"
    f.write_text("base\n")
    _git(["add", "agent.py"], repo)
    _git(["commit", "-m", "base"], repo)

    # Divergent branch edits the same line.
    _git(["checkout", "-b", "feature"], repo)
    f.write_text("feature change\n")
    _git(["commit", "-am", "feature"], repo)
    _git(["checkout", "main"], repo)
    f.write_text("main change\n")
    _git(["commit", "-am", "main"], repo)

    # Merge feature -> conflict, left in progress on main.
    res = subprocess.run(["git", "merge", "feature"], cwd=str(repo), capture_output=True, text=True)
    assert res.returncode != 0, "expected a merge conflict"
    assert (repo / ".git" / "MERGE_HEAD").exists()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True
    ).stdout
    assert any(line.startswith("UU") for line in status.splitlines())
    return repo


def test_restore_default_branch_aborts_conflicted_merge(conflicted_repo: Path) -> None:
    assert restore_default_branch(conflicted_repo, "main") is True
    # MERGE_HEAD gone and the worktree is clean again.
    assert not (conflicted_repo / ".git" / "MERGE_HEAD").exists()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(conflicted_repo),
        capture_output=True,
        text=True,
    ).stdout
    assert status.strip() == ""


def test_restore_default_branch_clean_repo_is_noop_true(tmp_path: Path) -> None:
    repo = tmp_path / "clean"
    repo.mkdir()
    _git(["init", "-b", "main", str(repo)], tmp_path)
    _git(["config", "user.email", "t@e.com"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "commit.gpgsign", "false"], repo)
    (repo / "x").write_text("x\n")
    _git(["add", "x"], repo)
    _git(["commit", "-m", "x"], repo)
    assert restore_default_branch(repo, "main") is True
