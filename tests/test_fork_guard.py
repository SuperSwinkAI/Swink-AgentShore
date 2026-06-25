"""Unit tests for agentshore.core.fork_guard — pure helpers only, no orchestrator."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentshore.core.fork_guard import (
    ForkFinding,
    detect_cross_fork_pr_artifacts,
    detect_non_origin_remotes,
    parse_origin_owner,
)

# ---------------------------------------------------------------------------
# parse_origin_owner
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/jwesleye/myrepo.git", "jwesleye"),
        ("https://github.com/jwesleye/myrepo", "jwesleye"),
        ("git@github.com:jwesleye/myrepo.git", "jwesleye"),
        ("git@github.com:jwesleye/myrepo", "jwesleye"),
        ("jwesleye/myrepo", "jwesleye"),
        ("unseriousAI/zeke", "unseriousAI"),
        ("https://github.com/SuperSwinkAI/Swink-AgentShore.git", "SuperSwinkAI"),
        # Unparseable
        ("not-a-url", None),
        ("", None),
    ],
)
def test_parse_origin_owner(remote: str, expected: str | None) -> None:
    assert parse_origin_owner(remote) == expected


# ---------------------------------------------------------------------------
# detect_cross_fork_pr_artifacts
# ---------------------------------------------------------------------------


def test_detect_cross_fork_pr_same_owner_no_findings() -> None:
    artifacts = [{"type": "pr", "url": "https://github.com/jwesleye/myrepo/pull/1"}]
    findings = detect_cross_fork_pr_artifacts(artifacts, origin_owner="jwesleye")
    assert findings == []


def test_detect_cross_fork_pr_case_insensitive_no_findings() -> None:
    artifacts = [{"type": "pr", "url": "https://github.com/JwesLeye/myrepo/pull/1"}]
    findings = detect_cross_fork_pr_artifacts(artifacts, origin_owner="jwesleye")
    assert findings == []


def test_detect_cross_fork_pr_different_owner_one_finding() -> None:
    artifacts = [{"type": "pr", "url": "https://github.com/unseriousAI/zeke/pull/5"}]
    findings = detect_cross_fork_pr_artifacts(artifacts, origin_owner="jwesleye")
    assert len(findings) == 1
    assert findings[0].kind == "cross_fork_pr"
    assert "unseriousAI" in findings[0].detail
    assert "jwesleye" in findings[0].detail


def test_detect_cross_fork_pr_multiple_artifacts_mixed() -> None:
    artifacts = [
        {"type": "pr", "url": "https://github.com/jwesleye/myrepo/pull/1"},
        {"type": "pr", "url": "https://github.com/unseriousAI/fork/pull/7"},
        {"type": "issue", "url": "https://github.com/unseriousAI/zeke/issues/3"},
        "not-a-dict",
        {"type": "pr"},  # no url
        {"type": "pr", "url": 42},  # non-string url
    ]
    findings = detect_cross_fork_pr_artifacts(artifacts, origin_owner="jwesleye")
    assert len(findings) == 1
    assert findings[0].kind == "cross_fork_pr"
    assert "fork" in findings[0].detail


def test_detect_cross_fork_pr_skips_non_pr_artifacts() -> None:
    artifacts = [
        {"type": "issue", "url": "https://github.com/otherfork/repo/issues/1"},
        {"type": "pr_merged", "url": "https://github.com/anotherfork/repo/pull/2"},
    ]
    findings = detect_cross_fork_pr_artifacts(artifacts, origin_owner="jwesleye")
    assert findings == []


def test_detect_cross_fork_pr_unparseable_url_skipped() -> None:
    artifacts = [{"type": "pr", "url": "not-a-github-url"}]
    findings = detect_cross_fork_pr_artifacts(artifacts, origin_owner="jwesleye")
    assert findings == []


# ---------------------------------------------------------------------------
# detect_non_origin_remotes
# ---------------------------------------------------------------------------


@pytest.fixture()
def git_repo_with_remotes(tmp_path: Path) -> Path:
    """Create a bare-minimum git repo with 'origin' and 'fork' remotes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    # Add origin — point to a local path so no network is needed.
    remote_origin = tmp_path / "origin.git"
    remote_origin.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote_origin)], check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote_origin)],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    return repo


@pytest.fixture()
def git_repo_origin_only(tmp_path: Path) -> Path:
    """Create a git repo with only the 'origin' remote."""
    repo = tmp_path / "repo_origin_only"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    remote_origin = tmp_path / "origin2.git"
    remote_origin.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote_origin)], check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote_origin)],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    return repo


@pytest.mark.asyncio
async def test_detect_non_origin_remotes_with_fork_remote(git_repo_with_remotes: Path) -> None:
    # Add a 'fork' remote on top of the fixture.
    subprocess.run(
        ["git", "remote", "add", "fork", "https://github.com/someone/fork.git"],
        cwd=str(git_repo_with_remotes),
        check=True,
        capture_output=True,
    )
    findings = await detect_non_origin_remotes(git_repo_with_remotes)
    assert len(findings) == 1
    assert findings[0].kind == "non_origin_remote"
    assert "fork" in findings[0].detail


@pytest.mark.asyncio
async def test_detect_non_origin_remotes_only_origin_no_findings(
    git_repo_origin_only: Path,
) -> None:
    findings = await detect_non_origin_remotes(git_repo_origin_only)
    assert findings == []


@pytest.mark.asyncio
async def test_detect_non_origin_remotes_bad_path_returns_empty(tmp_path: Path) -> None:
    findings = await detect_non_origin_remotes(tmp_path / "nonexistent")
    assert findings == []


@pytest.mark.asyncio
async def test_detect_non_origin_remotes_multiple_extra_remotes(
    git_repo_with_remotes: Path,
) -> None:
    for name in ("fork", "upstream"):
        subprocess.run(
            ["git", "remote", "add", name, f"https://github.com/someone/{name}.git"],
            cwd=str(git_repo_with_remotes),
            check=True,
            capture_output=True,
        )
    findings = await detect_non_origin_remotes(git_repo_with_remotes)
    assert len(findings) == 2
    kinds = {f.kind for f in findings}
    assert kinds == {"non_origin_remote"}
    names = {f.detail for f in findings}
    assert any("fork" in d for d in names)
    assert any("upstream" in d for d in names)


# ---------------------------------------------------------------------------
# ForkFinding dataclass
# ---------------------------------------------------------------------------


def test_fork_finding_frozen() -> None:
    f = ForkFinding(kind="cross_fork_pr", detail="owner mismatch")
    with pytest.raises(AttributeError):
        f.kind = "other"  # type: ignore[misc]
