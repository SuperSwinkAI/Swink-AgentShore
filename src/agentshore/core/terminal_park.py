"""Terminal-park policy: mark PRs manual-required and issues needs-human."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.core.helpers import _logger
from agentshore.github.labels import (
    MANUAL_REQUIRED_LABEL,
    NEEDS_HUMAN_LABEL,
)

if TYPE_CHECKING:
    from agentshore.data.store import DataStore
    from agentshore.github.adapter import GitHubAdapter


_UNBLOCK_MANUAL_REQUIRED_MARKERS: tuple[str, ...] = (
    "forbidden by skill policy",
    "ci-change",
    "human maintainer",
    "manual maintainer",
    "not fixable in code",
    "infrastructure failures",
    "external ci",
    "ci config or infrastructure",
)

_WRITE_PLAN_UNPLANNABLE_MARKERS: tuple[str, ...] = (
    "too ambiguous",
    "too large",
    "too broad",
    "cannot produce a plan",
    "cannot be planned",
    "unable to plan",
    "needs human",
    "needs decomposition",
    "must be split",
    "requires human",
)


class TerminalParkPolicy:
    """Applies terminal-park labels to PRs and issues that need human intervention.

    Extracted from ``CompletionProcessor`` — all behaviour is verbatim.
    Constructed inside ``CompletionProcessor.__init__`` from the already-
    injected deps; ``CompletionProcessor.mark_pr_manual_required`` and
    ``mark_issue_needs_human`` delegate here.
    """

    def __init__(
        self,
        *,
        store: DataStore,
        session_id: str,
        github_api: GitHubAdapter | None,
    ) -> None:
        self._store = store
        self._session_id = session_id
        self._github = github_api

    async def mark_pr_manual_required(self, pr_number: int) -> None:
        """Persist a terminal manual gate after repeated unblock_pr failures."""
        await self._store.add_pull_request_labels(
            self._session_id,
            pr_number,
            [MANUAL_REQUIRED_LABEL],
        )
        if self._github is not None:
            await self._github.label_issue(
                pr_number,
                [MANUAL_REQUIRED_LABEL],
                f"manual_required:pr{pr_number}",
            )
        _logger.warning(
            "pr_manual_required",
            session_id=self._session_id,
            pr_number=pr_number,
            label=MANUAL_REQUIRED_LABEL,
        )

    async def mark_issue_needs_human(self, issue_number: int) -> None:
        """Park an un-plannable issue behind NEEDS_HUMAN_LABEL (store + GitHub)."""
        await self._store.add_issue_labels(
            issue_number,
            self._session_id,
            [NEEDS_HUMAN_LABEL],
        )
        if self._github is not None:
            await self._github.label_issue(
                issue_number,
                [NEEDS_HUMAN_LABEL],
                f"needs_human:issue{issue_number}",
            )
        _logger.warning(
            "issue_needs_human",
            session_id=self._session_id,
            issue_number=issue_number,
            label=NEEDS_HUMAN_LABEL,
        )
