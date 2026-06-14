"""Tests for PlayExecutor — the shared play execution lifecycle."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.config import AgentPreferencesConfig, RuntimeConfig, ScopeConfig
from agentshore.errors import AgentTimeout, PreconditionFailed
from agentshore.plays.base import PlayParams
from agentshore.plays.executor import PlayExecutor, build_idempotency_key
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    IssueSnapshot,
    OrchestratorState,
    PlayOutcome,
    PlayType,
    SessionState,
    SkillResult,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_state(
    agents: list[AgentSnapshot] | None = None,
    issues: list[IssueSnapshot] | None = None,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="sess-test",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=agents or [],
        open_issues=issues or [],
    )


def _agent(agent_id: str = "agent-1") -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.IDLE,
        total_tokens=0,
        total_cost=0.0,
        tasks_completed=0,
        tasks_failed=0,
        context_size=50_000,
        model_tier="medium",
    )


def _make_outcome(
    play_type: PlayType = PlayType.ISSUE_PICKUP,
    success: bool = True,
    agent_id: str | None = "agent-1",
    artifacts: list[object] | None = None,
    error: str | None = None,
) -> PlayOutcome:
    return PlayOutcome(
        play_type=play_type,
        agent_id=agent_id,
        success=success,
        partial=False,
        duration_seconds=0.5,
        token_cost=100,
        dollar_cost=0.01,
        artifacts=artifacts or [],
        alignment_delta=0.0,
        error=error,
    )


def _make_play(
    play_type: PlayType = PlayType.ISSUE_PICKUP,
    skill_name: str | None = "agentshore-issue-pickup",
    preconditions_result: list[str] | None = None,
    outcome: PlayOutcome | None = None,
    raise_on_execute: Exception | None = None,
) -> MagicMock:
    play = MagicMock()
    play.play_type = play_type
    play.skill_name = skill_name
    play.capability = "can_implement"
    play.preconditions = MagicMock(return_value=preconditions_result or [])
    play.estimated_cost = MagicMock(return_value=0.05)
    # Declarative behavior flags the executor reads off the Play (replacing the
    # old play-type frozensets). A bare MagicMock returns truthy mocks for these,
    # so set real bools mirroring the concrete plays' overrides.
    play.authors_prs = play_type == PlayType.ISSUE_PICKUP
    play.retarget_pr_base = play_type in {
        PlayType.MERGE_PR,
        PlayType.CODE_REVIEW,
        PlayType.UNBLOCK_PR,
    }
    play.is_handoff = play_type == PlayType.END_AGENT
    play.is_observation = False
    play.requeue_on_anti_confirmation = play_type == PlayType.CODE_REVIEW

    if raise_on_execute is not None:
        play.execute = AsyncMock(side_effect=raise_on_execute)
    else:
        play.execute = AsyncMock(return_value=outcome or _make_outcome(play_type))

    return play


def _make_registry(play: MagicMock | None = None) -> MagicMock:
    registry = MagicMock()
    if play:
        registry.get = MagicMock(return_value=play)
    else:
        registry.get = MagicMock(side_effect=KeyError("not registered"))
    return registry


def _make_resolver(params: PlayParams | None = None) -> AsyncMock:
    resolver = AsyncMock()
    resolver.resolve = AsyncMock(return_value=params or PlayParams(issue_number=1))
    return resolver


def _make_store() -> AsyncMock:
    store = AsyncMock()
    store.record_play = AsyncMock(return_value=42)
    store.update_play = AsyncMock()
    store.get_pr_author = AsyncMock(return_value=None)
    store.get_pr_author_type = AsyncMock(return_value=None)
    store.get_pr_github_author = AsyncMock(return_value=None)
    store.get_last_implementer = AsyncMock(return_value=None)
    store.record_handoff = AsyncMock()
    store.record_pull_request = AsyncMock()
    store.update_branch_activity = AsyncMock()
    store.record_external_mutation = AsyncMock()
    store.add_issue_labels = AsyncMock()
    store.log_scope_drift = AsyncMock()
    store.start_work_claim_group = AsyncMock(return_value=True)
    store.finish_work_claim_group = AsyncMock()
    return store


def _make_manager(
    agent_id: str = "agent-1",
    agent_type: AgentType = AgentType.CLAUDE_CODE,
    github_identity: str | None = None,
) -> MagicMock:
    manager = MagicMock()
    handle = MagicMock()
    handle.agent_id = agent_id
    handle.agent_type = agent_type
    handle.status = AgentStatus.IDLE
    handle.context_size = 50_000
    handle.model_tier = "medium"
    handle.github_identity = github_identity
    # Circuit-breaker scoring reads these directly off the handle (no longer
    # getattr-guarded); set real values so the agent sorts as healthy.
    handle.task_history = []
    handle.timeout_count = 0
    handle.consecutive_timeouts = 0
    manager.handles = {agent_id: handle}
    manager.branch_exposure = {}
    manager.get_handle = MagicMock(return_value=handle)
    manager.record_branch_exposure = MagicMock()
    manager.record_branch_commit = MagicMock()
    manager.mark_agent_error = AsyncMock()
    return manager


def _make_cfg(strict_scope: bool = False) -> RuntimeConfig:
    return RuntimeConfig(
        scope=ScopeConfig(strict_mode=strict_scope),
        agent_preferences=AgentPreferencesConfig(),
    )


def _make_executor(
    play: MagicMock | None = None,
    params: PlayParams | None = None,
    store: AsyncMock | None = None,
    manager: MagicMock | None = None,
    cfg: RuntimeConfig | None = None,
    strict_scope: bool = False,
    github: MagicMock | None = None,
) -> PlayExecutor:
    return PlayExecutor(
        registry=_make_registry(play),
        resolver=_make_resolver(params),
        store=store or _make_store(),
        manager=manager or _make_manager(),
        cfg=cfg or _make_cfg(strict_scope=strict_scope),
        project_path=Path("/tmp/project"),
        session_id="sess-test",
        github=github,
    )


# ---------------------------------------------------------------------------
# 1. Placeholder row inserted BEFORE play.execute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_play_persists_dispatched_agent_id() -> None:
    """A failed skill-backed play whose outcome omits agent_id is still
    attributed to the agent it was dispatched to.

    Regression: failure outcomes carried agent_id=None, so _persist_play wrote
    None and the ESR Play Log rendered failed plays as the literal "agentshore"
    instead of the agent that ran them. params.agent_id (set at dispatch) is the
    fallback.
    """
    store = _make_store()
    params = PlayParams(issue_number=1)
    failed = _make_outcome(
        success=False, agent_id=None, error="ci-change requested but forbidden by skill policy"
    )
    play = _make_play(outcome=failed)
    executor = _make_executor(play=play, params=params, store=store)

    await executor.execute(PlayType.ISSUE_PICKUP, _make_state(agents=[_agent()]))

    assert store.update_play.await_args is not None
    assert store.update_play.await_args.kwargs["agent_id"] == "agent-1"


@pytest.mark.asyncio
async def test_executor_starts_and_completes_work_claim() -> None:
    store = _make_store()
    params = PlayParams(issue_number=1, extras={"claim_group_id": "claim-1"})
    play = _make_play(outcome=_make_outcome())
    executor = _make_executor(play=play, params=params, store=store)

    outcome = await executor.execute(PlayType.ISSUE_PICKUP, _make_state(agents=[_agent()]))

    assert outcome.success
    store.start_work_claim_group.assert_awaited_once_with(
        "sess-test", "claim-1", play_id=42, agent_id="agent-1"
    )
    store.finish_work_claim_group.assert_awaited_with("sess-test", "claim-1", status="completed")


@pytest.mark.asyncio
async def test_issue_pickup_publish_failure_existing_pr_reconciles_to_success() -> None:
    store = _make_store()
    github = MagicMock()
    github.find_open_pr_by_branch = AsyncMock(
        return_value={
            "number": 77,
            "url": "https://github.com/o/r/pull/77",
            "headRefName": "agentshore/225-fix-auth",
            "headRefOid": "abc123",
        }
    )
    github.label_issue = AsyncMock(return_value=True)
    github.fetch_pull_request_by_number = AsyncMock(return_value=None)
    play = _make_play(
        outcome=_make_outcome(
            success=False,
            error="gh pr create failed: HTTP 401 Bad credentials",
        )
    )
    play._last_skill_result = SkillResult(
        success=False,
        error="gh pr create failed: HTTP 401 Bad credentials",
        issue_picked_up=225,
        branch="agentshore/225-fix-auth",
        tests_passed=True,
    )
    params = PlayParams(issue_number=225)
    executor = _make_executor(play=play, params=params, store=store, github=github)

    with patch("agentshore.plays.executor.validate_scope", new_callable=AsyncMock):
        outcome = await executor.execute(
            PlayType.ISSUE_PICKUP,
            _make_state(
                agents=[_agent()], issues=[IssueSnapshot(225, "Auth fix", "open", None, [], None)]
            ),
        )

    assert outcome.success is True
    assert outcome.error is None
    assert outcome.artifacts[0]["number"] == 77  # type: ignore[index]
    github.find_open_pr_by_branch.assert_awaited_once_with(
        "agentshore/225-fix-auth",
        identity_env={},
    )
    store.record_pull_request.assert_awaited()


@pytest.mark.asyncio
async def test_issue_pickup_publish_reconcile_tolerates_torn_down_agent() -> None:
    """issue_pickup publish-reconcile degrades gracefully when the runner agent
    was already end_agent'd before the post-dispatch reconcile runs.

    Regression (#18): get_handle raises PreconditionFailed (an OrchestratorError,
    not a KeyError) for an unregistered agent. The reconcile path's identity
    overlay only caught IdentityResolutionError, so a torn-down agent leaked the
    PreconditionFailed out of the play task — surfacing as play_task_failed and
    losing the completion bookkeeping. The reconcile must instead fall back to
    branch evidence without trying to flag the (already gone) agent.
    """
    store = _make_store()
    github = MagicMock()
    manager = _make_manager()
    # Agent torn down between dispatch and the post-completion reconcile.
    manager.get_handle = MagicMock(side_effect=PreconditionFailed("Unknown agent_id: 'gone'"))

    play = _make_play(
        outcome=_make_outcome(
            success=False,
            error="gh pr create failed: HTTP 401 Bad credentials",
        )
    )
    play._last_skill_result = SkillResult(
        success=False,
        error="gh pr create failed: HTTP 401 Bad credentials",
        issue_picked_up=225,
        branch="agentshore/225-fix-auth",
        tests_passed=True,
    )
    params = PlayParams(issue_number=225)
    executor = _make_executor(play=play, params=params, store=store, manager=manager, github=github)

    with patch("agentshore.plays.executor.validate_scope", new_callable=AsyncMock):
        outcome = await executor.execute(
            PlayType.ISSUE_PICKUP,
            _make_state(
                agents=[_agent()], issues=[IssueSnapshot(225, "Auth fix", "open", None, [], None)]
            ),
        )

    # No exception propagated; the play resolved to a real (failed) outcome with
    # branch evidence, and the gone agent was NOT flagged (nothing to flag).
    assert outcome.success is False
    manager.mark_agent_error.assert_not_awaited()
    github.find_open_pr_by_branch.assert_not_called()


@pytest.mark.asyncio
async def test_issue_pickup_reconcile_create_pr_uses_configured_target_branch() -> None:
    """desktop-53m0: when ``project.target_branch`` is set, the executor's PR
    publish-reconcile path must pass that value as ``base`` to ``create_pr``
    instead of calling ``default_branch``.
    """
    from agentshore.config import ProjectConfig

    store = _make_store()
    github = MagicMock()
    github.find_open_pr_by_branch = AsyncMock(return_value=None)
    github.default_branch = AsyncMock(return_value="main")
    github.create_pr = AsyncMock(
        return_value={
            "number": 91,
            "url": "https://github.com/o/r/pull/91",
            "headRefName": "agentshore/300-add-bar",
            "headRefOid": "deadbeef",
        }
    )
    github.label_issue = AsyncMock(return_value=True)
    github.fetch_pull_request_by_number = AsyncMock(return_value=None)

    play = _make_play(
        outcome=_make_outcome(
            success=False,
            error="gh pr create failed: HTTP 401 Bad credentials",
        )
    )
    play._last_skill_result = SkillResult(
        success=False,
        error="gh pr create failed: HTTP 401 Bad credentials",
        issue_picked_up=300,
        branch="agentshore/300-add-bar",
        tests_passed=True,
    )
    params = PlayParams(issue_number=300)
    cfg = RuntimeConfig(
        scope=ScopeConfig(strict_mode=False),
        agent_preferences=AgentPreferencesConfig(),
        project=ProjectConfig(target_branch="develop"),
    )
    executor = _make_executor(play=play, params=params, store=store, github=github, cfg=cfg)

    with (
        patch("agentshore.plays.executor.validate_scope", new_callable=AsyncMock),
        patch.object(
            executor._reconciler, "_remote_branch_exists", new=AsyncMock(return_value=True)
        ),
    ):
        outcome = await executor.execute(
            PlayType.ISSUE_PICKUP,
            _make_state(
                agents=[_agent()],
                issues=[IssueSnapshot(300, "Add bar", "open", None, [], None)],
            ),
        )

    assert outcome.success is True
    # The configured target_branch wins; default_branch must not be consulted.
    github.create_pr.assert_awaited_once()
    create_pr_kwargs = github.create_pr.await_args.kwargs
    assert create_pr_kwargs["base"] == "develop"
    github.default_branch.assert_not_awaited()


@pytest.mark.asyncio
async def test_issue_pickup_reconcile_create_pr_falls_back_to_default_branch_when_unset() -> None:
    """desktop-53m0: with no configured ``target_branch``, behaviour is
    unchanged — the executor still resolves the base via
    ``GitHubAdapter.default_branch``.
    """
    store = _make_store()
    github = MagicMock()
    github.find_open_pr_by_branch = AsyncMock(return_value=None)
    github.default_branch = AsyncMock(return_value="trunk")
    github.create_pr = AsyncMock(
        return_value={
            "number": 92,
            "url": "https://github.com/o/r/pull/92",
            "headRefName": "agentshore/301-add-baz",
            "headRefOid": "cafef00d",
        }
    )
    github.label_issue = AsyncMock(return_value=True)
    github.fetch_pull_request_by_number = AsyncMock(return_value=None)

    play = _make_play(
        outcome=_make_outcome(
            success=False,
            error="gh pr create failed: HTTP 401 Bad credentials",
        )
    )
    play._last_skill_result = SkillResult(
        success=False,
        error="gh pr create failed: HTTP 401 Bad credentials",
        issue_picked_up=301,
        branch="agentshore/301-add-baz",
        tests_passed=True,
    )
    params = PlayParams(issue_number=301)
    executor = _make_executor(play=play, params=params, store=store, github=github)

    with (
        patch("agentshore.plays.executor.validate_scope", new_callable=AsyncMock),
        patch.object(
            executor._reconciler, "_remote_branch_exists", new=AsyncMock(return_value=True)
        ),
    ):
        outcome = await executor.execute(
            PlayType.ISSUE_PICKUP,
            _make_state(
                agents=[_agent()],
                issues=[IssueSnapshot(301, "Add baz", "open", None, [], None)],
            ),
        )

    assert outcome.success is True
    github.default_branch.assert_awaited_once()
    create_pr_kwargs = github.create_pr.await_args.kwargs
    assert create_pr_kwargs["base"] == "trunk"


@pytest.mark.asyncio
async def test_executor_releases_claim_on_unresolved_target() -> None:
    """A legacy resolve that finds no claimable target still releases the claim.

    Eligibility refactor: the executor no longer re-runs play.preconditions()
    (validity is settled upstream by the EligibilityAuthority). The surviving
    pre-dispatch skip on the non-PPO legacy path is ``no_target`` — the resolver
    returned None because the target was lost. The claim group is still
    released and no plays row advances beyond the skip record.
    """
    store = _make_store()
    play = _make_play()
    # Legacy resolve-None path: no override is passed to execute(), so the
    # executor resolves and gets None back (target lost between mask and
    # dispatch). Carry a claim_group_id so we can assert it is released.
    resolver = AsyncMock()
    resolver.resolve = AsyncMock(return_value=None)
    executor = PlayExecutor(
        registry=_make_registry(play),
        resolver=resolver,
        store=store,
        manager=_make_manager(),
        cfg=_make_cfg(),
        project_path=Path("/tmp/project"),
        session_id="sess-test",
    )

    outcome = await executor.execute(PlayType.ISSUE_PICKUP, _make_state(agents=[_agent()]))

    assert outcome.skipped is True
    assert outcome.skip_category == "no_target"
    store.start_work_claim_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_executor_marks_retrying_on_agent_timeout() -> None:
    store = _make_store()
    params = PlayParams(issue_number=1, extras={"claim_group_id": "claim-1"})
    play = _make_play(raise_on_execute=AgentTimeout("timed out"))
    executor = _make_executor(play=play, params=params, store=store)

    outcome = await executor.execute(PlayType.ISSUE_PICKUP, _make_state(agents=[_agent()]))

    assert outcome.success is False
    assert outcome.partial is True
    assert outcome.retry_requested is True
    store.finish_work_claim_group.assert_awaited_with("sess-test", "claim-1", status="retrying")


@pytest.mark.asyncio
async def test_placeholder_row_inserted_before_execute() -> None:
    call_order: list[str] = []

    store = _make_store()

    async def record_play(record):
        call_order.append("record_play")
        return 99

    async def do_execute(state, params, *, ctx):
        call_order.append("execute")
        # play_id should already exist by the time execute is called
        assert ctx.play_id == 99
        return _make_outcome()

    play = _make_play()
    play.execute = AsyncMock(side_effect=do_execute)
    store.record_play = AsyncMock(side_effect=record_play)

    executor = _make_executor(play=play, store=store)
    await executor.execute(PlayType.ISSUE_PICKUP, _make_state())

    assert call_order.index("record_play") < call_order.index("execute")


# ---------------------------------------------------------------------------
# 2. Unregistered play returns failure outcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unregistered_play_returns_failure() -> None:
    store = _make_store()
    executor = _make_executor(store=store)  # registry raises KeyError
    outcome = await executor.execute(PlayType.ISSUE_PICKUP, _make_state())
    assert outcome.success is False
    assert "no play registered" in (outcome.error or "")
    # Issue #565 (Bug B): registry-miss skip now persists a plays-table row.
    store.record_play.assert_awaited_once()
    recorded = store.record_play.await_args.args[0]
    assert recorded.success is False
    assert recorded.failure_category == "skip:code_error"


# ---------------------------------------------------------------------------
# 3. Precondition failure at executor time returns a skipped (masked) outcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolved_target_persists_skip_no_target_row() -> None:
    """A legacy resolve-None persists a plays-table row with skip:no_target.

    Eligibility refactor: the executor no longer re-checks preconditions
    (validity is owned by the EligibilityAuthority and confirm() upstream). On
    the legacy non-PPO path, an unresolved target is the surviving pre-dispatch
    skip; the executor persists a plays-table row with
    failure_category="skip:no_target" so the UI run history reflects what the
    active-play panel showed (issue #565, Bug B).
    """
    store = _make_store()
    play = _make_play()
    resolver = AsyncMock()
    resolver.resolve = AsyncMock(return_value=None)
    executor = PlayExecutor(
        registry=_make_registry(play),
        resolver=resolver,
        store=store,
        manager=_make_manager(),
        cfg=_make_cfg(),
        project_path=Path("/tmp/project"),
        session_id="sess-test",
    )
    outcome = await executor.execute(PlayType.ISSUE_PICKUP, _make_state())
    assert outcome.skipped is True
    assert outcome.skip_category == "no_target"
    store.record_play.assert_awaited_once()
    recorded = store.record_play.await_args.args[0]
    assert recorded.success is False
    assert recorded.failure_category == "skip:no_target"


@pytest.mark.asyncio
async def test_bypass_preconditions_skips_precondition_gate() -> None:
    # Bootstrap fleet seeding sets bypass_preconditions=True so that the
    # cooldown gate added in InstantiateAgentPlay doesn't block back-to-back
    # boot-time instantiates. The executor must honor the same flag the
    # override-queue mask check honors.
    play = _make_play(preconditions_result=["instantiate cooldown (0/5 plays since last)"])
    executor = _make_executor(
        play=play,
        params=PlayParams(target_agent_type="claude_code", bypass_preconditions=True),
    )
    outcome = await executor.execute(PlayType.ISSUE_PICKUP, _make_state())
    assert outcome.success is True
    play.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_write_plan_releases_session_planned_issue_marker() -> None:
    play = _make_play(
        play_type=PlayType.WRITE_IMPLEMENTATION_PLAN,
        skill_name=None,
        outcome=_make_outcome(
            PlayType.WRITE_IMPLEMENTATION_PLAN,
            success=False,
            error="github identity mismatch",
        ),
    )
    executor = _make_executor(
        play=play,
        params=PlayParams(issue_number=234),
    )

    outcome = await executor.execute(PlayType.WRITE_IMPLEMENTATION_PLAN, _make_state())

    assert outcome.success is False
    assert 234 not in executor.planned_issues


@pytest.mark.asyncio
async def test_successful_write_plan_keeps_session_planned_issue_marker() -> None:
    play = _make_play(
        play_type=PlayType.WRITE_IMPLEMENTATION_PLAN,
        skill_name=None,
        outcome=_make_outcome(PlayType.WRITE_IMPLEMENTATION_PLAN, success=True),
    )
    executor = _make_executor(
        play=play,
        params=PlayParams(issue_number=234),
    )

    outcome = await executor.execute(PlayType.WRITE_IMPLEMENTATION_PLAN, _make_state())

    assert outcome.success is True
    assert 234 in executor.planned_issues


# ---------------------------------------------------------------------------
# 4. Anti-confirmation rejects same agent (DB check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anti_confirmation_db_check_rejects_same_identity_reviewer() -> None:
    """The candidate's GH identity must differ from the PR author's GH login."""
    agent_id = "agent-1"
    pr_number = 42

    store = _make_store()
    store.get_pr_github_author = AsyncMock(return_value="user_a")

    # Same identity as the PR author — must be rejected, regardless of agent_id.
    manager = _make_manager(agent_id=agent_id, github_identity="user_a")
    play = _make_play(play_type=PlayType.CODE_REVIEW, skill_name="agentshore-code-review")
    params = PlayParams(pr_number=pr_number)

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        mock_select.return_value = manager.handles[agent_id]
        executor = _make_executor(play=play, params=params, store=store, manager=manager)
        outcome = await executor.execute(PlayType.CODE_REVIEW, _make_state())

    assert outcome.success is True
    assert outcome.skipped is True
    assert outcome.skip_category == "staffing"
    assert "anti_confirmation" in (outcome.error or "").lower()
    # Issue #565 (Bug B): pre-record skips now persist a row tagged skip:staffing.
    store.record_play.assert_awaited_once()
    assert store.record_play.await_args.args[0].failure_category == "skip:staffing"
    play.execute.assert_not_called()


@pytest.mark.asyncio
async def test_anti_confirmation_rejects_same_identity_different_type() -> None:
    """A different agent_type but same GH identity (same login) is still rejected.

    Regression for the type-vs-identity confusion: agent_type is irrelevant;
    only the GH login matters. Two agents of *different* types sharing one
    login (e.g. both running as `agentbot`) cannot review each other's PRs.
    """
    pr_number = 42
    store = _make_store()
    store.get_pr_github_author = AsyncMock(return_value="user_a")

    # Different type from the PR author's tracked agent type, but same identity.
    manager = _make_manager(
        agent_id="codex-1",
        agent_type=AgentType.CODEX,
        github_identity="user_a",
    )
    play = _make_play(play_type=PlayType.CODE_REVIEW, skill_name="agentshore-code-review")
    params = PlayParams(pr_number=pr_number)

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        mock_select.return_value = manager.handles["codex-1"]
        executor = _make_executor(play=play, params=params, store=store, manager=manager)
        outcome = await executor.execute(PlayType.CODE_REVIEW, _make_state())

    assert outcome.success is True
    assert outcome.skipped is True
    assert outcome.skip_category == "staffing"
    assert "anti_confirmation" in (outcome.error or "").lower()
    # Issue #565 (Bug B): pre-record skips now persist a row tagged skip:staffing.
    store.record_play.assert_awaited_once()
    assert store.record_play.await_args.args[0].failure_category == "skip:staffing"
    play.execute.assert_not_called()


@pytest.mark.asyncio
async def test_anti_confirmation_allows_cross_identity_reviewer() -> None:
    """A reviewer whose GH identity differs from the PR author proceeds."""
    pr_number = 42
    store = _make_store()
    store.get_pr_github_author = AsyncMock(return_value="user_a")

    manager = _make_manager(
        agent_id="codex-1",
        agent_type=AgentType.CODEX,
        github_identity="user_b",
    )
    play = _make_play(play_type=PlayType.CODE_REVIEW, skill_name="agentshore-code-review")
    params = PlayParams(pr_number=pr_number)

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        mock_select.return_value = manager.handles["codex-1"]
        executor = _make_executor(play=play, params=params, store=store, manager=manager)
        outcome = await executor.execute(PlayType.CODE_REVIEW, _make_state())

    play.execute.assert_called_once()
    assert outcome.success is True


# ---------------------------------------------------------------------------
# 5. Circuit breaker OPEN (PreconditionFailed from play.execute) → agent_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_breaker_open_handled() -> None:
    play = _make_play(raise_on_execute=PreconditionFailed("Circuit breaker OPEN"))
    executor = _make_executor(play=play)

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        handle = MagicMock()
        handle.agent_id = "agent-1"
        mock_select.return_value = handle
        outcome = await executor.execute(PlayType.ISSUE_PICKUP, _make_state())

    assert outcome.success is False
    assert "Circuit breaker" in (outcome.error or "")


# ---------------------------------------------------------------------------
# 6. Malformed / failed result propagates correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_result_returns_failure_outcome() -> None:
    failed_outcome = _make_outcome(success=False, error="no valid result block found")
    play = _make_play(outcome=failed_outcome)

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        handle = MagicMock()
        handle.agent_id = "agent-1"
        mock_select.return_value = handle
        executor = _make_executor(play=play)
        outcome = await executor.execute(PlayType.ISSUE_PICKUP, _make_state())

    assert outcome.success is False
    assert "no valid result block" in (outcome.error or "")


# ---------------------------------------------------------------------------
# 7. SWITCH play writes handoff with real play_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_agent_play_writes_handoff_with_real_play_id() -> None:
    agent_id = "agent-src"

    store = _make_store()
    store.record_play = AsyncMock(return_value=77)

    manager = _make_manager(agent_id=agent_id)
    manager.handles = {
        agent_id: MagicMock(agent_id=agent_id, status=AgentStatus.IDLE, context_size=120_000)
    }

    end_outcome = PlayOutcome(
        play_type=PlayType.END_AGENT,
        agent_id=agent_id,
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=0,
        dollar_cost=0.0,
        artifacts=[],
        alignment_delta=0.0,
    )
    play = _make_play(
        play_type=PlayType.END_AGENT,
        skill_name=None,  # internal play
        outcome=end_outcome,
    )
    params = PlayParams(agent_id=agent_id)

    executor = _make_executor(play=play, params=params, store=store, manager=manager)
    outcome = await executor.execute(PlayType.END_AGENT, _make_state())

    assert outcome.success is True
    store.record_handoff.assert_called_once()
    call_args = store.record_handoff.call_args[0][0]
    assert call_args.play_id == 77
    assert call_args.source_agent_id == agent_id


# ---------------------------------------------------------------------------
# 8. PR artifact triggers both cache write AND DB write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_artifact_double_writes_cache_and_db() -> None:
    agent_id = "agent-1"
    pr_artifact = {"type": "pull_request", "number": 55, "branch": "feature/foo"}

    success_outcome = _make_outcome(
        artifacts=[pr_artifact],
        agent_id=agent_id,
    )
    play = _make_play(outcome=success_outcome)
    store = _make_store()
    manager = _make_manager(agent_id=agent_id)

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        mock_select.return_value = manager.handles[agent_id]
        executor = _make_executor(play=play, store=store, manager=manager)
        await executor.execute(PlayType.ISSUE_PICKUP, _make_state())

    # Cache write
    manager.record_branch_exposure.assert_called_once_with("feature/foo", agent_id)
    # DB write
    store.record_pull_request.assert_called_once()
    pr_record = store.record_pull_request.call_args[0][0]
    assert pr_record.pr_number == 55
    assert pr_record.author_agent_id == agent_id


# ---------------------------------------------------------------------------
# 8b. Wire-A: non-authoring plays must not stamp PR authorship
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("non_authoring_type", [PlayType.UNBLOCK_PR, PlayType.CODE_REVIEW])
async def test_pr_artifact_from_non_authoring_play_skips_authorship(
    non_authoring_type: PlayType,
) -> None:
    """unblock_pr / code_review emitting {"type":"pr"} must not call record_branch_exposure
    or record_pull_request — authorship belongs exclusively to issue_pickup."""
    agent_id = "agent-1"
    pr_artifact = {"type": "pr", "number": 99, "branch": "some/branch"}

    success_outcome = _make_outcome(artifacts=[pr_artifact], agent_id=agent_id)
    play = _make_play(
        play_type=non_authoring_type,
        skill_name="agentshore-unblock-pr",
        outcome=success_outcome,
    )
    store = _make_store()
    manager = _make_manager(agent_id=agent_id)

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        mock_select.return_value = manager.handles[agent_id]
        executor = _make_executor(play=play, store=store, manager=manager)
        await executor.execute(non_authoring_type, _make_state())

    manager.record_branch_exposure.assert_not_called()
    store.record_pull_request.assert_not_called()


# ---------------------------------------------------------------------------
# 9. Strict-mode scope drift path is intentionally absent without boundaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strict_scope_drift_no_clusters_no_drift() -> None:
    # The executor calls validate_scope without cluster/path-boundary parameters.
    # Since artifact drift is not inferred, the play succeeds from scope's perspective.
    drifted_outcome = _make_outcome(
        artifacts=[{"type": "commit", "path": "backend/api.py"}],
    )
    play = _make_play(outcome=drifted_outcome)
    store = _make_store()
    cfg = _make_cfg(strict_scope=True)

    state = _make_state()
    manager = _make_manager()

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        mock_select.return_value = manager.handles["agent-1"]
        executor = _make_executor(play=play, store=store, manager=manager, cfg=cfg)
        outcome = await executor.execute(PlayType.ISSUE_PICKUP, state)

    # No path boundaries → no drift → success unchanged
    assert outcome.success is True
    store.log_scope_drift.assert_not_called()


# ---------------------------------------------------------------------------
# 10. Unresolved params returns failure without calling play.execute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolved_params_returns_skipped_no_target() -> None:
    play = _make_play()
    resolver = AsyncMock()
    resolver.resolve = AsyncMock(return_value=None)
    store = _make_store()

    executor = PlayExecutor(
        registry=_make_registry(play),
        resolver=resolver,
        store=store,
        manager=_make_manager(),
        cfg=_make_cfg(),
        project_path=Path("/tmp"),
        session_id="sess-test",
    )
    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        outcome = await executor.execute(PlayType.ISSUE_PICKUP, _make_state())

    assert outcome.success is True
    assert outcome.skipped is True
    assert outcome.skip_category == "no_target"
    assert "unresolved" in (outcome.error or "")
    # Issue #565 (Bug B): pre-record skips now persist a plays-table row
    # so the UI's run history reflects what the active-play panel showed.
    store.record_play.assert_awaited_once()
    recorded = store.record_play.await_args.args[0]
    assert recorded.success is False
    assert recorded.failure_category == "skip:no_target"
    mock_select.assert_not_called()
    play.execute.assert_not_called()


# ---------------------------------------------------------------------------
# 3A. play_id stamped on returned outcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_play_id_stamped_on_returned_outcome() -> None:
    store = _make_store()
    store.record_play = AsyncMock(return_value=99)
    play = _make_play()

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        handle = MagicMock()
        handle.agent_id = "agent-1"
        mock_select.return_value = handle
        executor = _make_executor(play=play, store=store)
        outcome = await executor.execute(PlayType.ISSUE_PICKUP, _make_state())

    assert outcome.play_id == 99


@pytest.mark.asyncio
async def test_agent_current_play_marked_and_cleared() -> None:
    store = _make_store()
    store.record_play = AsyncMock(return_value=77)
    play = _make_play()
    manager = _make_manager()
    params = PlayParams(issue_number=12, pr_number=34, branch="feature/hud")

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        handle = manager.handles["agent-1"]
        mock_select.return_value = handle
        executor = _make_executor(play=play, params=params, store=store, manager=manager)
        await executor.execute(PlayType.ISSUE_PICKUP, _make_state())

    handle.start_play.assert_called_once()
    start_kwargs = handle.start_play.call_args.kwargs
    assert start_kwargs["play_type"] == PlayType.ISSUE_PICKUP
    assert start_kwargs["play_id"] == 77
    assert start_kwargs["issue_number"] == 12
    assert start_kwargs["pr_number"] == 34
    assert start_kwargs["branch"] == "feature/hud"
    handle.clear_play.assert_called_once_with(77)


# ---------------------------------------------------------------------------
# 3A. alignment_delta is None when state.graph is absent (Track 5: beads-native)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alignment_delta_none_when_no_graph() -> None:
    # With no beads graph (state.graph is None), the live delta is None regardless
    # of play type. None is distinct from 0.0 — it tells reward.py "no beads yet".
    play = _make_play()
    store = _make_store()

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        handle = MagicMock()
        handle.agent_id = "agent-1"
        mock_select.return_value = handle
        executor = _make_executor(play=play, store=store)
        outcome = await executor.execute(PlayType.CODE_REVIEW, _make_state())

    # No graph → None (not 0.0)
    assert outcome.alignment_delta is None


# ---------------------------------------------------------------------------
# 3A. alignment_delta is 0.0 when a graph is present but completion is unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alignment_delta_zero_first_tick_with_graph() -> None:
    from agentshore.beads import ProjectGraph

    play = _make_play(
        play_type=PlayType.CALIBRATE_ALIGNMENT, skill_name="agentshore-calibrate-alignment"
    )
    play.capability = None
    store = _make_store()
    graph = ProjectGraph(global_closure_ratio=0.5, tasks_ready=2, tasks_total=4)

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        handle = MagicMock()
        handle.agent_id = "agent-1"
        mock_select.return_value = handle
        executor = _make_executor(play=play, store=store)
        # No post-play closure movement means delta is 0.0
        state_with_graph = _make_state()
        state_with_graph = dataclasses.replace(state_with_graph, graph=graph)
        outcome = await executor.execute(PlayType.CALIBRATE_ALIGNMENT, state_with_graph)

    # A graph was present, so unchanged completion is 0.0 rather than None.
    assert outcome.alignment_delta == pytest.approx(0.0)
    assert outcome.alignment_delta is not None
    # play_id also stamped
    assert outcome.play_id == 42  # _make_store() default


@pytest.mark.asyncio
async def test_alignment_delta_persisted_from_post_play_graph() -> None:
    from agentshore.beads import ProjectGraph

    play = _make_play(
        play_type=PlayType.CALIBRATE_ALIGNMENT, skill_name="agentshore-calibrate-alignment"
    )
    play.capability = None
    store = _make_store()
    before = ProjectGraph(global_closure_ratio=0.0, tasks_ready=1, tasks_total=4)
    after = ProjectGraph(global_closure_ratio=0.25, tasks_ready=0, tasks_total=4)

    with (
        patch("agentshore.plays.executor.select_agent_for") as mock_select,
        patch("agentshore.plays.executor.load_graph", new_callable=AsyncMock, return_value=after),
    ):
        handle = MagicMock()
        handle.agent_id = "agent-1"
        mock_select.return_value = handle
        executor = _make_executor(play=play, store=store)
        state_with_graph = dataclasses.replace(_make_state(), graph=before)
        outcome = await executor.execute(PlayType.CALIBRATE_ALIGNMENT, state_with_graph)

    assert outcome.alignment_delta == pytest.approx(0.25)
    record = store.record_play.await_args.args[0]
    assert record.alignment_before == pytest.approx(0.0)
    update_kwargs = store.update_play.await_args.kwargs
    assert update_kwargs["alignment_before"] == pytest.approx(0.0)
    assert update_kwargs["alignment_after"] == pytest.approx(0.25)
    assert update_kwargs["alignment_delta"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# 3A. IssueInflationDetected sets inflation_raised on outcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inflation_raised_set_on_outcome() -> None:
    from agentshore.errors import IssueInflationDetected

    play = _make_play()
    store = _make_store()
    manager = _make_manager()

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        mock_select.return_value = manager.handles["agent-1"]
        with patch(
            "agentshore.plays.executor.validate_scope",
            new=AsyncMock(side_effect=IssueInflationDetected("too many")),
        ):
            executor = _make_executor(play=play, store=store, manager=manager)
            outcome = await executor.execute(PlayType.ISSUE_PICKUP, _make_state())

    assert outcome.inflation_raised is True
    assert outcome.success is True  # inflation doesn't flip success


# ---------------------------------------------------------------------------
# Alignment delta uses graph.global_closure_ratio (v0.10.0)
# ---------------------------------------------------------------------------


def test_alignment_delta_uses_graph_not_cluster_list() -> None:
    """_ALIGNMENT_PLAYS was removed in v0.10.0; alignment_delta now comes from
    state.graph.global_closure_ratio. Verify the executor module no longer
    exports _ALIGNMENT_PLAYS."""
    import agentshore.plays.executor as _executor_mod

    assert not hasattr(_executor_mod, "_ALIGNMENT_PLAYS")


# ---------------------------------------------------------------------------
# Issue #5 — on_agent_changed fires for normal busy/idle transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_agent_changed_busy_emitted_for_normal_dispatch() -> None:
    """Executor emits BUSY; final IDLE/ERROR is owned by the orchestrator."""
    agent_id = "agent-1"
    events: list[tuple[str, AgentStatus]] = []

    class TrackingProvider:
        async def on_state_update(self, state: object) -> None:
            pass

        async def on_play_started(self, play_type: object, params: object) -> None:
            pass

        async def on_play_completed(self, play: object) -> None:
            pass

        async def on_agent_changed(self, aid: str, status: AgentStatus) -> None:
            events.append((aid, status))

        async def on_feedback_requested(self, reason: str) -> None:
            pass

        async def on_session_paused(self, reason: str) -> None:
            pass

    provider = TrackingProvider()
    play = _make_play()
    store = _make_store()
    manager = _make_manager(agent_id=agent_id)

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        mock_select.return_value = manager.handles[agent_id]
        executor = PlayExecutor(
            registry=_make_registry(play),
            resolver=_make_resolver(PlayParams(issue_number=1)),
            store=store,
            manager=manager,
            cfg=_make_cfg(),
            project_path=Path("/tmp/project"),
            session_id="sess-test",
            state_provider=provider,
        )
        await executor.execute(PlayType.ISSUE_PICKUP, _make_state())

    assert events == [(agent_id, AgentStatus.BUSY)]


@pytest.mark.asyncio
async def test_on_agent_changed_not_emitted_for_non_skill_plays() -> None:
    """Non-skill plays (no agent selection) do not emit on_agent_changed."""
    agent_id = "agent-1"
    events: list[tuple[str, AgentStatus]] = []

    class TrackingProvider:
        async def on_state_update(self, state: object) -> None:
            pass

        async def on_play_started(self, play_type: object, params: object) -> None:
            pass

        async def on_play_completed(self, play: object) -> None:
            pass

        async def on_agent_changed(self, aid: str, status: AgentStatus) -> None:
            events.append((aid, status))

        async def on_feedback_requested(self, reason: str) -> None:
            pass

        async def on_session_paused(self, reason: str) -> None:
            pass

    provider = TrackingProvider()

    non_skill_outcome = PlayOutcome(
        play_type=PlayType.TAKE_BREAK,
        agent_id=None,
        success=True,
        partial=False,
        duration_seconds=0.0,
        token_cost=0,
        dollar_cost=0.0,
        artifacts=[],
        alignment_delta=0.0,
    )
    play = _make_play(
        play_type=PlayType.TAKE_BREAK,
        skill_name=None,  # no skill = no agent selection
        outcome=non_skill_outcome,
    )
    store = _make_store()
    manager = _make_manager(agent_id=agent_id)

    executor = PlayExecutor(
        registry=_make_registry(play),
        resolver=_make_resolver(PlayParams()),
        store=store,
        manager=manager,
        cfg=_make_cfg(),
        project_path=Path("/tmp/project"),
        session_id="sess-test",
        state_provider=provider,
    )
    await executor.execute(PlayType.TAKE_BREAK, _make_state())

    # No BUSY/IDLE events since no agent was selected
    assert events == [], f"Expected no on_agent_changed events for non-skill play, got: {events}"


# ---------------------------------------------------------------------------
# Review queue — PR artifact enqueues review and applies author label
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_artifact_enqueues_review() -> None:
    """A PR artifact produced by issue_pickup enqueues a review_queue record."""
    agent_id = "agent-1"
    pr_artifact = {"type": "pull_request", "number": 88, "branch": "feature/bar"}

    success_outcome = _make_outcome(artifacts=[pr_artifact], agent_id=agent_id)
    play = _make_play(outcome=success_outcome)
    store = _make_store()
    store.enqueue_review = AsyncMock(return_value=1)
    manager = _make_manager(agent_id=agent_id)

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        mock_select.return_value = manager.handles[agent_id]
        executor = _make_executor(play=play, store=store, manager=manager)
        await executor.execute(PlayType.ISSUE_PICKUP, _make_state())

    store.enqueue_review.assert_called_once()
    record = store.enqueue_review.call_args[0][0]
    assert record.pr_number == 88
    assert record.session_id == "sess-test"
    assert record.author_label == "claude_code"


@pytest.mark.asyncio
async def test_pr_artifact_applies_github_label() -> None:
    """When author_agent_type is set and github adapter is present, label_issue is called."""
    agent_id = "agent-1"
    pr_artifact = {"type": "pull_request", "number": 99, "branch": "feature/baz"}

    success_outcome = _make_outcome(artifacts=[pr_artifact], agent_id=agent_id)
    play = _make_play(outcome=success_outcome)
    store = _make_store()
    store.enqueue_review = AsyncMock(return_value=1)
    manager = _make_manager(agent_id=agent_id)
    github = AsyncMock()
    github.label_issue = AsyncMock(return_value=True)
    github.fetch_pull_request_by_number = AsyncMock(return_value=None)

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        mock_select.return_value = manager.handles[agent_id]
        executor = PlayExecutor(
            registry=_make_registry(play),
            resolver=_make_resolver(PlayParams(issue_number=1)),
            store=store,
            manager=manager,
            cfg=_make_cfg(),
            project_path=Path("/tmp/project"),
            session_id="sess-test",
            github=github,
        )
        await executor.execute(PlayType.ISSUE_PICKUP, _make_state())

    github.label_issue.assert_called_once_with(
        99, ["agentshore/author:claude_code"], "author_label:pr99:claude_code"
    )


@pytest.mark.asyncio
async def test_pr_artifact_no_label_when_no_author_type() -> None:
    """When author_agent_type is None, label_issue must NOT be called."""
    agent_id = "agent-1"
    pr_artifact = {"type": "pull_request", "number": 77, "branch": "feature/qux"}

    success_outcome = _make_outcome(artifacts=[pr_artifact], agent_id=agent_id)
    play = _make_play(outcome=success_outcome)
    store = _make_store()
    store.enqueue_review = AsyncMock(return_value=1)
    manager = _make_manager(agent_id=agent_id)
    # get_handle is called by _anti_confirmation_check and _mark_agent_current_play
    # before it is called inside _wire_deferrals to resolve author_agent_type.
    # We need the first calls to succeed but the _wire_deferrals call to raise
    # KeyError so author_agent_type ends up None.
    real_handle = manager.handles[agent_id]
    call_count = 0

    def _get_handle_side_effect(aid: str) -> object:
        nonlocal call_count
        call_count += 1
        # First call: _mark_agent_current_play. Identity-based anti-confirmation
        # only calls get_handle for CODE_REVIEW; this is ISSUE_PICKUP, so it
        # short-circuits without a lookup. The _wire_deferrals call (count 2)
        # must raise so author_agent_type ends up None.
        if call_count <= 1:
            return real_handle
        raise KeyError("terminated")

    manager.get_handle = MagicMock(side_effect=_get_handle_side_effect)
    github = AsyncMock()
    github.label_issue = AsyncMock(return_value=True)
    github.fetch_pull_request_by_number = AsyncMock(return_value=None)

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        mock_select.return_value = manager.handles[agent_id]
        executor = PlayExecutor(
            registry=_make_registry(play),
            resolver=_make_resolver(PlayParams(issue_number=1)),
            store=store,
            manager=manager,
            cfg=_make_cfg(),
            project_path=Path("/tmp/project"),
            session_id="sess-test",
            github=github,
        )
        await executor.execute(PlayType.ISSUE_PICKUP, _make_state())

    github.label_issue.assert_not_called()


@pytest.mark.asyncio
async def test_pr_artifact_no_label_when_no_github() -> None:
    """When self._github is None, enqueue_review still runs but no label call."""
    agent_id = "agent-1"
    pr_artifact = {"type": "pull_request", "number": 66, "branch": "feature/quux"}

    success_outcome = _make_outcome(artifacts=[pr_artifact], agent_id=agent_id)
    play = _make_play(outcome=success_outcome)
    store = _make_store()
    store.enqueue_review = AsyncMock(return_value=1)
    manager = _make_manager(agent_id=agent_id)

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        mock_select.return_value = manager.handles[agent_id]
        # No github= arg → defaults to None
        executor = _make_executor(play=play, store=store, manager=manager)
        await executor.execute(PlayType.ISSUE_PICKUP, _make_state())

    # enqueue_review should still be called
    store.enqueue_review.assert_called_once()
    # No github adapter → no label call (and no error)


# ---------------------------------------------------------------------------
# Requested mutations — issue policy gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_disallowed_issue_failure_labels_issue() -> None:
    """A policy-forbidden issue failure becomes a durable agentshore/disallowed gate."""
    agent_id = "agent-1"
    error = "ci-change requested but forbidden by skill policy"
    failed_outcome = _make_outcome(success=False, agent_id=agent_id, error=error)
    play = _make_play(outcome=failed_outcome)
    play._last_skill_result = SkillResult(success=False, error=error)
    store = _make_store()
    manager = _make_manager(agent_id=agent_id)
    github = AsyncMock()
    github.label_issue = AsyncMock(return_value=True)
    github.fetch_pull_request_by_number = AsyncMock(return_value=None)

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        mock_select.return_value = manager.handles[agent_id]
        executor = PlayExecutor(
            registry=_make_registry(play),
            resolver=_make_resolver(PlayParams(issue_number=209)),
            store=store,
            manager=manager,
            cfg=_make_cfg(),
            project_path=Path("/tmp/project"),
            session_id="sess-test",
            github=github,
        )
        await executor.execute(PlayType.ISSUE_PICKUP, _make_state())

    github.label_issue.assert_awaited_once()
    issue_number, labels, _key = github.label_issue.await_args.args
    assert issue_number == 209
    assert labels == ["agentshore/disallowed"]
    store.add_issue_labels.assert_awaited_once_with(209, "sess-test", ["agentshore/disallowed"])


@pytest.mark.asyncio
async def test_requested_label_mutation_is_applied_to_issue() -> None:
    """The executor applies label_issue-style mutations instead of leaving them pending."""
    agent_id = "agent-1"
    outcome = _make_outcome(success=False, agent_id=agent_id, error="blocked")
    play = _make_play(outcome=outcome)
    play._last_skill_result = SkillResult(
        success=False,
        requested_mutations=[
            {
                "type": "label",
                "target": "issue#209",
                "action": "add",
                "value": "agentshore/disallowed",
            }
        ],
        error="blocked",
    )
    store = _make_store()
    manager = _make_manager(agent_id=agent_id)
    github = AsyncMock()
    github.label_issue = AsyncMock(return_value=True)
    github.fetch_pull_request_by_number = AsyncMock(return_value=None)

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        mock_select.return_value = manager.handles[agent_id]
        executor = PlayExecutor(
            registry=_make_registry(play),
            resolver=_make_resolver(PlayParams(issue_number=209)),
            store=store,
            manager=manager,
            cfg=_make_cfg(),
            project_path=Path("/tmp/project"),
            session_id="sess-test",
            github=github,
        )
        await executor.execute(PlayType.ISSUE_PICKUP, _make_state())

    github.label_issue.assert_awaited_once()
    assert github.label_issue.await_args.args[0:2] == (209, ["agentshore/disallowed"])
    store.record_external_mutation.assert_not_awaited()
    store.add_issue_labels.assert_awaited_once_with(209, "sess-test", ["agentshore/disallowed"])


@pytest.mark.asyncio
async def test_request_play_mutation_is_ignored() -> None:
    """A ``request_play`` requested-mutation is dropped, not recorded or promoted.

    The agent-dictates-next-play mechanism was removed (it bypassed the PPO
    policy). Any lingering ``request_play`` emission must be a no-op: no
    ``external_mutations`` row, no label applied — the PPO chooses the next play.
    """
    agent_id = "agent-1"
    outcome = _make_outcome(success=True, agent_id=agent_id)
    play = _make_play(outcome=outcome)
    play._last_skill_result = SkillResult(
        success=True,
        requested_mutations=[{"type": "request_play", "play": "merge_pr", "pr": 42}],
    )
    store = _make_store()
    manager = _make_manager(agent_id=agent_id)
    github = AsyncMock()
    github.label_issue = AsyncMock(return_value=True)
    github.fetch_pull_request_by_number = AsyncMock(return_value=None)

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        mock_select.return_value = manager.handles[agent_id]
        executor = PlayExecutor(
            registry=_make_registry(play),
            resolver=_make_resolver(PlayParams(issue_number=209)),
            store=store,
            manager=manager,
            cfg=_make_cfg(),
            project_path=Path("/tmp/project"),
            session_id="sess-test",
            github=github,
        )
        await executor.execute(PlayType.ISSUE_PICKUP, _make_state())

    store.record_external_mutation.assert_not_awaited()
    github.label_issue.assert_not_awaited()


# ---------------------------------------------------------------------------
# Loop B fix — requeue on AntiConfirmationViolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_code_review_requeues_on_anti_confirmation_under_cap() -> None:
    """Executor requeues and returns a staffing skip when AntiConfirmationViolation fires."""
    from agentshore.errors import AntiConfirmationViolation

    play = _make_play(play_type=PlayType.CODE_REVIEW, skill_name="agentshore-code-review")
    params = PlayParams(pr_number=55)
    requeued: list[tuple[PlayType, PlayParams]] = []

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        mock_select.side_effect = AntiConfirmationViolation("no IDLE cross-type agent")
        executor = PlayExecutor(
            registry=_make_registry(play),
            resolver=_make_resolver(params),
            store=_make_store(),
            manager=_make_manager(),
            cfg=_make_cfg(),
            project_path=Path("/tmp/project"),
            session_id="sess-test",
            requeue_callback=lambda pt, p: requeued.append((pt, p)),
        )
        outcome = await executor.execute(PlayType.CODE_REVIEW, _make_state())

    assert outcome.success is True
    assert outcome.partial is True
    assert outcome.skipped is True
    assert outcome.skip_category == "staffing"
    assert outcome.error is not None and "requeued" in outcome.error
    assert len(requeued) == 1
    pt, rp = requeued[0]
    assert pt == PlayType.CODE_REVIEW
    assert rp.extras.get("requeue_attempts") == 1
    assert "play_id" not in rp.extras
    play.execute.assert_not_called()


@pytest.mark.asyncio
async def test_code_review_skips_after_three_requeues() -> None:
    """After hitting the cap, the violation remains a staffing skip, not a play failure."""
    from agentshore.errors import AntiConfirmationViolation

    play = _make_play(play_type=PlayType.CODE_REVIEW, skill_name="agentshore-code-review")
    params = PlayParams(pr_number=55, extras={"requeue_attempts": 3})
    requeued: list[object] = []
    store = _make_store()

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        mock_select.side_effect = AntiConfirmationViolation("no IDLE cross-type agent")
        executor = PlayExecutor(
            registry=_make_registry(play),
            resolver=_make_resolver(params),
            store=store,
            manager=_make_manager(),
            cfg=_make_cfg(),
            project_path=Path("/tmp/project"),
            session_id="sess-test",
            requeue_callback=lambda pt, p: requeued.append((pt, p)),
        )
        outcome = await executor.execute(PlayType.CODE_REVIEW, _make_state())

    assert outcome.success is True
    assert outcome.skipped is True
    assert outcome.skip_category == "staffing"
    assert len(requeued) == 0
    # Issue #565 (Bug B): pre-record skip persists a plays-table row.
    store.record_play.assert_awaited_once()
    assert store.record_play.await_args.args[0].failure_category == "skip:staffing"
    play.execute.assert_not_called()


@pytest.mark.asyncio
async def test_code_review_skips_without_requeue_callback() -> None:
    """Without a callback wired, violations still skip instead of recording failed plays."""
    from agentshore.errors import AntiConfirmationViolation

    play = _make_play(play_type=PlayType.CODE_REVIEW, skill_name="agentshore-code-review")
    params = PlayParams(pr_number=55)
    store = _make_store()

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        mock_select.side_effect = AntiConfirmationViolation("no IDLE cross-type agent")
        executor = _make_executor(play=play, params=params, store=store)
        outcome = await executor.execute(PlayType.CODE_REVIEW, _make_state())

    assert outcome.success is True
    assert outcome.skipped is True
    assert outcome.skip_category == "staffing"
    # Issue #565 (Bug B): pre-record skip persists a plays-table row.
    store.record_play.assert_awaited_once()
    assert store.record_play.await_args.args[0].failure_category == "skip:staffing"


# ---------------------------------------------------------------------------
# play_started structlog event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_play_started_event_emitted() -> None:
    """play_started is logged once per executed play with expected fields."""
    agent_id = "agent-1"
    play = _make_play()
    manager = _make_manager(agent_id=agent_id)

    with (
        patch("agentshore.plays.executor.select_agent_for") as mock_select,
        patch("agentshore.plays.executor._logger") as mock_logger,
    ):
        mock_select.return_value = manager.handles[agent_id]
        executor = _make_executor(play=play, manager=manager)
        await executor.execute(PlayType.ISSUE_PICKUP, _make_state())

    calls = [c for c in mock_logger.info.call_args_list if c.args and c.args[0] == "play_started"]
    assert len(calls) == 1
    kwargs = calls[0].kwargs
    assert kwargs["play_type"] == PlayType.ISSUE_PICKUP.value
    assert kwargs["agent_id"] == agent_id
    assert "play_id" in kwargs


# ---------------------------------------------------------------------------
# build_idempotency_key tests
# ---------------------------------------------------------------------------


def test_build_idempotency_key_contains_session_id() -> None:
    """The idempotency key must embed the session_id to prevent cross-session collisions."""
    session_id = "sess-abc123"
    mutation: dict[str, object] = {"type": "label_issue", "target": "42"}
    key = build_idempotency_key(session_id, mutation)
    # Key is a 16-char hex string derived from a payload that includes the session_id.
    # We verify the session_id influences the key by checking a different session
    # produces a different key.
    other_key = build_idempotency_key("sess-xyz999", mutation)
    assert key != other_key, "keys for different session_ids must differ"


def test_build_idempotency_key_format_stable() -> None:
    """Key format is a 16-character lowercase hex string (regression guard)."""
    key = build_idempotency_key("sess-stable", {"type": "close_issue", "target": "7"})
    assert len(key) == 16
    assert all(c in "0123456789abcdef" for c in key)


def test_build_idempotency_key_deterministic() -> None:
    """Same inputs always produce the same key."""
    session_id = "sess-det"
    mutation: dict[str, object] = {"type": "create_pr", "target": "feature/x"}
    key1 = build_idempotency_key(session_id, mutation)
    key2 = build_idempotency_key(session_id, mutation)
    assert key1 == key2


def test_build_idempotency_key_rejects_empty_session_id() -> None:
    """An empty session_id must raise ValueError to prevent silent cross-session collisions."""
    import pytest

    with pytest.raises(ValueError, match="session_id"):
        build_idempotency_key("", {"type": "label_issue", "target": "1"})


# ---------------------------------------------------------------------------
# end_agent through the executor (#154 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_agent_play_clears_agent_through_executor(
    tmp_path: Path, mock_agent_path: Path
) -> None:
    """Dispatching END_AGENT via the executor must actually retire the agent.

    #154 regression guard. The executor marks the target agent in-flight
    (_mark_agent_current_play -> handle.start_play) before the play body runs,
    so EndAgentPlay's manager.clear() call races against the agent's own
    END_AGENT marker. #144 added an active-play guard to clear() but never
    updated end_agent.py, making every RL-driven retirement fail with
    PreconditionFailed. Unit tests on clear() alone could not catch this —
    only a dispatch through the real executor against a real AgentManager
    exercises the marker-then-clear sequence.
    """
    import sys

    from agentshore.agents.manager import AgentManager
    from agentshore.config import AgentConfig
    from agentshore.data.store import DataStore, SessionRecord
    from agentshore.plays.internal.end_agent import EndAgentPlay

    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    await store.create_session(
        SessionRecord(
            session_id="sess-test",
            project_path=str(tmp_path),
            started_at="2026-06-10T00:00:00+00:00",
        )
    )
    try:
        manager = AgentManager(
            session_id="sess-test",
            store=store,
            cfg=RuntimeConfig(
                agents={
                    "codex": AgentConfig(  # type: ignore[assignment]
                        enabled=True, binary=str(mock_agent_path), timeout=10
                    )
                }
            ),
            working_dir=tmp_path,
            python_executable=sys.executable,
        )
        handle = await manager.instantiate(AgentType.CODEX)
        agent_id = handle.agent_id

        executor = _make_executor(
            play=EndAgentPlay(),  # type: ignore[arg-type]  # real play, not a mock
            params=PlayParams(agent_id=agent_id),
            manager=manager,  # type: ignore[arg-type]  # real manager, not a mock
        )

        outcome = await executor.execute(PlayType.END_AGENT, _make_state(agents=[_agent(agent_id)]))

        assert outcome.success is True, f"end_agent failed: {outcome.error}"
        assert agent_id not in manager.handles, "agent handle must be removed"
    finally:
        await store.close()
