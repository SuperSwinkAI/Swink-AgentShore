"""Tests for the deterministic post-merge fast-forward sync (branch_sync).

Exercises :func:`fast_forward_local_branch` against real temporary git repos
(the established pattern in ``test_merge_pr_no_false_positive``), covering the
fast-forward, already-current, no-local-branch, diverged (left untouched), and
fetch-failed paths — for both the checked-out and not-checked-out target.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentshore.core.branch_sync import (
    FFSyncStatus,
    fast_forward_local_branch,
)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _sha(repo: Path, ref: str) -> str:
    return _git(["rev-parse", ref], repo).stdout.strip()


@pytest.fixture
def synced_repo(tmp_path: Path) -> Path:
    """A clone whose local ``integration`` tracks an upstream, plus a second
    clone used to push *new* commits to the upstream so the first clone falls
    behind. Returns the first clone (the "primary checkout")."""
    upstream = tmp_path / "upstream.git"
    _git(["init", "--bare", "-b", "main", str(upstream)], tmp_path)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "-b", "main", str(seed)], tmp_path)
    _git(["config", "user.email", "t@example.com"], seed)
    _git(["config", "user.name", "T"], seed)
    _git(["config", "commit.gpgsign", "false"], seed)
    (seed / "README.md").write_text("hello\n")
    _git(["add", "README.md"], seed)
    _git(["commit", "-m", "init"], seed)
    _git(["remote", "add", "origin", str(upstream)], seed)
    _git(["push", "-u", "origin", "main"], seed)
    _git(["checkout", "-b", "integration"], seed)
    (seed / "f.txt").write_text("base\n")
    _git(["add", "f.txt"], seed)
    _git(["commit", "-m", "base"], seed)
    _git(["push", "-u", "origin", "integration"], seed)

    repo = tmp_path / "repo"
    _git(["clone", str(upstream), str(repo)], tmp_path)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "commit.gpgsign", "false"], repo)
    # Materialise local integration, then park on main (realistic primary-checkout state).
    _git(["checkout", "integration"], repo)
    _git(["checkout", "main"], repo)
    return repo


def _advance_remote_integration(tmp_path: Path) -> str:
    """Push a new commit to origin/integration from a throwaway clone."""
    pusher = tmp_path / "pusher"
    if not pusher.exists():
        upstream = tmp_path / "upstream.git"
        _git(["clone", str(upstream), str(pusher)], tmp_path)
        _git(["config", "user.email", "t@example.com"], pusher)
        _git(["config", "user.name", "T"], pusher)
        _git(["config", "commit.gpgsign", "false"], pusher)
    _git(["checkout", "integration"], pusher)
    _git(["pull", "--ff-only"], pusher)
    marker = f"adv-{len(list(pusher.glob('adv-*')))}"
    (pusher / marker).write_text("x\n")
    _git(["add", marker], pusher)
    _git(["commit", "-m", marker], pusher)
    _git(["push", "origin", "integration"], pusher)
    return _sha(pusher, "integration")


async def test_ff_synced_when_not_checked_out(synced_repo: Path, tmp_path: Path) -> None:
    """Local integration (not checked out) advances via update-ref to remote."""
    new_sha = _advance_remote_integration(tmp_path)
    assert _sha(synced_repo, "integration") != new_sha

    result = await fast_forward_local_branch(synced_repo, "integration")

    assert result.status is FFSyncStatus.SYNCED
    assert _sha(synced_repo, "integration") == new_sha
    # Checkout must stay on main — sync never switches branches.
    assert _git(["branch", "--show-current"], synced_repo).stdout.strip() == "main"


async def test_ff_synced_when_checked_out(synced_repo: Path, tmp_path: Path) -> None:
    """When integration *is* the current checkout, merge --ff-only advances it."""
    _git(["checkout", "integration"], synced_repo)
    new_sha = _advance_remote_integration(tmp_path)

    result = await fast_forward_local_branch(synced_repo, "integration")

    assert result.status is FFSyncStatus.SYNCED
    assert _sha(synced_repo, "integration") == new_sha
    assert _git(["branch", "--show-current"], synced_repo).stdout.strip() == "integration"


async def test_already_current_is_noop(synced_repo: Path) -> None:
    result = await fast_forward_local_branch(synced_repo, "integration")
    assert result.status is FFSyncStatus.ALREADY_CURRENT


async def test_no_local_branch(synced_repo: Path, tmp_path: Path) -> None:
    """Remote branch exists but the local clone has no ref for it → no-op.

    (This is the path that matters in practice: target_branch resolves on the
    remote, but the primary checkout never materialised a local branch for it.)
    """
    # Remote-only branch 'staging' the primary clone never tracks.
    pusher = tmp_path / "pusher"
    upstream = tmp_path / "upstream.git"
    _git(["clone", str(upstream), str(pusher)], tmp_path)
    _git(["config", "user.email", "t@example.com"], pusher)
    _git(["config", "user.name", "T"], pusher)
    _git(["config", "commit.gpgsign", "false"], pusher)
    _git(["checkout", "-b", "staging"], pusher)
    (pusher / "s.txt").write_text("s\n")
    _git(["add", "s.txt"], pusher)
    _git(["commit", "-m", "staging"], pusher)
    _git(["push", "-u", "origin", "staging"], pusher)

    result = await fast_forward_local_branch(synced_repo, "staging")
    assert result.status is FFSyncStatus.NO_LOCAL_BRANCH


async def test_diverged_left_untouched(synced_repo: Path, tmp_path: Path) -> None:
    """Local integration with a commit not on the remote is never rewound."""
    # Local diverges: a local-only commit on integration.
    _git(["checkout", "integration"], synced_repo)
    (synced_repo / "local-only.txt").write_text("local\n")
    _git(["add", "local-only.txt"], synced_repo)
    _git(["commit", "-m", "local-only"], synced_repo)
    _git(["checkout", "main"], synced_repo)
    local_before = _sha(synced_repo, "integration")
    # Remote advances independently.
    _advance_remote_integration(tmp_path)

    result = await fast_forward_local_branch(synced_repo, "integration")

    assert result.status is FFSyncStatus.DIVERGED
    assert _sha(synced_repo, "integration") == local_before


async def test_fetch_failed_is_non_raising(synced_repo: Path) -> None:
    """A broken remote yields FETCH_FAILED rather than raising."""
    _git(["remote", "set-url", "origin", str(synced_repo / "does-not-exist.git")], synced_repo)
    result = await fast_forward_local_branch(synced_repo, "integration", timeout=10.0)
    assert result.status is FFSyncStatus.FETCH_FAILED
