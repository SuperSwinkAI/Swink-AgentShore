"""Shared fixtures for the ``tests/agents/worktree/`` suite.

We rely on a *real* SQLite database (aiosqlite ``:memory:`` / temp file) and
a *real* git repo materialised in ``tmp_path``. Mocking SQLite or git would
let schema bugs through silently.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from agentshore.data.store import DataStore


async def _git(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    """Synchronous git wrapper via ``asyncio.create_subprocess_exec`` (real git)."""
    full_env = os.environ.copy()
    full_env.setdefault("GIT_AUTHOR_NAME", "AgentShore Test")
    full_env.setdefault("GIT_AUTHOR_EMAIL", "test@agentshore.example")
    full_env.setdefault("GIT_COMMITTER_NAME", "AgentShore Test")
    full_env.setdefault("GIT_COMMITTER_EMAIL", "test@agentshore.example")
    full_env.setdefault("GIT_CONFIG_GLOBAL", "/dev/null")
    full_env.setdefault("GIT_CONFIG_SYSTEM", "/dev/null")
    if env:
        full_env.update(env)
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        env=full_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    if proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): "
            f"{stderr_b.decode(errors='replace')}"
        )
    return stdout_b.decode("utf-8", errors="replace")


def _git_sync(*args: str, cwd: Path | None = None) -> str:
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "AgentShore Test")
    env.setdefault("GIT_AUTHOR_EMAIL", "test@agentshore.example")
    env.setdefault("GIT_COMMITTER_NAME", "AgentShore Test")
    env.setdefault("GIT_COMMITTER_EMAIL", "test@agentshore.example")
    env.setdefault("GIT_CONFIG_GLOBAL", "/dev/null")
    env.setdefault("GIT_CONFIG_SYSTEM", "/dev/null")
    return subprocess.check_output(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        text=True,
        stderr=subprocess.STDOUT,
    )


@pytest.fixture
def fake_remote_repo(tmp_path: Path) -> Path:
    """A bare git repo with one commit on ``main``, usable as ``origin``."""
    remote_dir = tmp_path / "remote.git"
    remote_dir.mkdir()
    _git_sync("init", "--bare", "--initial-branch=main", cwd=remote_dir)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git_sync("init", "--initial-branch=main", cwd=seed)
    _git_sync("config", "commit.gpgsign", "false", cwd=seed)
    (seed / "README.md").write_text("# seed\n")
    _git_sync("add", "README.md", cwd=seed)
    _git_sync("commit", "-m", "initial", cwd=seed)
    _git_sync("remote", "add", "origin", str(remote_dir), cwd=seed)
    _git_sync("push", "-u", "origin", "main", cwd=seed)
    return remote_dir


@pytest.fixture
def main_repo(tmp_path: Path, fake_remote_repo: Path) -> Path:
    """A working clone of ``fake_remote_repo`` — used as the dispatcher CWD."""
    clone = tmp_path / "main"
    _git_sync("clone", str(fake_remote_repo), str(clone))
    _git_sync("config", "commit.gpgsign", "false", cwd=clone)
    return clone


@pytest.fixture
def worktree_root(tmp_path: Path) -> Path:
    root = tmp_path / "agentshore-worktrees"
    root.mkdir()
    return root


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[DataStore]:
    """Real DataStore backed by a temp SQLite file."""
    db_path = tmp_path / "wt-foundation.db"
    s = DataStore(db_path)
    await s.initialize()
    # Seed a session so the FK on worktrees(session_id) is satisfied.
    await s._conn.execute(
        "INSERT INTO sessions (session_id, project_path, started_at) "
        "VALUES ('sess-1', '/tmp/proj', '2026-05-21T00:00:00+00:00')"
    )
    await s._conn.execute(
        "INSERT INTO sessions (session_id, project_path, started_at) "
        "VALUES ('sess-other', '/tmp/proj', '2026-05-21T00:00:00+00:00')"
    )
    await s._conn.commit()
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def remote_branch(fake_remote_repo: Path, tmp_path: Path) -> str:
    """Create an additional ``feature/x`` branch on the fake remote."""
    work = tmp_path / "branch-seed"
    _git_sync("clone", str(fake_remote_repo), str(work))
    _git_sync("config", "commit.gpgsign", "false", cwd=work)
    _git_sync("checkout", "-b", "feature/x", cwd=work)
    (work / "feature.txt").write_text("feature\n")
    _git_sync("add", "feature.txt", cwd=work)
    _git_sync("commit", "-m", "feature commit", cwd=work)
    _git_sync("push", "-u", "origin", "feature/x", cwd=work)
    return "feature/x"
