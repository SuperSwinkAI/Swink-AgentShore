"""Tests for untrack-on-ignore in git_safety (issue: .beads stayed tracked).

Adding a path to ``.gitignore`` is a no-op if the path was committed before the
ignore line existed. The git-safety sweep must ``git rm --cached`` such paths so
the ignore takes effect, leaving the working-tree copy in place.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agentshore.core.git_safety import (
    commit_gitignore_if_dirty,
    ensure_gitignore_entries,
    untrack_ignored_entries,
)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")


def test_untracks_already_committed_beads(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    # Commit a .beads file BEFORE it is ignored — the regression scenario.
    beads = repo / ".beads"
    beads.mkdir()
    (beads / "graph.db").write_text("data")
    _git(repo, "add", ".beads/graph.db")
    _git(repo, "commit", "-q", "-m", "seed beads")
    assert _git(repo, "ls-files", ".beads").strip(), "precondition: .beads tracked"

    ensure_gitignore_entries(repo)
    untracked = untrack_ignored_entries(repo)
    committed = commit_gitignore_if_dirty(repo)

    assert ".beads/" in untracked
    assert committed is True
    # No longer tracked, but the working-tree copy survives (--cached).
    assert _git(repo, "ls-files", ".beads").strip() == ""
    assert (beads / "graph.db").exists()
    assert ".beads/" in (repo / ".gitignore").read_text()
    # Clean tree after the commit (the removal + .gitignore are committed).
    assert _git(repo, "status", "--porcelain").strip() == ""


def test_idempotent_when_nothing_tracked(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    (repo / "README.md").write_text("hi")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")

    ensure_gitignore_entries(repo)
    untracked = untrack_ignored_entries(repo)
    # Nothing was tracked, so nothing to untrack; only .gitignore was created.
    assert untracked == []
    committed = commit_gitignore_if_dirty(repo)
    assert committed is True  # the new .gitignore is committed

    # Second pass is a clean no-op.
    assert ensure_gitignore_entries(repo) == []
    assert untrack_ignored_entries(repo) == []
    assert commit_gitignore_if_dirty(repo) is False
