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

from agentshore.core.git_safety import RestoreResult, restore_default_branch


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
    assert restore_default_branch(conflicted_repo, "main").ok is True
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
    result = restore_default_branch(repo, "main")
    assert result.ok is True
    assert result.stderr is None


@pytest.fixture
def untracked_blocked_repo(tmp_path: Path) -> Path:
    """HEAD on a feature branch + untracked files the default branch would overwrite.

    This is the #175 wedge: a trunk-scoped play ran in the main checkout, left
    untracked files, and moved HEAD onto a feature branch. ``git checkout main``
    is refused ("untracked working tree files would be overwritten"), and neither
    ``merge --abort`` nor ``reset --hard`` clears untracked state — so the old
    restore returned False and latched a permanent trunk-dispatch pause.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main", str(repo)], tmp_path)
    _git(["config", "user.email", "t@e.com"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "commit.gpgsign", "false"], repo)
    # AgentShore repos always gitignore .agentshore/ — so the quarantine dir the
    # restore creates does not itself re-dirty the tree.
    (repo / ".gitignore").write_text(".agentshore/\n")
    (repo / "data.txt").write_text("main data\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "nested.txt").write_text("main nested\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "base"], repo)
    # A feature branch that does not carry those tracked files.
    _git(["checkout", "-b", "feature"], repo)
    _git(["rm", "data.txt", "sub/nested.txt"], repo)
    _git(["commit", "-m", "drop tracked files"], repo)
    # The agent left HEAD on feature with *untracked* copies main would overwrite.
    (repo / "data.txt").write_text("contaminating untracked\n")
    (repo / "sub").mkdir(exist_ok=True)
    (repo / "sub" / "nested.txt").write_text("contaminating nested\n")
    refused = subprocess.run(
        ["git", "checkout", "main"], cwd=str(repo), capture_output=True, text=True
    )
    assert refused.returncode != 0, "expected a bare checkout to be refused"
    assert "overwritten" in (refused.stdout + refused.stderr)
    return repo


def test_restore_quarantines_untracked_blockers_and_lands_on_default(
    untracked_blocked_repo: Path,
) -> None:
    repo = untracked_blocked_repo
    assert restore_default_branch(repo, "main").ok is True
    # Back on the default branch with a clean tree.
    head = subprocess.run(
        ["git", "symbolic-ref", "HEAD"], cwd=str(repo), capture_output=True, text=True
    ).stdout.strip()
    assert head == "refs/heads/main"
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True
    ).stdout
    assert status.strip() == ""
    # The contaminating content was preserved (moved, not deleted), nested paths intact.
    reclaimed = repo / ".agentshore" / "reclaimed" / "restore"
    assert (reclaimed / "data.txt").read_text() == "contaminating untracked\n"
    assert (reclaimed / "sub" / "nested.txt").read_text() == "contaminating nested\n"
    # The tracked default-branch versions are restored in the working tree.
    assert (repo / "data.txt").read_text() == "main data\n"
    assert (repo / "sub" / "nested.txt").read_text() == "main nested\n"


@pytest.fixture
def tracked_dirt_blocked_repo(tmp_path: Path) -> Path:
    """HEAD on a feature branch + un-attributable *tracked* trunk modifications.

    The #175 follow-up: even after the untracked quarantine landed, reconcile
    could still jam because the main checkout carried TRACKED modifications it
    refused to discard, so the dispatch-pause latch never cleared. Here a play
    edited a tracked file (``conflict.txt``) whose content differs between the
    feature branch and ``main``, so a bare ``git checkout main`` is refused
    ("Your local changes would be overwritten"). The deterministic ``reset
    --hard`` tier must discard the un-attributable tracked dirt and land on the
    default branch.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main", str(repo)], tmp_path)
    _git(["config", "user.email", "t@e.com"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "commit.gpgsign", "false"], repo)
    (repo / ".gitignore").write_text(".agentshore/\n")
    (repo / "conflict.txt").write_text("main version\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "base"], repo)
    # Feature branch carries a *different* committed version of the same file.
    _git(["checkout", "-b", "feature"], repo)
    (repo / "conflict.txt").write_text("feature committed version\n")
    _git(["commit", "-am", "feature edit"], repo)
    # The agent left HEAD on feature with an uncommitted tracked modification
    # that ``main`` would overwrite.
    (repo / "conflict.txt").write_text("un-attributable local change\n")
    refused = subprocess.run(
        ["git", "checkout", "main"], cwd=str(repo), capture_output=True, text=True
    )
    assert refused.returncode != 0, "expected a bare checkout to be refused"
    assert "overwritten" in (refused.stdout + refused.stderr)
    return repo


def test_restore_resets_tracked_dirt_and_lands_on_default(
    tracked_dirt_blocked_repo: Path,
) -> None:
    """#175 follow-up: ``reset --hard`` clears un-attributable tracked trunk dirt.

    Proves the deterministic restore (the latch-clearing backstop the reconcile
    path calls) genuinely resolves the tracked-dirt case, so the dispatch-paused
    latch lifts instead of staying stuck on a state the reconcile skill's stricter
    attribution policy refuses to discard.
    """
    repo = tracked_dirt_blocked_repo
    assert restore_default_branch(repo, "main").ok is True
    head = subprocess.run(
        ["git", "symbolic-ref", "HEAD"], cwd=str(repo), capture_output=True, text=True
    ).stdout.strip()
    assert head == "refs/heads/main"
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True
    ).stdout
    assert status.strip() == ""
    # The default-branch committed content is back; the local change was discarded.
    assert (repo / "conflict.txt").read_text() == "main version\n"


def test_restore_surfaces_stderr_on_genuine_failure(tmp_path: Path) -> None:
    """#175 observability: a real restore failure carries the git stderr reason.

    Point ``restore_default_branch`` at a branch that does not exist so every
    checkout tier fails. The result must be ``ok=False`` with a non-empty
    ``stderr`` so the caller can log a concrete ``reason=`` on
    ``main_repo_auto_restore_failed`` instead of an opaque pause.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main", str(repo)], tmp_path)
    _git(["config", "user.email", "t@e.com"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "commit.gpgsign", "false"], repo)
    (repo / "x").write_text("x\n")
    _git(["add", "x"], repo)
    _git(["commit", "-m", "x"], repo)

    result = restore_default_branch(repo, "does-not-exist")
    assert result.ok is False
    assert result.stderr is not None
    assert result.stderr  # non-empty git error text
    assert len(result.stderr) <= 500


def test_restore_result_is_truthy_on_success() -> None:
    """Legacy ``if restore_default_branch(...):`` callers stay correct."""
    assert bool(RestoreResult(ok=True)) is True
    assert bool(RestoreResult(ok=False, stderr="boom")) is False
