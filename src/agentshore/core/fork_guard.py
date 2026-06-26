"""Detect cross-fork PR artifacts and non-origin push remotes after play completion.

Detectors are pure (sync) or best-effort async helpers. They never raise — callers
wrap them in ``_safe_call`` so a detector failure cannot affect completion.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


@dataclass(frozen=True)
class ForkFinding:
    """A single detection hit from the fork-guard detectors."""

    kind: str  # "cross_fork_pr" | "non_origin_remote"
    detail: str


def parse_origin_owner(remote_or_nwo: str) -> str | None:
    """Return the GitHub owner (org/user) from a remote URL or owner/repo string.

    Accepts:
    - ``https://github.com/owner/repo(.git)``
    - ``git@github.com:owner/repo(.git)``
    - ``owner/repo``

    Returns the owner string (not casefolded — callers compare with casefold).
    Returns ``None`` when the input cannot be parsed.
    """
    value = remote_or_nwo.strip()
    if not value:
        return None

    # SSH short form: git@github.com:owner/repo
    if value.startswith("git@github.com:"):
        path = value.removeprefix("git@github.com:").removesuffix(".git").strip("/")
        owner, sep, _ = path.partition("/")
        return owner if sep else None

    # HTTPS / HTTP URL
    parsed = urllib.parse.urlparse(value)
    host = (parsed.hostname or "").casefold()
    if host == "github.com":
        path = urllib.parse.unquote(parsed.path).strip("/").removesuffix(".git")
        owner, sep, _ = path.partition("/")
        return owner if sep else None

    # Plain "owner/repo" string (no scheme, no host)
    if "/" in value and not value.startswith("http"):
        owner, sep, _ = value.partition("/")
        return owner if sep else None

    return None


def detect_cross_fork_pr_artifacts(
    artifacts: Sequence[object],
    origin_owner: str,
) -> list[ForkFinding]:
    """Return findings for any PR artifact whose owner != *origin_owner*.

    *artifacts* is the ``PlayOutcome.artifacts`` list — items may be any JSON
    value; only dicts with ``type == "pr"`` and a parseable ``url`` are checked.
    Non-PR artifacts and unparseable URLs are silently skipped.
    """
    findings: list[ForkFinding] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        if artifact.get("type") != "pr":
            continue
        url = artifact.get("url")
        if not isinstance(url, str):
            continue
        owner = parse_origin_owner(url)
        if owner is None:
            continue
        if owner.casefold() != origin_owner.casefold():
            findings.append(
                ForkFinding(
                    kind="cross_fork_pr",
                    detail=f"PR {url!r} owner {owner!r} != origin owner {origin_owner!r}",
                )
            )
    return findings


async def detect_non_origin_remotes(worktree: Path) -> list[ForkFinding]:
    """Return findings for any git remote in *worktree* whose name != ``origin``.

    Runs ``git remote`` in *worktree* via ``agentshore.command.git`` (the
    project's hardened async git wrapper). Returns ``[]`` on any error so
    the caller's completion path is never blocked.
    """
    try:
        from agentshore import command

        result = await command.git("remote", cwd=worktree)
        if result.returncode != 0 or not result.stdout.strip():
            return []
        remotes = [r.strip() for r in result.stdout.splitlines() if r.strip()]
    except Exception:
        return []

    findings: list[ForkFinding] = []
    for name in remotes:
        if name != "origin":
            findings.append(
                ForkFinding(
                    kind="non_origin_remote",
                    detail=f"unexpected remote {name!r} in worktree {worktree}",
                )
            )
    return findings
