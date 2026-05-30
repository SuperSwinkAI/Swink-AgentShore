"""Regression test pinning the merge_pr no-false-positive guarantee.

``merge_pr`` legitimately advances the SHA under ``refs/heads/main`` via
``git merge --no-ff origin/<branch>`` followed by ``git push``. The
symbolic-ref invariant guard must NOT treat that as a mutation — the ref
pointer is unchanged, only the commit it resolves to advances.

See ``desktop-kqo5`` BEHAVIOR PER PLAY TYPE table.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest
import structlog

from agentshore.core.git_safety import check_main_repo_branch_mutated, current_head_ref


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
def merge_repo(tmp_path: Path) -> Path:
    """Repo with main + feature branch pushed to origin, ready for a merge."""
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

    _git(["checkout", "-b", "feature/widget"], repo)
    (repo / "widget.txt").write_text("widget\n")
    _git(["add", "widget.txt"], repo)
    _git(["commit", "-m", "add widget"], repo)
    _git(["push", "-u", "origin", "feature/widget"], repo)
    _git(["checkout", "main"], repo)
    _git(["fetch", "origin"], repo)
    _git(["remote", "set-head", "origin", "main"], repo)
    return repo


def test_merge_pr_no_ff_advances_sha_but_symbolic_ref_unchanged(
    merge_repo: Path,
) -> None:
    """The canonical merge_pr behavior: SHA advances, symbolic-ref stays put."""
    pre_ref = current_head_ref(merge_repo)
    pre_sha = _git(["rev-parse", "HEAD"], merge_repo).stdout.strip()
    assert pre_ref == "refs/heads/main"

    # Simulate the merge_pr play body: merge origin/<branch> --no-ff + push.
    _git(["merge", "--no-ff", "origin/feature/widget", "-m", "Merge feature/widget"], merge_repo)
    _git(["push", "origin", "main"], merge_repo)

    post_ref = current_head_ref(merge_repo)
    post_sha = _git(["rev-parse", "HEAD"], merge_repo).stdout.strip()

    # SHA advanced (commit moved forward).
    assert post_sha != pre_sha
    # Symbolic ref unchanged — this is what the guard checks.
    assert post_ref == pre_ref == "refs/heads/main"

    # The guard's verdict: NO mutation, no auto-restore needed.
    mutated, observed_post, restored = check_main_repo_branch_mutated(
        merge_repo, pre_ref=pre_ref, default_branch="main"
    )
    assert mutated is False
    assert observed_post == pre_ref
    assert restored is False


@pytest.mark.asyncio
async def test_orchestrator_guard_emits_no_warning_on_merge_pr(
    merge_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """End-to-end through ``_check_main_repo_invariant``: no warning, no restore."""
    from agentshore.core.orchestrator import Orchestrator
    from agentshore.state import PlayType

    orch = Orchestrator.__new__(Orchestrator)
    orch._repo_root = merge_repo
    orch._session_id = "test-session-merge"
    orch._default_branch = "main"
    orch._pre_play_branches = {"d-merge": "refs/heads/main"}
    orch._main_repo_dispatch_paused = False

    class _StubManager:
        handles: dict[str, object] = {}

    orch._manager = _StubManager()  # type: ignore[assignment]

    # Simulate the merge mid-play.
    _git(["merge", "--no-ff", "origin/feature/widget", "-m", "Merge feature/widget"], merge_repo)
    _git(["push", "origin", "main"], merge_repo)

    with (
        structlog.testing.capture_logs() as captured_raw,
        caplog.at_level(logging.INFO, logger="agentshore.core"),
    ):
        await orch._check_main_repo_invariant(
            dispatch_id="d-merge",
            play_type=PlayType.MERGE_PR,
            agent_id="claude-merge",
            agent_type="claude_code",
        )
    captured = captured_raw if captured_raw else _events_from_caplog(list(caplog.records))

    event_names = [str(e.get("event", "")) for e in captured]
    assert "main_repo_branch_mutated" not in event_names, (
        "merge_pr must not be flagged — symbolic-ref unchanged despite SHA advance"
    )
    assert "main_repo_auto_restore_failed" not in event_names
    # No paused dispatch on a healthy merge_pr.
    assert orch._main_repo_dispatch_paused is False
