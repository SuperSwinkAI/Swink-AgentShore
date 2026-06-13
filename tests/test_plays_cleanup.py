"""Tests for CleanupPlay and the agentshore-cleanup skill template."""

from __future__ import annotations

from pathlib import Path

from agentshore.config import PlayPacingConfig, RuntimeConfig
from agentshore.play_pacing import STANDARD_PLAY_COOLDOWN_PLAYS
from agentshore.plays.registry import build_default_registry
from agentshore.plays.skill_backed.cleanup import CleanupPlay
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    BudgetSnapshot,
    IssueSnapshot,
    OrchestratorState,
    PlayType,
    SessionState,
)


def _snap(
    agent_id: str = "a1",
    agent_type: AgentType = AgentType.CLAUDE_CODE,
    status: AgentStatus = AgentStatus.IDLE,
) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=agent_type,
        status=status,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=1,
        tasks_failed=0,
    )


def _issue(num: int = 1) -> IssueSnapshot:
    return IssueSnapshot(
        issue_number=num,
        title="Test",
        state="open",
        priority=None,
        labels=[],
        source=None,
    )


def _state(
    agents: list[AgentSnapshot] | None = None,
    issues: list[IssueSnapshot] | None = None,
    in_flight_plays: list[PlayType] | None = None,
    plays_since_last_play_type: dict[PlayType, int] | None = None,
    total_plays: int = 60,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="sess",
        session_state=SessionState.RUNNING,
        total_plays=total_plays,
        total_cost=0.0,
        agents=[_snap()] if agents is None else agents,
        open_issues=[] if issues is None else issues,
        budget=BudgetSnapshot(5.0, 0.0, 5.0, 0.1),
        in_flight_plays=[] if in_flight_plays is None else in_flight_plays,
        plays_since_last_play_type=(
            {} if plays_since_last_play_type is None else plays_since_last_play_type
        ),
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_cleanup_play_registered() -> None:
    registry = build_default_registry()
    play = registry.get(PlayType.CLEANUP)
    assert isinstance(play, CleanupPlay)
    assert play.skill_name == "agentshore-cleanup"
    assert play.play_type == PlayType.CLEANUP


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


def test_cleanup_preconditions_met() -> None:
    assert CleanupPlay().preconditions(_state()) == []


def test_cleanup_preconditions_capability_gated() -> None:
    agents = [_snap(status=AgentStatus.BUSY)]
    errors = CleanupPlay().preconditions(_state(agents=agents))
    assert errors != []


def test_cleanup_preconditions_in_flight() -> None:
    errors = CleanupPlay().preconditions(_state(in_flight_plays=[PlayType.CLEANUP]))
    assert [e.text for e in errors] == ["cleanup already in flight"]


def test_cleanup_blocked_during_cooldown() -> None:
    plays_left = STANDARD_PLAY_COOLDOWN_PLAYS - 1
    errors = CleanupPlay().preconditions(
        _state(plays_since_last_play_type={PlayType.CLEANUP: plays_left})
    )
    assert [e.text for e in errors] == [
        f"cleanup cooldown ({plays_left}/{STANDARD_PLAY_COOLDOWN_PLAYS} plays since last)"
    ]


def test_cleanup_allowed_after_cooldown() -> None:
    assert (
        CleanupPlay().preconditions(
            _state(plays_since_last_play_type={PlayType.CLEANUP: STANDARD_PLAY_COOLDOWN_PLAYS})
        )
        == []
    )


def test_cleanup_registry_uses_configured_standard_cooldown() -> None:
    cfg = RuntimeConfig(play_pacing=PlayPacingConfig(standard_cooldown_plays=7))
    play = build_default_registry(cfg).get(PlayType.CLEANUP)

    errors = play.preconditions(_state(plays_since_last_play_type={PlayType.CLEANUP: 6}))
    assert [e.text for e in errors] == ["cleanup cooldown (6/7 plays since last)"]
    assert play.preconditions(_state(plays_since_last_play_type={PlayType.CLEANUP: 7})) == []


def test_cleanup_not_blocked_by_large_open_issue_backlog() -> None:
    """The open-issue ceiling was removed: a big backlog must not mask cleanup.

    Trunk quality debt accumulates precisely when there's a large backlog, so
    cleanup stays reachable (rate-limited only by the standard cooldown).
    """
    many_issues = [_issue(num=i) for i in range(1, 60)]
    assert CleanupPlay().preconditions(_state(issues=many_issues)) == []


# ---------------------------------------------------------------------------
# Issue #564: first-run floor only applies when seed_project actually ran
# ---------------------------------------------------------------------------


def test_cleanup_allowed_on_existing_project_with_low_total_plays() -> None:
    """seed_project masked (existing project) → cleanup can fire as first play."""
    errors = CleanupPlay().preconditions(
        _state(total_plays=0, plays_since_last_play_type={}),
    )
    assert errors == []


def test_cleanup_blocked_warmup_when_seed_project_just_ran() -> None:
    """Fresh project: seed_project ran but total_plays still below the floor."""
    errors = CleanupPlay().preconditions(
        _state(
            total_plays=1,
            plays_since_last_play_type={PlayType.SEED_PROJECT: 0},
        ),
    )
    assert [e.text for e in errors] == ["warmup floor (1/20 plays)"]


def test_cleanup_allowed_after_warmup_completes_on_fresh_project() -> None:
    """Fresh project past the warmup window: cleanup can fire."""
    errors = CleanupPlay().preconditions(
        _state(
            total_plays=20,
            plays_since_last_play_type={PlayType.SEED_PROJECT: 20 - 1},
        ),
    )
    assert errors == []


# ---------------------------------------------------------------------------
# Skill template
# ---------------------------------------------------------------------------

_TEMPLATE_ROOT = (
    Path(__file__).parent.parent
    / "src"
    / "agentshore"
    / "skills"
    / "templates"
    / "agentshore-cleanup"
)


def test_cleanup_skill_template_bundled() -> None:
    skill_md = _TEMPLATE_ROOT / "SKILL.md"
    assert skill_md.exists(), f"agentshore-cleanup/SKILL.md not found at {skill_md}"
    content = skill_md.read_text()
    assert "name: agentshore-cleanup" in content


def test_cleanup_skill_includes_forbidden_mutations() -> None:
    content = (_TEMPLATE_ROOT / "SKILL.md").read_text()
    # Forbidden section exists in some form (heading or bold inline).
    import re as _re

    assert _re.search(r"(?:^|\n)(?:#+\s*Forbidden|\*\*Forbidden)", content), (
        "agentshore-cleanup: missing a Forbidden section"
    )
    assert ".github/workflows/**" in content


def test_agentshore_revert_template_removed() -> None:
    revert_dir = _TEMPLATE_ROOT.parent / "agentshore-revert"
    assert not revert_dir.exists(), "agentshore-revert template should have been deleted"
