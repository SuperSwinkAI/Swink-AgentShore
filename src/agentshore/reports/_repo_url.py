"""Repo-URL resolution helpers (subprocess I/O + normalization).

Extracted from ``reports/collector.py`` (TNQA 10 H1) — the only non-pure code
in the collector; separated so the aggregation modules stay side-effect-free.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentshore.data.store import GitHubIssueRecord


def _repo_url_from_github_child_url(url: str) -> str | None:
    match = re.match(r"^(https://github\.com/[^/]+/[^/]+)/(?:issues|pull)/\d+", url)
    if match is None:
        return None
    return match.group(1)


async def _git_remote_url(project_path: str) -> str | None:
    # stdin=DEVNULL: a git child must never inherit the sidecar's stdin (the live
    # Tauri JSON-RPC pipe) -- Git-for-Windows' MSYS2 runtime wedges at 0 CPU
    # probing that contended pipe.
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            project_path,
            "config",
            "--get",
            "remote.origin.url",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    remote = stdout.decode(errors="replace").strip()
    return remote or None


def _normalize_repo_url(remote: str) -> str | None:
    value = remote.removesuffix(".git")
    if value.startswith("git@github.com:"):
        return "https://github.com/" + value.removeprefix("git@github.com:")
    if value.startswith("ssh://git@github.com/"):
        return "https://github.com/" + value.removeprefix("ssh://git@github.com/")
    if value.startswith("https://") or value.startswith("http://"):
        return value
    return None


async def resolve_repo_url(
    project_path: str,
    issues: Sequence[GitHubIssueRecord],
) -> str | None:
    """Return the best report link for the repository, using local and GitHub data."""
    for issue in issues:
        if issue.url:
            repo_url = _repo_url_from_github_child_url(issue.url)
            if repo_url is not None:
                return repo_url

    remote = await _git_remote_url(project_path)
    if remote is None:
        return None
    return _normalize_repo_url(remote)
