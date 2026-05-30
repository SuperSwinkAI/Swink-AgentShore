"""Session-start sweeper test for the backslash-space sibling leak (desktop-4ugk part 3).

The sweeper flags sibling directories whose names contain literal
backslash-space and surfaces them as ``project_root_escape_detected``. It
must NEVER auto-delete — operator intervention only.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest
import structlog

from agentshore.core.git_safety import find_path_escape_siblings


def _events_from_caplog(records: list[logging.LogRecord]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    std_fields = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()) | {
        "message",
        "asctime",
    }
    for r in records:
        msg = r.msg
        if isinstance(msg, dict):
            out.append(msg)
            continue
        if hasattr(r, "event"):
            out.append(
                {
                    k: v
                    for k, v in r.__dict__.items()
                    if k not in std_fields and not k.startswith("_")
                }
            )
    return out


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def parent_with_escape(tmp_path: Path) -> tuple[Path, Path]:
    """Parent dir with a healthy project + a backslash-space leaked sibling."""
    parent = tmp_path / "Development"
    parent.mkdir()
    project = parent / "example-repo"
    project.mkdir()
    leaked = parent / "Open\\ Agent\\ Tools"
    leaked.mkdir()
    (leaked / "sentinel.txt").write_text("must-not-delete\n")
    return project, leaked


def test_find_path_escape_siblings_finds_leak(parent_with_escape: tuple[Path, Path]) -> None:
    project, leaked = parent_with_escape
    found = find_path_escape_siblings(project)
    assert leaked in found


def test_sweeper_does_not_auto_delete(parent_with_escape: tuple[Path, Path]) -> None:
    project, leaked = parent_with_escape
    found = find_path_escape_siblings(project)
    assert leaked in found
    # Critical: the helper observes only — it must never remove the sibling.
    assert leaked.exists()
    assert (leaked / "sentinel.txt").read_text() == "must-not-delete\n"


@pytest.mark.asyncio
async def test_phase_git_safety_sweep_emits_warning_and_preserves_dir(
    parent_with_escape: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """``_phase_git_safety_sweep`` runs the sweep and emits the warning.

    The project must be a real git repo so the default-branch resolution
    leg of the phase succeeds.
    """
    from agentshore.core.orchestrator import Orchestrator
    from agentshore.core.phases import _phase_git_safety_sweep

    project, leaked = parent_with_escape
    upstream = project.parent / "upstream.git"
    _git(["init", "--bare", "-b", "main", str(upstream)], project.parent)
    _git(["init", "-b", "main", str(project)], project.parent)
    _git(["config", "user.email", "test@example.com"], project)
    _git(["config", "user.name", "Test"], project)
    _git(["config", "commit.gpgsign", "false"], project)
    (project / "README.md").write_text("hello\n")
    _git(["add", "README.md"], project)
    _git(["commit", "-m", "init"], project)
    _git(["remote", "add", "origin", str(upstream)], project)
    _git(["push", "-u", "origin", "main"], project)
    _git(["remote", "set-head", "origin", "main"], project)

    orch = Orchestrator.__new__(Orchestrator)
    orch._default_branch = "main"

    with (
        structlog.testing.capture_logs() as captured_raw,
        caplog.at_level(logging.INFO, logger="agentshore.core"),
    ):
        await _phase_git_safety_sweep(orch=orch, repo_root=project, sid="test-sweep")
    captured = captured_raw if captured_raw else _events_from_caplog(list(caplog.records))

    event_names = [str(e.get("event", "")) for e in captured]
    assert "project_root_escape_detected" in event_names
    # Auto-delete must NEVER happen.
    assert leaked.exists()
    assert (leaked / "sentinel.txt").read_text() == "must-not-delete\n"


@pytest.mark.asyncio
async def test_phase_git_safety_sweep_restores_poisoned_head(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Session start against a repo currently on agentshore/* runs the sweeper,
    logs, and restores."""
    from agentshore.core.git_safety import current_head_ref
    from agentshore.core.orchestrator import Orchestrator
    from agentshore.core.phases import _phase_git_safety_sweep

    upstream = tmp_path / "upstream.git"
    _git(["init", "--bare", "-b", "main", str(upstream)], tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main", str(repo)], tmp_path)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    _git(["config", "commit.gpgsign", "false"], repo)
    (repo / "README.md").write_text("hello\n")
    _git(["add", "README.md"], repo)
    _git(["commit", "-m", "init"], repo)
    _git(["remote", "add", "origin", str(upstream)], repo)
    _git(["push", "-u", "origin", "main"], repo)
    _git(["checkout", "-b", "agentshore/153-leftover"], repo)
    (repo / "alert.txt").write_text("alert\n")
    _git(["add", "alert.txt"], repo)
    _git(["commit", "-m", "leftover"], repo)
    _git(["push", "-u", "origin", "agentshore/153-leftover"], repo)
    _git(["remote", "set-head", "origin", "main"], repo)
    # IMPORTANT: leave the working repo checked out on agentshore/X to simulate
    # a poisoned session start.

    orch = Orchestrator.__new__(Orchestrator)
    orch._default_branch = "main"

    with (
        structlog.testing.capture_logs() as captured_raw,
        caplog.at_level(logging.INFO, logger="agentshore.core"),
    ):
        await _phase_git_safety_sweep(orch=orch, repo_root=repo, sid="test-poisoned")
    captured = captured_raw if captured_raw else _events_from_caplog(list(caplog.records))

    event_names = [str(e.get("event", "")) for e in captured]
    assert "main_repo_branch_mutated" in event_names
    assert "main_repo_branch_restored" in event_names
    # And HEAD restored back to main.
    assert current_head_ref(repo) == "refs/heads/main"
    # Default branch resolved into the orchestrator cache.
    assert orch._default_branch == "main"
