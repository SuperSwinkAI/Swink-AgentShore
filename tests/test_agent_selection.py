"""Tests for the agent selection rule chain (_selection.py)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from agentshore.agents._selection import select_agent_for
from agentshore.agents.handle import AgentHandle, TaskRecord
from agentshore.config import AgentPreferencesConfig
from agentshore.errors import AntiConfirmationViolation
from agentshore.state import AgentStatus, AgentType, PlayType


def _make_handle(
    agent_id: str = "a1",
    agent_type: AgentType = AgentType.CLAUDE_CODE,
    status: AgentStatus = AgentStatus.IDLE,
    tasks: int = 0,
    model_tier: str | None = None,
    github_identity: str | None = None,
) -> AgentHandle:
    h = AgentHandle(
        agent_id=agent_id,
        agent_type=agent_type,
        status=status,
        working_dir=Path("/tmp"),
        model_tier=model_tier,
        github_identity=github_identity,
    )
    for i in range(tasks):
        h.add_task(
            TaskRecord(
                play_id=f"play-{i}",
                play_type=PlayType.ISSUE_PICKUP,
                success=True,
            )
        )
    return h


def _handles(*agents: AgentHandle) -> dict[str, AgentHandle]:
    return {h.agent_id: h for h in agents}


def test_no_idle_agents_raises() -> None:
    busy = _make_handle("a1", status=AgentStatus.BUSY)
    with pytest.raises(AntiConfirmationViolation, match="No IDLE agents"):
        select_agent_for(PlayType.ISSUE_PICKUP, _handles(busy))


# Circuit breaker (#22): deprioritize a known-dead agent.
def test_is_agent_circuit_broken_predicate() -> None:
    from agentshore.state import is_agent_circuit_broken

    # Healthy: any success clears it, regardless of failures/timeouts.
    assert not is_agent_circuit_broken(tasks_completed=1, tasks_failed=5, timeout_count=9)
    # Fresh agent with no history is not broken.
    assert not is_agent_circuit_broken(tasks_completed=0, tasks_failed=0, timeout_count=0)
    # One non-timeout failure is below the limit.
    assert not is_agent_circuit_broken(tasks_completed=0, tasks_failed=1, timeout_count=0)
    # A single timeout with 0 successes trips it.
    assert is_agent_circuit_broken(tasks_completed=0, tasks_failed=0, timeout_count=1)
    # Two failures with 0 successes trips it.
    assert is_agent_circuit_broken(tasks_completed=0, tasks_failed=2, timeout_count=0)


def _broken_handle(agent_id: str) -> AgentHandle:
    """A handle with 0 successful tasks and a dispatch timeout — circuit-broken."""
    h = _make_handle(agent_id, tasks=0)
    h.timeout_count = 1
    h.add_task(TaskRecord(play_id="p0", play_type=PlayType.ISSUE_PICKUP, success=False))
    return h


def test_circuit_broken_agent_deprioritized_in_favor_of_healthy() -> None:
    broken = _broken_handle("dead")
    healthy = _make_handle("ok", tasks=1)  # one successful task → healthy
    result = select_agent_for(PlayType.ISSUE_PICKUP, _handles(broken, healthy))
    assert result.agent_id == "ok"


def test_circuit_broken_agent_still_selected_when_only_option() -> None:
    """Soft, not hard: if every IDLE candidate is broken we still pick one
    rather than wedge — the play-availability gate is the hard mask."""
    broken = _broken_handle("dead")
    result = select_agent_for(PlayType.ISSUE_PICKUP, _handles(broken))
    assert result.agent_id == "dead"


# Anti-confirmation: CodeReview reviewer must differ from PR author.
def test_code_review_excludes_pr_author_by_github_identity() -> None:
    # code_review is large-only (#254); these identity tests use large reviewers.
    author = _make_handle("author", model_tier="large", github_identity="alice")
    reviewer = _make_handle("reviewer", model_tier="large", github_identity="bob")

    result = select_agent_for(
        PlayType.CODE_REVIEW,
        _handles(author, reviewer),
        pr_github_author="alice",
    )
    assert result.agent_id == "reviewer"


def test_code_review_identity_filter_is_case_insensitive() -> None:
    author = _make_handle("author", model_tier="large", github_identity="bot-user")
    reviewer = _make_handle("reviewer", model_tier="large", github_identity="example-user")

    result = select_agent_for(
        PlayType.CODE_REVIEW,
        _handles(author, reviewer),
        pr_github_author="bot-user",
    )
    assert result.agent_id == "reviewer"


def test_code_review_no_pr_github_author_does_not_filter() -> None:
    author = _make_handle("author", model_tier="large", github_identity="alice")

    # No pr_github_author → anti-confirmation filter doesn't apply.
    result = select_agent_for(
        PlayType.CODE_REVIEW,
        _handles(author),
    )
    assert result.agent_id == "author"


def test_code_review_all_blocked_raises() -> None:
    only = _make_handle("only", github_identity="alice")
    with pytest.raises(AntiConfirmationViolation):
        select_agent_for(
            PlayType.CODE_REVIEW,
            _handles(only),
            pr_github_author="alice",
        )


def test_code_review_different_identity_not_blocked() -> None:
    author = _make_handle("author", model_tier="large", github_identity="alice")
    reviewer = _make_handle("reviewer", model_tier="large", github_identity="bob")

    result = select_agent_for(
        PlayType.CODE_REVIEW,
        _handles(author, reviewer),
        pr_github_author="alice",
    )
    assert result.agent_id == "reviewer"


def test_code_review_agent_without_github_identity_not_blocked() -> None:
    # An agent with no resolved github_identity matches no author.
    no_id = _make_handle("no-id", model_tier="large", github_identity=None)
    result = select_agent_for(
        PlayType.CODE_REVIEW,
        _handles(no_id),
        pr_github_author="alice",
    )
    assert result.agent_id == "no-id"


# RUN_QA: no anti-confirmation — runs against the merged trunk.
def test_run_qa_picks_last_branch_implementer_when_offered() -> None:
    """QA has no anti-confirmation; the last implementer is just as eligible
    as anyone else (QA exercises the merged trunk, not a specific commit)."""
    implementer = _make_handle("impl", model_tier="large")

    result = select_agent_for(
        PlayType.RUN_QA,
        _handles(implementer),
        branch_exposure={"feature/x": "impl"},
        branch="feature/x",
    )
    assert result.agent_id == "impl"


def test_run_qa_no_branch_does_not_filter() -> None:
    impl = _make_handle("impl", model_tier="large")

    result = select_agent_for(
        PlayType.RUN_QA,
        _handles(impl),
        branch_exposure={"feature/x": "impl"},
    )
    assert result.agent_id == "impl"


def test_exclude_list_removes_matching_agent_type() -> None:
    codex = _make_handle("codex-1", AgentType.CODEX)
    claude = _make_handle("claude-1", AgentType.CLAUDE_CODE)
    prefs = AgentPreferencesConfig(
        exclude={"issue_pickup": ["codex"]},
    )

    result = select_agent_for(
        PlayType.ISSUE_PICKUP,
        _handles(codex, claude),
        preferences=prefs,
    )
    assert result.agent_id == "claude-1"


def test_exclude_list_different_play_type_not_excluded() -> None:
    codex = _make_handle("codex-1", AgentType.CODEX)
    prefs = AgentPreferencesConfig(
        exclude={"code_review": ["codex"]},  # only excluded from code_review
    )

    result = select_agent_for(
        PlayType.ISSUE_PICKUP,  # different play type
        _handles(codex),
        preferences=prefs,
    )
    assert result.agent_id == "codex-1"


def test_all_excluded_raises() -> None:
    codex = _make_handle("c1", AgentType.CODEX)
    prefs = AgentPreferencesConfig(exclude={"issue_pickup": ["codex"]})
    with pytest.raises(AntiConfirmationViolation):
        select_agent_for(PlayType.ISSUE_PICKUP, _handles(codex), preferences=prefs)


# Auth-suppression (#277): a backend-auth-failed type is benched for the session.
# Its handle returns to IDLE, so the selector must drop it explicitly or a
# late-resolved play keeps re-dispatching the dead backend.
def test_auth_suppressed_type_excluded_from_selection() -> None:
    grok = _make_handle("grok-1", AgentType.GROK)
    claude = _make_handle("claude-1", AgentType.CLAUDE_CODE)

    result = select_agent_for(
        PlayType.ISSUE_PICKUP,
        _handles(grok, claude),
        auth_suppressed_types=frozenset({"grok"}),
    )
    assert result.agent_id == "claude-1"


def test_auth_suppressed_only_type_raises() -> None:
    grok = _make_handle("grok-1", AgentType.GROK)
    with pytest.raises(AntiConfirmationViolation, match="auth-suppressed"):
        select_agent_for(
            PlayType.ISSUE_PICKUP,
            _handles(grok),
            auth_suppressed_types=frozenset({"grok"}),
        )


def test_auth_suppressed_empty_set_is_noop() -> None:
    grok = _make_handle("grok-1", AgentType.GROK)
    result = select_agent_for(PlayType.ISSUE_PICKUP, _handles(grok))
    assert result.agent_id == "grok-1"


def test_type_affinity_promotes_preferred_type() -> None:
    grok = _make_handle("g1", AgentType.GROK, model_tier="large")
    claude = _make_handle("c1", AgentType.CLAUDE_CODE, model_tier="large")
    prefs = AgentPreferencesConfig(affinity={"code_review": "grok"})

    result = select_agent_for(
        PlayType.CODE_REVIEW,
        _handles(claude, grok),
        preferences=prefs,
    )
    assert result.agent_id == "g1"


def test_cluster_affinity_promotes_branch_exposed_agent() -> None:
    exposed = _make_handle("exposed")
    fresh = _make_handle("fresh")

    result = select_agent_for(
        PlayType.ISSUE_PICKUP,
        _handles(fresh, exposed),
        branch_exposure={"feature/z": "exposed"},
        branch="feature/z",
    )
    assert result.agent_id == "exposed"


def test_least_busy_agent_preferred() -> None:
    busy = _make_handle("busy", tasks=5)
    idle = _make_handle("idle", tasks=0)

    result = select_agent_for(PlayType.ISSUE_PICKUP, _handles(busy, idle))
    assert result.agent_id == "idle"


# Rule ordering: anti-confirmation applies before soft preferences.
def test_anti_confirmation_overrides_type_affinity() -> None:
    """Even if the author's type is preferred, they must still be excluded."""
    author = _make_handle(
        "author", AgentType.CLAUDE_CODE, model_tier="large", github_identity="alice"
    )
    fallback = _make_handle("fallback", AgentType.CODEX, model_tier="large", github_identity="bob")
    prefs = AgentPreferencesConfig(affinity={"code_review": "claude_code"})

    result = select_agent_for(
        PlayType.CODE_REVIEW,
        _handles(author, fallback),
        pr_github_author="alice",
        preferences=prefs,
    )
    assert result.agent_id == "fallback"


def test_tier_filter_blocks_small_from_coding_play() -> None:
    """Small-tier agents are blocked from issue_pickup (writes code)."""
    small = _make_handle("small-1", model_tier="small")
    medium = _make_handle("medium-1", model_tier="medium")

    result = select_agent_for(PlayType.ISSUE_PICKUP, _handles(small, medium))
    assert result.agent_id == "medium-1"


def test_cleanup_prefers_medium_over_large_when_both_idle() -> None:
    """Cleanup is eligible on all tiers, but soft tier_score prefers medium
    over large so the larger agent stays available for plays that need it."""
    medium = _make_handle("medium-1", model_tier="medium")
    large = _make_handle("large-1", model_tier="large")

    result = select_agent_for(PlayType.CLEANUP, _handles(medium, large))
    assert result.agent_id == "medium-1"


def test_tier_filter_only_small_idle_for_coding_play_raises() -> None:
    """No medium/large IDLE → coding play fails with the merged-rule message."""
    small_only = _make_handle("small-only", model_tier="small")
    with pytest.raises(AntiConfirmationViolation, match="tier-eligibility"):
        select_agent_for(PlayType.ISSUE_PICKUP, _handles(small_only))


def test_cleanup_blocks_large_tier() -> None:
    """Cleanup is cheap mechanical housekeeping (small ∪ medium); large is
    excluded as overkill, mirroring merge_pr/prune.

    Cleanup is no longer a bootstrap first-play — the cold start spawns the full
    enabled fleet, so small/medium agents are present immediately and the old
    large-only bootstrap carve-out (skip:staffing on a large-only fleet, seen
    2026-05-22) no longer applies. A large-only fleet now has no eligible cleanup
    agent."""
    large_only = _make_handle("large-only", model_tier="large")
    with pytest.raises(AntiConfirmationViolation, match="tier-eligibility"):
        select_agent_for(PlayType.CLEANUP, _handles(large_only))


def test_prune_blocks_large_tier() -> None:
    """Prune is mechanical branch/trunk housekeeping (small ∪ medium); large is
    excluded as overkill. A large-only fleet has no eligible prune agent."""
    large_only = _make_handle("large-only", model_tier="large")
    with pytest.raises(AntiConfirmationViolation, match="tier-eligibility"):
        select_agent_for(PlayType.PRUNE, _handles(large_only))

    medium = _make_handle("medium-1", model_tier="medium")
    assert select_agent_for(PlayType.PRUNE, _handles(medium)).agent_id == "medium-1"


def test_merge_pr_blocks_large_tier() -> None:
    """Merge is cheap mechanical work; preserve large agents for harder plays."""
    large_only = _make_handle("large-only", model_tier="large")
    with pytest.raises(AntiConfirmationViolation, match="tier-eligibility"):
        select_agent_for(PlayType.MERGE_PR, _handles(large_only))


def test_merge_pr_uses_medium_before_large() -> None:
    """Medium can land approved PRs while large capacity stays available."""
    medium = _make_handle("medium-1", model_tier="medium")
    large = _make_handle("large-1", model_tier="large")

    result = select_agent_for(PlayType.MERGE_PR, _handles(medium, large))
    assert result.agent_id == "medium-1"


def test_issue_pickup_prefers_medium_before_large_when_other_affinity_ties() -> None:
    """Issue pickup allows large fallback but should consume medium capacity first."""
    medium = _make_handle("medium-1", model_tier="medium")
    large = _make_handle("large-1", model_tier="large")

    result = select_agent_for(PlayType.ISSUE_PICKUP, _handles(medium, large))
    assert result.agent_id == "medium-1"


def test_issue_pickup_branch_affinity_can_override_tier_cost() -> None:
    """An exposed large agent may still be the right handoff for its branch."""
    medium = _make_handle("medium-1", model_tier="medium")
    large = _make_handle("large-1", model_tier="large")

    result = select_agent_for(
        PlayType.ISSUE_PICKUP,
        _handles(medium, large),
        branch_exposure={"feature/x": "large-1"},
        branch="feature/x",
    )
    assert result.agent_id == "large-1"


def test_tier_filter_medium_runs_small_band() -> None:
    """Medium is valid for medium/large plays like ISSUE_PICKUP but not write_plan (large-only)."""
    medium = _make_handle("medium-1", model_tier="medium")
    large = _make_handle("large-1", model_tier="large")

    cheap = select_agent_for(PlayType.CLEANUP, _handles(medium))
    coding = select_agent_for(PlayType.WRITE_IMPLEMENTATION_PLAN, _handles(medium, large))
    assert cheap.agent_id == "medium-1"
    assert coding.agent_id == "large-1"


def test_tier_filter_unknown_tier_treated_as_medium() -> None:
    """Agents with model_tier=None are treated as medium (DEFAULT_MODEL_TIER)."""
    no_tier = _make_handle("notier", model_tier=None)

    # ISSUE_PICKUP is medium ∪ large, so a None-tier (=medium) agent qualifies;
    # this exercises the default-tier coercion. (CODE_REVIEW is large-only — #254.)
    coding = select_agent_for(PlayType.ISSUE_PICKUP, _handles(no_tier))
    cheap = select_agent_for(PlayType.CLEANUP, _handles(no_tier))
    assert coding.agent_id == "notier"
    assert cheap.agent_id == "notier"


def test_tier_filter_picks_large_over_medium_for_write_plan() -> None:
    """write_implementation_plan is large-only; large beats medium regardless of busyness."""
    medium_idle = _make_handle("medium", model_tier="medium", tasks=0)
    large_busy = _make_handle("large", model_tier="large", tasks=10)

    result = select_agent_for(PlayType.WRITE_IMPLEMENTATION_PLAN, _handles(medium_idle, large_busy))
    assert result.agent_id == "large"


def test_tier_filter_requires_large_for_design_audit() -> None:
    """Design audit is a heavyweight design/spec review and requires large tier."""
    small = _make_handle("small-1", model_tier="small")
    large = _make_handle("large-1", model_tier="large", tasks=10)

    result = select_agent_for(PlayType.DESIGN_AUDIT, _handles(small, large))
    assert result.agent_id == "large-1"


def test_tier_filter_requires_large_for_code_review() -> None:
    """#254: code_review is large-only. A medium agent never qualifies; large is picked.

    code_review is AgentShore's heaviest analytical play; a fast/medium model
    overruns the harness's reliable turn-completion envelope (medium Gemini-Flash
    produced clean-exit empty no-ops exclusively on code_review). This is a tier
    capability floor, not a per-harness rule.
    """
    medium = _make_handle("medium-1", model_tier="medium", tasks=0)
    large = _make_handle("large-1", model_tier="large", tasks=10)

    # Even though the medium agent is idle and cheaper, only large qualifies.
    result = select_agent_for(PlayType.CODE_REVIEW, _handles(medium, large))
    assert result.agent_id == "large-1"

    # With no large agent available, code_review has no eligible reviewer at all.
    with pytest.raises(AntiConfirmationViolation):
        select_agent_for(PlayType.CODE_REVIEW, _handles(medium))


def test_blocked_logs_per_rule(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """When all candidates are eliminated, log a structured breakdown per rule.

    Sets up a CODE_REVIEW scenario where each hard filter eliminates at least
    one candidate:
      - "author"   → PR author (anti-confirmation)
      - "excluded" → CODEX type listed in preferences.exclude
      - "small"    → small tier (CODE_REVIEW is large-only — #254)

    ``author``/``excluded`` are large tier so they survive the tier filter and
    are eliminated by their intended rules; ``small`` is the tier-eliminated one.
    """
    author = _make_handle(
        "author", AgentType.CLAUDE_CODE, model_tier="large", github_identity="alice"
    )
    excluded = _make_handle("excluded", AgentType.CODEX, model_tier="large", github_identity="bob")
    small = _make_handle(
        "small", AgentType.CLAUDE_CODE, model_tier="small", github_identity="carol"
    )
    prefs = AgentPreferencesConfig(exclude={"code_review": ["codex"]})

    with caplog.at_level(logging.WARNING), pytest.raises(AntiConfirmationViolation):
        select_agent_for(
            PlayType.CODE_REVIEW,
            _handles(author, excluded, small),
            pr_github_author="alice",
            preferences=prefs,
        )

    # structlog routing varies by test ordering — accept either capsys
    # (PrintLogger) or caplog (stdlib). Both should carry the structured fields.
    captured = capsys.readouterr()
    capsys_text = captured.out + captured.err
    caplog_text = "\n".join(rec.getMessage() for rec in caplog.records)
    combined = capsys_text + "\n" + caplog_text

    assert "agent_selection_blocked" in combined, (
        f"missing agent_selection_blocked event (capsys={capsys_text!r}, caplog={caplog.records!r})"
    )
    assert "code_review" in combined
    # Each rule's eliminated agent_id must appear in the rendered event.
    assert "anti_confirmation" in combined
    assert "exclude" in combined
    assert "tier" in combined
    assert "author" in combined
    assert "excluded" in combined
    assert "small" in combined
