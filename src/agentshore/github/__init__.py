"""GitHub integration — wraps the `gh` CLI for issue/PR operations."""

from __future__ import annotations

from agentshore.github.adapter import GitHubAdapter, GitHubUnavailableError

__all__ = ["GitHubAdapter", "GitHubUnavailableError"]
