"""Trusted GitHub PR author filtering."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.agents.identity import IdentityResolutionError, resolved_github_login_for_agent
from agentshore.identity_names import canonical_identity_name
from agentshore.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Collection

    from agentshore.config.models import RuntimeConfig
    from agentshore.data.models import PullRequestRecord

_logger = get_logger(__name__)


def trusted_pr_author_logins(cfg: RuntimeConfig) -> frozenset[str]:
    """Return GitHub logins whose PRs AgentShore may track."""

    trusted: set[str] = set(cfg.trusted_ids.github_logins)
    for agent_key, agent_cfg in cfg.agents.items():
        if not agent_cfg.enabled:
            continue
        if not agent_cfg.identity:
            _logger.warning(
                "trusted_pr_identity_unresolved",
                agent_key=agent_key,
                reason="no_identity",
            )
            continue
        try:
            login = resolved_github_login_for_agent(cfg, agent_cfg)
        except IdentityResolutionError as exc:
            _logger.warning(
                "trusted_pr_identity_unresolved",
                agent_key=agent_key,
                identity=agent_cfg.identity,
                reason=str(exc),
            )
            continue
        if login is None:
            _logger.warning(
                "trusted_pr_identity_unresolved",
                agent_key=agent_key,
                identity=agent_cfg.identity,
                reason="no_resolved_login",
            )
            continue
        trusted.add(canonical_identity_name(login))
    return frozenset(trusted)


def trusted_issue_author_logins(cfg: RuntimeConfig) -> frozenset[str]:
    """GitHub logins whose issues AgentShore may pick up when issue-author
    enforcement is enabled. Shares the PR trusted set (configured logins ∪ the
    enabled agents' own resolved identities) so AgentShore never ignores issues
    it filed itself."""
    return trusted_pr_author_logins(cfg)


def filter_trusted_pull_requests(
    pull_requests: Collection[PullRequestRecord],
    cfg: RuntimeConfig,
    *,
    trusted_authors: Collection[str] | None = None,
    context: str,
) -> list[PullRequestRecord]:
    """Return only PRs authored by trusted GitHub logins."""

    trusted = (
        frozenset(canonical_identity_name(author) for author in trusted_authors)
        if trusted_authors is not None
        else trusted_pr_author_logins(cfg)
    )
    pr_allow_list = frozenset(cfg.trusted_ids.pr_allow_list)
    filtered: list[PullRequestRecord] = []
    for pr in pull_requests:
        author = canonical_identity_name(pr.github_author) if pr.github_author else None
        if author is not None and author in trusted:
            filtered.append(pr)
            continue
        if pr.pr_number in pr_allow_list:
            filtered.append(pr)
            _logger.info(
                "github_pull_request_allowlisted",
                pr_number=pr.pr_number,
                author=pr.github_author,
                title=pr.title,
                context=context,
            )
            continue
        _logger.info(
            "github_pull_request_ignored",
            reason="untrusted_author",
            pr_number=pr.pr_number,
            author=pr.github_author,
            title=pr.title,
            context=context,
        )
    return filtered
