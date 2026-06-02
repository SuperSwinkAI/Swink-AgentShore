"""Tests for the main-repo symbolic-ref invariant guard (desktop-kqo5).

Covers:
- Pure-Python helpers in ``agentshore.core.git_safety``.
- The orchestrator boundary check in ``_CompletionMixin._check_main_repo_invariant``
  via a code-review-style synthetic that mutates HEAD while a play is in flight.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest
import structlog

from agentshore.core.git_safety import (
    PATH_ESCAPE_MARKER,
    check_main_repo_branch_mutated,
    current_head_ref,
    find_path_escape_siblings,
    path_contains_backslash_space,
    resolve_default_branch,
    restore_default_branch,
)
from agentshore.core.main_repo_guard import MainRepoGuard


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def main_repo(tmp_path: Path) -> Path:
    """Minimal git repo with origin/HEAD pointing at main + a agentshore/X branch."""
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

    # Second branch — simulates the agentshore/<issue> branch agents land on.
    _git(["checkout", "-b", "agentshore/153-implement-alerting-hooks-and-runbook"], repo)
    (repo / "alert.txt").write_text("alerting\n")
    _git(["add", "alert.txt"], repo)
    _git(["commit", "-m", "alerting hooks"], repo)
    _git(["push", "-u", "origin", "agentshore/153-implement-alerting-hooks-and-runbook"], repo)
    _git(["checkout", "main"], repo)
    _git(["fetch", "origin"], repo)
    _git(["remote", "set-head", "origin", "main"], repo)
    return repo


# ---------------------------------------------------------------------------
# git_safety helpers (pure functions, no orchestrator)
# ---------------------------------------------------------------------------


def test_resolve_default_branch_reads_origin_head(main_repo: Path) -> None:
    branch, assumed = resolve_default_branch(main_repo)
    assert branch == "main"
    assert assumed is False


def test_resolve_default_branch_falls_back_when_no_origin(tmp_path: Path) -> None:
    repo = tmp_path / "no-origin"
    repo.mkdir()
    _git(["init", "-b", "trunk", str(repo)], tmp_path)
    branch, assumed = resolve_default_branch(repo)
    assert branch == "main"
    assert assumed is True


def test_current_head_ref_returns_symbolic_ref(main_repo: Path) -> None:
    assert current_head_ref(main_repo) == "refs/heads/main"


def test_current_head_ref_returns_none_when_detached(main_repo: Path) -> None:
    _git(["checkout", "--detach", "HEAD"], main_repo)
    assert current_head_ref(main_repo) is None


def test_restore_default_branch_recovers_main(main_repo: Path) -> None:
    _git(["checkout", "agentshore/153-implement-alerting-hooks-and-runbook"], main_repo)
    assert current_head_ref(main_repo) != "refs/heads/main"
    assert restore_default_branch(main_repo, "main") is True
    assert current_head_ref(main_repo) == "refs/heads/main"


def test_check_main_repo_branch_mutated_detects_and_restores(main_repo: Path) -> None:
    pre = current_head_ref(main_repo)
    assert pre == "refs/heads/main"
    _git(["checkout", "agentshore/153-implement-alerting-hooks-and-runbook"], main_repo)
    mutated, post, restored = check_main_repo_branch_mutated(
        main_repo, pre_ref=pre, default_branch="main"
    )
    assert mutated is True
    assert post == "refs/heads/agentshore/153-implement-alerting-hooks-and-runbook"
    assert restored is True
    assert current_head_ref(main_repo) == "refs/heads/main"


def test_check_main_repo_branch_mutated_no_op_when_unchanged(main_repo: Path) -> None:
    pre = current_head_ref(main_repo)
    mutated, post, restored = check_main_repo_branch_mutated(
        main_repo, pre_ref=pre, default_branch="main"
    )
    assert mutated is False
    assert post == pre
    assert restored is False


def test_path_contains_backslash_space() -> None:
    assert path_contains_backslash_space("/Users/example/Dev/Some\\ Project") is True
    assert path_contains_backslash_space(Path("/tmp/Bad\\ Path")) is True
    assert path_contains_backslash_space("/tmp/Good Path") is False
    assert path_contains_backslash_space("/tmp/no-space") is False


def test_find_path_escape_siblings_finds_bad_sibling(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "good-project").mkdir()
    (parent / "Bad\\ Sibling").mkdir()
    (parent / "Another\\ Bad").mkdir()
    found = find_path_escape_siblings(parent / "good-project")
    found_names = sorted(p.name for p in found)
    assert found_names == ["Another\\ Bad", "Bad\\ Sibling"]


def test_find_path_escape_siblings_returns_empty_when_clean(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "project").mkdir()
    (parent / "sibling").mkdir()
    assert find_path_escape_siblings(parent / "project") == []


def test_path_escape_marker_constant() -> None:
    # Catch-and-rename guard: the marker is load-bearing for the sweeper.
    assert PATH_ESCAPE_MARKER == "\\ "


# ---------------------------------------------------------------------------
# Orchestrator boundary check via _check_main_repo_invariant
# ---------------------------------------------------------------------------


class _StubManager:
    """Minimal AgentManager substitute exposing the ``handles`` dict."""

    def __init__(self) -> None:
        self.handles: dict[str, object] = {}


class _CompletionGuardHarness:
    """Wraps a real Orchestrator-by-MRO call into _check_main_repo_invariant.

    Builds the bare minimum attribute surface ``_CompletionMixin`` needs to
    call its own method without spinning up the full bootstrap pipeline.
    """

    def __init__(self, *, repo_root: Path, default_branch: str = "main") -> None:
        from agentshore.core.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch._repo_root = repo_root
        orch._session_id = "test-session"
        orch._main_repo = MainRepoGuard(default_branch=default_branch)
        orch._manager = _StubManager()  # type: ignore[assignment]
        self.orch = orch


def _events_from_caplog(records: list[logging.LogRecord]) -> list[dict[str, object]]:
    """Reconstruct structlog event dicts from stdlib LogRecord (see test_core_weights_inventory)."""
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
            ev: dict[str, object] = {
                k: v for k, v in r.__dict__.items() if k not in std_fields and not k.startswith("_")
            }
            out.append(ev)
    return out


def _event_names(captured: list[dict[str, object]]) -> list[str]:
    """Extract just the structlog ``event`` keys from a capture_logs() payload."""
    return [str(e.get("event", "")) for e in captured]


@pytest.mark.asyncio
async def test_code_review_mutating_main_triggers_warning_and_restore(
    main_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Simulate the canonical bug: code_review runs ``git checkout agentshore/X``
    in the main worktree. Assert play_completed emits
    ``main_repo_branch_mutated`` and HEAD is restored to default.
    """
    from agentshore.state import PlayType

    harness = _CompletionGuardHarness(repo_root=main_repo)
    dispatch_id = "d-code-review-mutates"
    harness.orch._main_repo.record_pre_play_branch(dispatch_id, "refs/heads/main")

    # Agent mutated the repo during the play.
    _git(["checkout", "agentshore/153-implement-alerting-hooks-and-runbook"], main_repo)
    assert current_head_ref(main_repo) == (
        "refs/heads/agentshore/153-implement-alerting-hooks-and-runbook"
    )

    with (
        structlog.testing.capture_logs() as captured_raw,
        caplog.at_level(logging.INFO, logger="agentshore.core"),
    ):
        await harness.orch._check_main_repo_invariant(
            dispatch_id=dispatch_id,
            play_type=PlayType.CODE_REVIEW,
            agent_id="claude-1",
            agent_type="claude_code",
        )
    captured = captured_raw if captured_raw else _events_from_caplog(list(caplog.records))

    # Auto-restore happened.
    assert current_head_ref(main_repo) == "refs/heads/main"
    # Snapshot consumed.
    assert dispatch_id not in harness.orch._main_repo._pre_play_branches
    event_names = _event_names(captured)
    assert "main_repo_branch_mutated" in event_names
    assert "main_repo_branch_restored" in event_names
    assert "main_repo_auto_restore_failed" not in event_names
    # Dispatch should NOT be paused on a successful restore.
    assert harness.orch._main_repo.dispatch_paused is False
    # The mutated event carries identity for the operator.
    mutated_event = next(e for e in captured if e.get("event") == "main_repo_branch_mutated")
    assert mutated_event["play_type"] == "code_review"
    assert mutated_event["agent_id"] == "claude-1"
    assert mutated_event["agent_type"] == "claude_code"
    assert mutated_event["pre_play_branch"] == "refs/heads/main"
    assert mutated_event["post_play_branch"] == (
        "refs/heads/agentshore/153-implement-alerting-hooks-and-runbook"
    )


@pytest.mark.asyncio
async def test_no_snapshot_no_check(main_repo: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A dispatch that never recorded a pre_play snapshot is silently skipped."""
    from agentshore.state import PlayType

    harness = _CompletionGuardHarness(repo_root=main_repo)
    _git(["checkout", "agentshore/153-implement-alerting-hooks-and-runbook"], main_repo)

    with (
        structlog.testing.capture_logs() as captured_raw,
        caplog.at_level(logging.INFO, logger="agentshore.core"),
    ):
        await harness.orch._check_main_repo_invariant(
            dispatch_id="nonexistent",
            play_type=PlayType.CODE_REVIEW,
            agent_id=None,
            agent_type=None,
        )
    captured = captured_raw if captured_raw else _events_from_caplog(list(caplog.records))
    assert "main_repo_branch_mutated" not in _event_names(captured)


@pytest.mark.asyncio
async def test_detached_head_post_play_is_treated_as_mutation(
    main_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from agentshore.state import PlayType

    harness = _CompletionGuardHarness(repo_root=main_repo)
    dispatch_id = "d-detached"
    harness.orch._main_repo.record_pre_play_branch(dispatch_id, "refs/heads/main")

    _git(["checkout", "--detach", "HEAD"], main_repo)
    assert current_head_ref(main_repo) is None

    with (
        structlog.testing.capture_logs() as captured_raw,
        caplog.at_level(logging.INFO, logger="agentshore.core"),
    ):
        await harness.orch._check_main_repo_invariant(
            dispatch_id=dispatch_id,
            play_type=PlayType.RUN_QA,
            agent_id="claude-1",
            agent_type="claude_code",
        )
    captured = captured_raw if captured_raw else _events_from_caplog(list(caplog.records))

    assert current_head_ref(main_repo) == "refs/heads/main"
    assert "main_repo_branch_mutated" in _event_names(captured)
    mutated_event = next(e for e in captured if e.get("event") == "main_repo_branch_mutated")
    # Detached HEAD post-play is signalled as post_play_branch = None.
    assert mutated_event["post_play_branch"] is None


@pytest.mark.asyncio
async def test_auto_restore_failure_pauses_dispatch(
    main_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from agentshore.core.mixins import completion as _completion_mod
    from agentshore.state import PlayType

    harness = _CompletionGuardHarness(repo_root=main_repo)
    dispatch_id = "d-restore-fails"
    harness.orch._main_repo.record_pre_play_branch(dispatch_id, "refs/heads/main")
    _git(["checkout", "agentshore/153-implement-alerting-hooks-and-runbook"], main_repo)

    def _broken_check(*_a: object, **_kw: object) -> tuple[bool, str | None, bool]:
        return (True, "refs/heads/agentshore/X", False)

    monkeypatch.setattr(_completion_mod, "check_main_repo_branch_mutated", _broken_check)

    with (
        structlog.testing.capture_logs() as captured_raw,
        caplog.at_level(logging.INFO, logger="agentshore.core"),
    ):
        await harness.orch._check_main_repo_invariant(
            dispatch_id=dispatch_id,
            play_type=PlayType.CODE_REVIEW,
            agent_id="claude-1",
            agent_type="claude_code",
        )
    captured = captured_raw if captured_raw else _events_from_caplog(list(caplog.records))

    assert harness.orch._main_repo.dispatch_paused is True
    assert "main_repo_auto_restore_failed" in _event_names(captured)
