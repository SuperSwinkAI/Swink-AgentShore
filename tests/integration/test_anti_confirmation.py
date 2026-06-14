"""Anti-confirmation bias integration: reviewers must not be PR authors."""

from __future__ import annotations

import pytest

from agentshore.agents._selection import select_agent_for
from agentshore.errors import AntiConfirmationViolation
from agentshore.state import AgentStatus, AgentType, PlayType

# ---------------------------------------------------------------------------
# Minimal AgentHandle stub
# ---------------------------------------------------------------------------


class _FakeHandle:
    """Minimal stand-in for AgentHandle used by select_agent_for."""

    def __init__(
        self,
        agent_id: str,
        agent_type: AgentType,
        model_tier: str | None = "medium",
        github_identity: str | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.status = AgentStatus.IDLE
        self.task_history: list[object] = []
        self.timeout_count = 0
        self.consecutive_timeouts = 0
        self.model_tier = model_tier
        self.github_identity = github_identity


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_code_review_excludes_pr_author() -> None:
    """select_agent_for never assigns the PR author as reviewer (identity-based)."""
    alice = _FakeHandle("alice", AgentType.CLAUDE_CODE, github_identity="alice-login")
    bob = _FakeHandle("bob", AgentType.CODEX, github_identity="bob-login")
    handles = {"alice": alice, "bob": bob}  # type: ignore[dict-item]

    selected = select_agent_for(
        PlayType.CODE_REVIEW,
        handles,  # type: ignore[arg-type]
        pr_github_author="alice-login",
    )
    assert selected.agent_id == "bob"


def test_run_qa_does_not_exclude_last_implementer() -> None:
    """RUN_QA has no anti-confirmation: it runs against the merged trunk,
    so the large-tier agent that last committed on a branch is just as eligible
    as any other large can_test agent. Soft cluster-affinity scoring keeps that
    exposure as a *preference* rather than an exclusion."""
    alice = _FakeHandle(
        "alice",
        AgentType.CLAUDE_CODE,
        model_tier="large",
        github_identity="alice-login",
    )
    handles = {"alice": alice}  # type: ignore[dict-item]

    branch_exposure = {"feat-x": "alice"}

    selected = select_agent_for(
        PlayType.RUN_QA,
        handles,  # type: ignore[arg-type]
        branch_exposure=branch_exposure,
        branch="feat-x",
    )
    assert selected.agent_id == "alice"


def test_anti_confirmation_raises_when_all_blocked() -> None:
    """AntiConfirmationViolation is raised when every agent is blocked."""
    alice = _FakeHandle("alice", AgentType.CLAUDE_CODE, github_identity="alice-login")
    handles = {"alice": alice}  # type: ignore[dict-item]

    with pytest.raises(AntiConfirmationViolation):
        select_agent_for(
            PlayType.CODE_REVIEW,
            handles,  # type: ignore[arg-type]
            pr_github_author="alice-login",
        )


def test_code_review_unblock_pr_stamp_does_not_block_different_identity() -> None:
    """An unblock_pr that ran on behalf of 'bob' should not block 'bob' from
    reviewing a PR authored by 'alice'. Only alice's identity is excluded."""
    alice = _FakeHandle("alice", AgentType.CLAUDE_CODE, github_identity="alice-login")
    bob = _FakeHandle("bob", AgentType.CODEX, github_identity="bob-login")
    handles = {"alice": alice, "bob": bob}  # type: ignore[dict-item]

    # Simulates: GH author is alice; codex (bob) did an unblock_pr on this PR.
    # Under the old agent_id-based system, bob would be wrongly blocked.
    # Under the identity-based system, only alice is blocked.
    selected = select_agent_for(
        PlayType.CODE_REVIEW,
        handles,  # type: ignore[arg-type]
        pr_github_author="alice-login",
    )
    assert selected.agent_id == "bob"
