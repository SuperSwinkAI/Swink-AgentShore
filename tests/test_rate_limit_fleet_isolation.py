"""desktop-ctnl regression: rate_limit on one gemini agent must not freeze the fleet.

After desktop-rni0 IDLE_TICK / RECOVER are no longer in the policy head, so PPO
cannot learn to prefer idling under rate_limit. The mask invariant the test
pins: rate_limit on a single gemini agent only zeros gemini's slots in
``compute_agent_eligibility_mask``; claude_code / codex slots stay selectable
for plays whose preconditions are otherwise met.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agentshore.plays.registry import build_default_registry
from agentshore.rl.action_space import PLAY_TO_INDEX
from agentshore.rl.mask import compute_agent_eligibility_mask
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    OrchestratorState,
    PlayType,
    PullRequestSnapshot,
    SessionState,
)


def _agent(
    *,
    agent_id: str,
    agent_type: AgentType,
    status: AgentStatus = AgentStatus.IDLE,
    last_error_class: str | None = None,
    model_tier: str = "medium",
) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=agent_type,
        status=status,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
        model_tier=model_tier,
        last_error_class=last_error_class,
    )


def _state(agents: list[AgentSnapshot]) -> OrchestratorState:
    return OrchestratorState(
        session_id="s",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=agents,
    )


def _cfg() -> MagicMock:
    cfg = MagicMock()
    cfg.agent_preferences.exclude = {}
    return cfg


def test_rate_limit_on_gemini_does_not_zero_claude_codex_eligibility() -> None:
    """A single gemini agent in rate_limit ERROR must leave claude/codex slots open."""
    agents = [
        _agent(
            agent_id="gem-1",
            agent_type=AgentType.GEMINI,
            status=AgentStatus.ERROR,
            last_error_class="rate_limit",
        ),
        _agent(agent_id="cl-1", agent_type=AgentType.CLAUDE_CODE, model_tier="large"),
        _agent(agent_id="cx-1", agent_type=AgentType.CODEX, model_tier="medium"),
    ]
    state = _state(agents)
    registry = build_default_registry()

    mask = compute_agent_eligibility_mask(state, registry, cfg=_cfg())

    # Work plays that claude_code or codex are capable of must remain eligible
    # — the rate_limit on the gemini agent must not collapse the whole fleet.
    # CODE_REVIEW additionally requires a PR snapshot for anti-confirmation
    # gating, so we cover it in the dedicated test below.
    for play in (PlayType.ISSUE_PICKUP, PlayType.RUN_QA, PlayType.WRITE_IMPLEMENTATION_PLAN):
        assert mask[PLAY_TO_INDEX[play]], f"{play.value} was zeroed by gemini rate_limit"


def test_rate_limit_does_not_zero_code_review_when_a_reviewable_pr_exists() -> None:
    """A rate_limited gemini agent must not block code_review for claude/codex agents.

    With a PR snapshot whose ``github_author`` differs from the healthy agents'
    identities, the anti-confirmation gate accepts at least one (agent, PR)
    pair — so the slot stays selectable.
    """
    agents = [
        _agent(
            agent_id="gem-1",
            agent_type=AgentType.GEMINI,
            status=AgentStatus.ERROR,
            last_error_class="rate_limit",
        ),
        _agent(agent_id="cl-1", agent_type=AgentType.CLAUDE_CODE),
        _agent(agent_id="cx-1", agent_type=AgentType.CODEX),
    ]
    pr = PullRequestSnapshot(
        pr_number=1,
        title="t",
        state="open",
        branch="feat",
        issue_number=None,
        labels=[],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
        github_author="someone-else",
    )
    state = OrchestratorState(
        session_id="s",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=agents,
        pull_requests=[pr],
    )
    registry = build_default_registry()

    mask = compute_agent_eligibility_mask(state, registry, cfg=_cfg())

    assert mask[PLAY_TO_INDEX[PlayType.CODE_REVIEW]], (
        "code_review was zeroed despite a reviewable PR and healthy non-gemini agents"
    )


def test_rate_limit_excludes_only_same_type_candidates() -> None:
    """If all gemini agents are rate_limited but other types are healthy, gemini-only
    candidacy is the only thing that disappears."""
    agents = [
        _agent(
            agent_id="gem-1",
            agent_type=AgentType.GEMINI,
            status=AgentStatus.ERROR,
            last_error_class="rate_limit",
        ),
        _agent(
            agent_id="gem-2",
            agent_type=AgentType.GEMINI,
            status=AgentStatus.ERROR,
            last_error_class="rate_limit",
        ),
        _agent(agent_id="cl-1", agent_type=AgentType.CLAUDE_CODE, model_tier="large"),
    ]
    state = _state(agents)
    registry = build_default_registry()

    mask = compute_agent_eligibility_mask(state, registry, cfg=_cfg())

    assert mask[PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]]
    assert mask[PLAY_TO_INDEX[PlayType.RUN_QA]]
    assert mask[PLAY_TO_INDEX[PlayType.WRITE_IMPLEMENTATION_PLAN]]
