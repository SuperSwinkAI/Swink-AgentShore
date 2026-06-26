"""desktop-ctnl regression: rate_limit on one grok agent must not freeze the fleet.

After desktop-rni0 IDLE_TICK / RECOVER are no longer in the policy head, so PPO
cannot learn to prefer idling under rate_limit. The mask invariant the test
pins: rate_limit on a single grok agent only zeros grok's slots in
``compute_agent_eligibility_mask``; claude_code / codex slots stay selectable
for plays whose preconditions are otherwise met.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agentshore.errors import ErrorClass
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
    last_error_class: ErrorClass | None = None,
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


def test_rate_limit_on_grok_does_not_zero_claude_codex_eligibility() -> None:
    """A single grok agent in rate_limit ERROR must leave claude/codex slots open."""
    agents = [
        _agent(
            agent_id="grok-1",
            agent_type=AgentType.GROK,
            status=AgentStatus.ERROR,
            last_error_class=ErrorClass.RATE_LIMIT,
        ),
        _agent(agent_id="cl-1", agent_type=AgentType.CLAUDE_CODE, model_tier="large"),
        _agent(agent_id="cx-1", agent_type=AgentType.CODEX, model_tier="medium"),
    ]
    state = _state(agents)
    registry = build_default_registry()

    mask = compute_agent_eligibility_mask(state, registry, cfg=_cfg())

    # claude/codex work plays must stay eligible — grok's rate_limit must not
    # collapse the fleet. CODE_REVIEW needs a PR snapshot, covered separately below.
    for play in (PlayType.ISSUE_PICKUP, PlayType.RUN_QA, PlayType.WRITE_IMPLEMENTATION_PLAN):
        assert mask[PLAY_TO_INDEX[play]], f"{play.value} was zeroed by grok rate_limit"


def test_rate_limit_does_not_zero_code_review_when_a_reviewable_pr_exists() -> None:
    """A rate_limited grok agent must not block code_review for claude/codex agents.

    With a PR snapshot whose ``github_author`` differs from the healthy agents'
    identities, the anti-confirmation gate accepts at least one (agent, PR)
    pair — so the slot stays selectable.
    """
    agents = [
        _agent(
            agent_id="grok-1",
            agent_type=AgentType.GROK,
            status=AgentStatus.ERROR,
            last_error_class=ErrorClass.RATE_LIMIT,
        ),
        # code_review is large-only (#254); the healthy reviewers must be large.
        _agent(agent_id="cl-1", agent_type=AgentType.CLAUDE_CODE, model_tier="large"),
        _agent(agent_id="cx-1", agent_type=AgentType.CODEX, model_tier="large"),
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
        "code_review was zeroed despite a reviewable PR and healthy non-grok agents"
    )


def test_rate_limit_excludes_only_same_type_candidates() -> None:
    """If all grok agents are rate_limited but other types are healthy, grok-only
    candidacy is the only thing that disappears."""
    agents = [
        _agent(
            agent_id="grok-1",
            agent_type=AgentType.GROK,
            status=AgentStatus.ERROR,
            last_error_class=ErrorClass.RATE_LIMIT,
        ),
        _agent(
            agent_id="grok-2",
            agent_type=AgentType.GROK,
            status=AgentStatus.ERROR,
            last_error_class=ErrorClass.RATE_LIMIT,
        ),
        _agent(agent_id="cl-1", agent_type=AgentType.CLAUDE_CODE, model_tier="large"),
    ]
    state = _state(agents)
    registry = build_default_registry()

    mask = compute_agent_eligibility_mask(state, registry, cfg=_cfg())

    assert mask[PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]]
    assert mask[PLAY_TO_INDEX[PlayType.RUN_QA]]
    assert mask[PLAY_TO_INDEX[PlayType.WRITE_IMPLEMENTATION_PLAN]]


# #277: auth-suppression must mask late-resolved plays too. Unlike rate_limit, an
# auth-failed agent returns to IDLE (process healthy, only the backend token dead),
# so the IDLE gate alone never excludes it. The mask must drop the auth-suppressed
# TYPE explicitly, mirroring select_agent_for.


def _state_with_auth_suppressed(
    agents: list[AgentSnapshot], suppressed: set[str]
) -> OrchestratorState:
    return OrchestratorState(
        session_id="s",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=agents,
        auth_suppressed_agent_types=frozenset(suppressed),
    )


def test_auth_suppressed_grok_zeroes_grok_only_plays_even_while_idle() -> None:
    """A grok type in ``auth_suppressed_agent_types`` is masked for issue_pickup
    even though its handle is still IDLE — this is the #277 late-resolved gap."""
    agents = [
        _agent(agent_id="grok-1", agent_type=AgentType.GROK, status=AgentStatus.IDLE),
    ]
    state = _state_with_auth_suppressed(agents, {"grok"})
    registry = build_default_registry()

    mask = compute_agent_eligibility_mask(state, registry, cfg=_cfg())

    # The only idle agent is an auth-suppressed grok: issue_pickup must be masked
    # rather than left selectable to fail at runner selection every tick.
    assert not mask[PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]]


def test_auth_suppressed_type_does_not_collapse_healthy_types() -> None:
    """An auth-suppressed grok must not zero plays a healthy claude/codex can run."""
    agents = [
        _agent(agent_id="grok-1", agent_type=AgentType.GROK, status=AgentStatus.IDLE),
        _agent(agent_id="cl-1", agent_type=AgentType.CLAUDE_CODE, model_tier="large"),
        _agent(agent_id="cx-1", agent_type=AgentType.CODEX, model_tier="medium"),
    ]
    state = _state_with_auth_suppressed(agents, {"grok"})
    registry = build_default_registry()

    mask = compute_agent_eligibility_mask(state, registry, cfg=_cfg())

    for play in (PlayType.ISSUE_PICKUP, PlayType.RUN_QA, PlayType.WRITE_IMPLEMENTATION_PLAN):
        assert mask[PLAY_TO_INDEX[play]], f"{play.value} was zeroed by grok auth-suppression"
