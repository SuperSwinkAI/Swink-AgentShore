"""Parameter resolver — derives PlayParams from current session state."""

from __future__ import annotations

import dataclasses
from collections import Counter
from typing import TYPE_CHECKING, overload

from agentshore.agents.model_tiers import (
    DEFAULT_MODEL_TIER,
    MODEL_TIER_PRIORITY,
    enabled_model_tiers,
)
from agentshore.agents.worktree import TRUNK_SCOPED_PLAYS
from agentshore.github.pr_links import issue_numbers_for_pr
from agentshore.logging import get_logger
from agentshore.play_rules import needs_review
from agentshore.plays.base import PlayParams
from agentshore.plays.candidates import (
    PlayCandidate,
    PlayCandidateService,
    build_candidate_plan,
    idle_can_review_agents,
    in_progress_issue_numbers,
    issue_available_for_debug,
    pick_reviewer_for_pr,
    pr_merge_ready,
    pr_resource_keys,
    pr_resource_keys_for_pr,
    pr_unblockable,
)
from agentshore.plays.internal.end_agent import _MIN_PLAYS_PER_AGENT
from agentshore.state import (
    AgentStatus,
    AgentType,
    IssueSnapshot,
    OrchestratorState,
    PlayType,
    SessionState,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agentshore.agents.manager import AgentManager
    from agentshore.config import RuntimeConfig
    from agentshore.data.models import PullRequestRecord, ReviewQueueRecord
    from agentshore.data.store import DataStore
    from agentshore.github.adapter import GitHubAdapter
    from agentshore.state import AgentSnapshot

_logger = get_logger(__name__)

# Synthetic resource key acquired by all trunk-scoped plays so they
# serialize against each other. Without this, cleanup + merge_pr (and
# other trunk-scoped pairs) could run simultaneously, leaving the main
# checkout dirty and failing merges with dirty_trunk.
_TRUNK_RESOURCE_KEY = "trunk:main_repo"


_PR_WORK_PLAY_TYPES = frozenset({PlayType.CODE_REVIEW, PlayType.UNBLOCK_PR, PlayType.MERGE_PR})
_ISSUE_WORK_PLAY_TYPES = frozenset(
    {
        PlayType.ISSUE_PICKUP,
        PlayType.WRITE_IMPLEMENTATION_PLAN,
        PlayType.REFINE_TASK_BREAKDOWN,
    }
)

_idle_can_review_agents = idle_can_review_agents
_pick_reviewer_for_pr = pick_reviewer_for_pr


def _claim_group_id(params: PlayParams) -> str | None:
    raw = params.extras.get("claim_group_id")
    return raw if isinstance(raw, str) and raw else None


def _review_queue_id(params: PlayParams) -> int | None:
    raw = params.extras.get("review_queue_id")
    return raw if isinstance(raw, int) else None


def _issue_numbers_with_merged_prs(state: OrchestratorState) -> set[int]:
    """Issue numbers already resolved by merged PRs in the current state snapshot."""
    return {
        issue_number
        for pr in state.pull_requests
        if pr.state.upper() == "MERGED"
        for issue_number in issue_numbers_for_pr(pr)
    }


# After this many failed unblock_pr plays on the same PR in a single session,
# the resolver stops dispatching that PR — the skill has already diagnosed it
# as irresolvable (superseded, perpetual conflict, etc.).
_UNBLOCK_PR_EXHAUSTION_THRESHOLD = 3


class ParameterResolver:
    """Derives PlayParams for each play type from the current OrchestratorState.

    All resolution is read-only.  Returns None when a required parameter
    (issue_number, pr_number, branch, agent_id) cannot be resolved; the
    executor surfaces this as a failed PlayOutcome with error="unresolved".
    """

    def __init__(
        self,
        *,
        store: DataStore,
        manager: AgentManager,
        cfg: RuntimeConfig,
        github: GitHubAdapter | None = None,
        project_path: Path | None = None,
    ) -> None:
        self._store = store
        self._manager = manager
        self._cfg = cfg
        self._github = github
        self._project_path = project_path
        # Per-PR unblock_pr failure count for the current session (in-memory).
        # PRs reaching _UNBLOCK_PR_EXHAUSTION_THRESHOLD are excluded from dispatch
        # so irresolvable-conflict PRs don't keep consuming agent time.
        self._unblock_pr_failures: dict[int, int] = {}
        self._candidate_service = PlayCandidateService(
            store=store,
            cfg=cfg,
            github=github,
            project_path=project_path,
            unblock_failures=self._unblock_pr_failures,
            unblock_exhaustion_threshold=_UNBLOCK_PR_EXHAUSTION_THRESHOLD,
        )

    def record_unblock_pr_failure(self, pr_number: int) -> bool:
        """Increment the session-level failure count for *pr_number*.

        Called by the orchestrator after each failed unblock_pr play.  When the
        count reaches _UNBLOCK_PR_EXHAUSTION_THRESHOLD the PR is excluded from
        future _resolve_unblock_pr picks for the rest of this session.
        Returns True when the PR has just reached exhaustion.
        """
        self._unblock_pr_failures[pr_number] = self._unblock_pr_failures.get(pr_number, 0) + 1
        if self._unblock_pr_failures[pr_number] >= _UNBLOCK_PR_EXHAUSTION_THRESHOLD:
            _logger.warning(
                "unblock_pr_exhausted",
                pr_number=pr_number,
                failures=self._unblock_pr_failures[pr_number],
            )
            return True
        return False

    async def resolve(
        self,
        play_type: PlayType,
        state: OrchestratorState,
        *,
        override: PlayParams | None = None,
        config_index_override: tuple[str, str] | None = None,
    ) -> PlayParams | None:
        """Return resolved params, or None if resolution fails.

        *override* short-circuits resolution when it carries any populated
        field (e.g. ``pr_number=42``, ``issue_number=7``, ``seed_path=…``).
        A default-constructed ``PlayParams()`` is treated as "no override —
        please resolve" so the override queue can enqueue a play type without
        also pre-resolving its parameters.

        ``config_index_override`` is an ``(agent_type, model_tier)`` pair
        chosen by the PPO config head. When set and ``play_type`` is
        ``INSTANTIATE_AGENT``, the resolver returns those params directly
        instead of running its priority/round-robin fallback.
        """
        if override is not None and override != PlayParams():
            return await self._resolve_override(play_type, state, override)

        match play_type:
            # -- internal plays (synchronous) ---------------------------------
            case PlayType.INSTANTIATE_AGENT:
                return self._resolve_instantiate_agent(
                    state,
                    config_index_override=config_index_override,
                )
            case PlayType.END_AGENT:
                return self._resolve_end_agent(state)
            case PlayType.TAKE_BREAK:
                return self._resolve_take_break(state)
            # -- skill-backed plays (synchronous) ----------------------------
            case PlayType.REFINE_TASK_BREAKDOWN:
                return await self._resolve_refine_tasks(state)
            # -- skill-backed plays (async) -----------------------------------
            case PlayType.UNBLOCK_PR:
                return await self._resolve_unblock_pr(state)
            case PlayType.WRITE_IMPLEMENTATION_PLAN:
                return await self._resolve_write_implementation_plan(state)
            case PlayType.SYSTEMATIC_DEBUGGING:
                return await self._resolve_systematic_debugging(state)
            case PlayType.ISSUE_PICKUP:
                return await self._resolve_issue_pickup(state)
            case PlayType.CODE_REVIEW:
                return await self._resolve_code_review(state)
            case PlayType.MERGE_PR:
                return await self._resolve_merge_pr(state)
            case PlayType.RUN_QA:
                return await self._resolve_run_qa(state)
            case PlayType.BROWSER_VERIFICATION:
                return await self._resolve_browser_verification(state)
            # -- no-arg plays -------------------------------------------------
            case _:
                # END_SESSION, CLEANUP, TAKE_BREAK, GROOM_BACKLOG,
                # CALIBRATE_ALIGNMENT, DESIGN_AUDIT, RECONCILE_STATE, PRUNE, FUTURE_7/8
                return PlayParams()

    # -------------------------------------------------------------------------
    # Internal play resolvers
    # -------------------------------------------------------------------------

    def _resolve_take_break(self, state: OrchestratorState) -> PlayParams:
        """Attach the agent/error that made TAKE_BREAK eligible.

        TAKE_BREAK is internal, but it still targets exactly one agent. The
        trigger metadata lets dashboards explain why that agent is cooling
        down while other healthy agents keep working.
        """
        triggers = [
            agent
            for agent in state.agents
            if agent.status == AgentStatus.ERROR
            and agent.last_error_class in {"rate_limit", "unknown"}
            and agent.current_play_type != PlayType.TAKE_BREAK
        ]
        if not triggers:
            return PlayParams()

        def _trigger_key(agent: AgentSnapshot) -> tuple[int, str, str]:
            error_class = agent.last_error_class or "unknown"
            priority = 0 if error_class == "rate_limit" else 1
            return (priority, agent.agent_type.value, agent.agent_id)

        trigger = min(triggers, key=_trigger_key)
        return PlayParams(
            agent_id=trigger.agent_id,
            extras={
                "trigger_agent_id": trigger.agent_id,
                "trigger_agent_type": trigger.agent_type.value,
                "trigger_error_class": trigger.last_error_class or "unknown",
            },
        )

    def _resolve_instantiate_agent(
        self,
        state: OrchestratorState,
        *,
        config_index_override: tuple[str, str] | None = None,
    ) -> PlayParams | None:
        """Pick the first configured enabled type/tier without an idle agent.

        If ``config_index_override`` is supplied (PPO config head pick), it is
        used directly and the config-order round-robin path is skipped.
        """
        if config_index_override is not None:
            override_agent_type, override_model_tier = config_index_override
            return PlayParams(
                target_agent_type=override_agent_type,
                target_model_tier=override_model_tier,
            )
        existing = Counter(
            (s.agent_type.value, s.model_tier or DEFAULT_MODEL_TIER) for s in state.agents
        )
        idle_configs = {
            (s.agent_type.value, s.model_tier or DEFAULT_MODEL_TIER)
            for s in state.agents
            if s.status == AgentStatus.IDLE
        }
        enabled_agents = []
        for agent_key, agent_cfg in self._cfg.agents.items():
            try:
                agent_type = AgentType(agent_key)
            except ValueError:
                continue
            if not agent_cfg.enabled:
                continue
            enabled_agents.append((agent_type, enabled_model_tiers(agent_type, agent_cfg)))

        candidates: list[tuple[str, str]] = []
        for model_tier in MODEL_TIER_PRIORITY:
            for agent_type, enabled_tiers in enabled_agents:
                if model_tier not in enabled_tiers:
                    continue
                pair = (agent_type.value, model_tier)
                candidates.append(pair)
                if existing[pair] == 0:
                    return PlayParams(
                        target_agent_type=agent_type.value,
                        target_model_tier=model_tier,
                    )

        if candidates:
            available_candidates = [pair for pair in candidates if pair not in idle_configs]
            if not available_candidates:
                return None
            agent_key, model_tier = min(available_candidates, key=lambda pair: existing[pair])
            return PlayParams(target_agent_type=agent_key, target_model_tier=model_tier)

        return None

    def _resolve_end_agent(self, state: OrchestratorState) -> PlayParams | None:
        """Pick the IDLE agent with the highest failure rate or least utilization.

        Filters out agents below the per-agent play-count gate
        (``_MIN_PLAYS_PER_AGENT`` in ``end_agent.py``); only agents that have
        earned their keep this session are eligible to be terminated. The play
        precondition enforces the same gate, so this is a defense-in-depth
        check. During drain the gate is bypassed so any idle agent can be ended.
        """
        # A recovery-exhausted agent is wedged (typically ERROR state). The mask
        # re-enables END_AGENT for it (``_stage_wedged_end_agent``); target it
        # directly and bypass the min-plays / two-agent precondition the mask
        # already lifted, so the PPO's choice to retire it actually dispatches.
        if state.recovery_exhausted_agent_ids:
            wedged = [s for s in state.agents if s.agent_id in state.recovery_exhausted_agent_ids]
            if wedged:
                return PlayParams(agent_id=wedged[0].agent_id, bypass_preconditions=True)

        if state.session_state == SessionState.DRAINING:
            idle = [s for s in state.agents if s.status == AgentStatus.IDLE]
        else:
            idle = [
                s
                for s in state.agents
                if s.status == AgentStatus.IDLE
                and (s.tasks_completed + s.tasks_failed) > _MIN_PLAYS_PER_AGENT
            ]
        if not idle:
            return None

        def _score(s: AgentSnapshot) -> float:
            total = s.tasks_completed + s.tasks_failed
            if total == 0:
                return 0.0
            return s.tasks_failed / total

        worst = max(idle, key=_score)
        return PlayParams(agent_id=worst.agent_id)

    # -------------------------------------------------------------------------
    # Durable work claims
    # -------------------------------------------------------------------------

    async def _resolve_override(
        self, play_type: PlayType, state: OrchestratorState, override: PlayParams
    ) -> PlayParams | None:
        """Treat override params as target constraints, then validate and claim them."""
        claim_group_id = _claim_group_id(override)
        if claim_group_id is not None:
            active = await self._claim_group_is_active(state.session_id, claim_group_id)
            if active is False:
                _logger.warning(
                    "override_claim_inactive",
                    play_type=play_type.value,
                    claim_group_id=claim_group_id,
                    session_id=state.session_id,
                )
                return None

        if play_type in _PR_WORK_PLAY_TYPES:
            if override.pr_number is None:
                return await self.resolve(play_type, state, override=PlayParams())
            return await self._resolve_specific_pr(play_type, state, override)

        if play_type in _ISSUE_WORK_PLAY_TYPES:
            if override.issue_number is None:
                return await self.resolve(play_type, state, override=PlayParams())
            if not await self._issue_is_open_and_available(state, override.issue_number):
                return None
            return await self._claim_params(play_type, state, override)

        if play_type == PlayType.SYSTEMATIC_DEBUGGING:
            if override.issue_number is not None:
                if not await self._issue_is_debuggable(state, override.issue_number):
                    return None
            elif override.branch is None:
                return await self.resolve(play_type, state, override=PlayParams())
            else:
                override = dataclasses.replace(override, bypass_preconditions=True)
            return await self._claim_params(play_type, state, override)

        if play_type == PlayType.BROWSER_VERIFICATION and override.branch is None:
            return await self.resolve(play_type, state, override=PlayParams())

        return await self._claim_params(play_type, state, override)

    async def _resolve_specific_pr(
        self, play_type: PlayType, state: OrchestratorState, params: PlayParams
    ) -> PlayParams | None:
        pr_number = params.pr_number
        if pr_number is None:
            return None
        pr = await self._find_pr(state, pr_number)
        if pr is None or str(getattr(pr, "state", "")).lower() != "open":
            return None

        if play_type == PlayType.MERGE_PR:
            if not pr_merge_ready(pr, target_branch=self._cfg.project.target_branch):
                return None
            return await self._claim_params(play_type, state, params)

        if play_type == PlayType.UNBLOCK_PR:
            if not pr_unblockable(pr):
                return None
            return await self._claim_params(play_type, state, params)

        if play_type == PlayType.CODE_REVIEW:
            if bool(getattr(pr, "is_draft", False)) or not needs_review(pr):
                return None
            idle_reviewers = idle_can_review_agents(state)
            author = getattr(pr, "github_author", None)
            reviewer = pick_reviewer_for_pr(
                author if isinstance(author, str) else None,
                idle_reviewers,
            )
            if reviewer is None:
                return None
            params = dataclasses.replace(params, target_agent_id=reviewer.agent_id)
            return await self._claim_code_review_params(state, params, reviewer.agent_id)

        return None

    async def _claim_code_review_params(
        self, state: OrchestratorState, params: PlayParams, reviewer_agent_id: str
    ) -> PlayParams | None:
        if params.pr_number is None:
            return None
        pr = await self._find_pr(state, params.pr_number)
        resource_keys = list(
            pr_resource_keys_for_pr(pr)
            if pr is not None
            else pr_resource_keys(params.pr_number, params.issue_number)
        )

        claim_group_id = _claim_group_id(params)
        review_queue_id = _review_queue_id(params)
        if claim_group_id is None:
            pending = await self._pending_review_for_pr(state.session_id, params.pr_number)
            if pending is not None and pending.queue_id is not None:
                method = getattr(self._store, "claim_review_with_work_claims", None)
                if callable(method):
                    claimed = await method(
                        session_id=state.session_id,
                        queue_id=pending.queue_id,
                        agent_id=reviewer_agent_id,
                        play_type=PlayType.CODE_REVIEW.value,
                        resource_keys=resource_keys,
                    )
                    if isinstance(claimed, str):
                        return dataclasses.replace(
                            params,
                            extras={
                                **params.extras,
                                "claim_group_id": claimed,
                                "resource_keys": resource_keys,
                                "review_queue_id": pending.queue_id,
                            },
                        )
                    if claimed is None:
                        return None

        claimed_params = await self._claim_params(
            PlayType.CODE_REVIEW, state, params, resource_keys=resource_keys
        )
        if claimed_params is None:
            return None

        if review_queue_id is None:
            method = getattr(self._store, "claim_pending_review_for_pr", None)
            if callable(method):
                claimed_review = await method(state.session_id, params.pr_number, reviewer_agent_id)
                if claimed_review is not None and claimed_review.queue_id is not None:
                    claimed_params = dataclasses.replace(
                        claimed_params,
                        extras={
                            **claimed_params.extras,
                            "review_queue_id": claimed_review.queue_id,
                        },
                    )
        return claimed_params

    async def _claim_params(
        self,
        play_type: PlayType,
        state: OrchestratorState,
        params: PlayParams,
        *,
        resource_keys: list[str] | None = None,
    ) -> PlayParams | None:
        keys = resource_keys or await self._resource_keys_for_params(play_type, state, params)
        if not keys:
            return params

        claim_group_id = _claim_group_id(params)
        if claim_group_id is not None:
            active = await self._claim_group_is_active(state.session_id, claim_group_id)
            if active is False:
                return None
            return dataclasses.replace(
                params,
                extras={**params.extras, "resource_keys": keys},
            )

        method = getattr(self._store, "acquire_work_claims", None)
        if not callable(method):
            return params
        claimed = await method(state.session_id, play_type.value, keys)
        if isinstance(claimed, str):
            return dataclasses.replace(
                params,
                extras={**params.extras, "claim_group_id": claimed, "resource_keys": keys},
            )
        if claimed is None:
            return None
        # Unconfigured AsyncMock in older unit tests: preserve legacy resolution.
        return params

    async def _claim_group_is_active(self, session_id: str, claim_group_id: str) -> bool | None:
        method = getattr(self._store, "work_claim_group_is_active", None)
        if not callable(method):
            return None
        active = await method(session_id, claim_group_id)
        return active if isinstance(active, bool) else None

    async def _resource_keys_for_params(
        self, play_type: PlayType, state: OrchestratorState, params: PlayParams
    ) -> list[str]:
        keys: list[str] = []
        if play_type in _PR_WORK_PLAY_TYPES and params.pr_number is not None:
            pr = await self._find_pr(state, params.pr_number)
            keys.extend(
                pr_resource_keys_for_pr(pr)
                if pr is not None
                else pr_resource_keys(params.pr_number, params.issue_number)
            )
        elif (
            play_type in _ISSUE_WORK_PLAY_TYPES
            or (play_type == PlayType.SYSTEMATIC_DEBUGGING and params.issue_number is not None)
        ) and params.issue_number is not None:
            keys.append(f"issue:{params.issue_number}")
        elif params.branch:
            keys.append(f"branch:{params.branch}")
        elif play_type == PlayType.RUN_QA:
            keys.append(f"session:{PlayType.RUN_QA.value}")

        # Trunk-scoped plays mutate the main checkout. Serialize them by
        # claiming a synthetic trunk:main_repo key — prevents concurrent
        # cleanup + merge_pr races that leave trunk dirty.
        if play_type in TRUNK_SCOPED_PLAYS and _TRUNK_RESOURCE_KEY not in keys:
            keys.append(_TRUNK_RESOURCE_KEY)
        return keys

    async def _find_pr(self, state: OrchestratorState, pr_number: int) -> object | None:
        for pr in state.pull_requests:
            if pr.pr_number == pr_number:
                return pr
        method = getattr(self._store, "get_pull_request", None)
        if not callable(method):
            return None
        pr = await method(state.session_id, pr_number)
        return pr if getattr(pr, "pr_number", None) == pr_number else None

    async def _issue_is_open(self, state: OrchestratorState, issue_number: int) -> bool:
        for issue in state.open_issues:
            if issue.issue_number == issue_number and issue.state.upper() == "OPEN":
                return True
        method = getattr(self._store, "get_github_issue", None)
        if not callable(method):
            return False
        issue = await method(issue_number, state.session_id)
        return bool(issue is not None and str(issue.state).upper() == "OPEN")

    async def _issue_is_open_and_available(
        self, state: OrchestratorState, issue_number: int
    ) -> bool:
        if not await self._issue_is_open(state, issue_number):
            return False
        if issue_number in _issue_numbers_with_merged_prs(state):
            return False
        in_progress = {
            linked_issue_number
            for pr in state.pull_requests
            if pr.state.upper() == "OPEN"
            for linked_issue_number in issue_numbers_for_pr(pr)
        }
        return issue_number not in in_progress

    async def _issue_is_debuggable(self, state: OrchestratorState, issue_number: int) -> bool:
        open_pr_issue_numbers = {
            linked_issue_number
            for pr in state.pull_requests
            if pr.state.upper() == "OPEN"
            for linked_issue_number in issue_numbers_for_pr(pr)
        }
        if issue_number in open_pr_issue_numbers:
            return False

        bead_in_progress = in_progress_issue_numbers(state)
        for issue in state.open_issues:
            if issue.issue_number == issue_number and issue.state.upper() == "OPEN":
                return issue_available_for_debug(
                    issue,
                    open_pr_issue_numbers=open_pr_issue_numbers,
                    merged_pr_issue_numbers=_issue_numbers_with_merged_prs(state),
                    in_flight_issue_numbers=set(state.in_flight_issues),
                    bead_in_progress_issue_numbers=bead_in_progress,
                )

        method = getattr(self._store, "get_github_issue", None)
        if not callable(method):
            return False
        issue = await method(issue_number, state.session_id)
        if issue is None or str(issue.state).upper() != "OPEN":
            return False
        labels = list(getattr(issue, "labels", []) or [])
        snapshot = IssueSnapshot(
            issue_number=issue.issue_number,
            title=getattr(issue, "title", ""),
            state=issue.state,
            priority=getattr(issue, "priority", None),
            labels=labels,
            source=getattr(issue, "source", None),
        )
        return issue_available_for_debug(
            snapshot,
            open_pr_issue_numbers=open_pr_issue_numbers,
            merged_pr_issue_numbers=_issue_numbers_with_merged_prs(state),
            in_flight_issue_numbers=set(state.in_flight_issues),
            bead_in_progress_issue_numbers=bead_in_progress,
        )

    async def _pending_review_for_pr(
        self, session_id: str, pr_number: int
    ) -> ReviewQueueRecord | None:
        pending = await self._store.list_pending_reviews(session_id)
        for row in pending:
            if row.pr_number == pr_number:
                return row
        return None

    @staticmethod
    def _pr_merge_ready(pr: object) -> bool:
        return pr_merge_ready(pr)

    @staticmethod
    def _pr_blocked(pr: object) -> bool:
        return pr_unblockable(pr)

    # -------------------------------------------------------------------------
    # Skill-backed play resolvers
    # -------------------------------------------------------------------------

    async def _claim_candidate(
        self,
        state: OrchestratorState,
        candidate: PlayCandidate,
    ) -> PlayParams | None:
        if candidate.play_type == PlayType.CODE_REVIEW:
            reviewer_agent_id = candidate.params.target_agent_id
            if reviewer_agent_id is None:
                return None
            return await self._claim_code_review_params(state, candidate.params, reviewer_agent_id)
        return await self._claim_params(
            candidate.play_type,
            state,
            candidate.params,
            resource_keys=list(candidate.resource_keys),
        )

    async def _resolve_unblock_pr(self, state: OrchestratorState) -> PlayParams | None:
        for candidate in await self._candidate_service.candidates_for(PlayType.UNBLOCK_PR, state):
            claimed = await self._claim_candidate(state, candidate)
            if claimed is not None:
                return claimed
        return None

    async def _resolve_write_implementation_plan(
        self, state: OrchestratorState
    ) -> PlayParams | None:
        for candidate in await self._candidate_service.candidates_for(
            PlayType.WRITE_IMPLEMENTATION_PLAN, state
        ):
            claimed = await self._claim_candidate(state, candidate)
            if claimed is not None:
                return claimed
        return None

    async def _resolve_systematic_debugging(self, state: OrchestratorState) -> PlayParams | None:
        for candidate in await self._candidate_service.candidates_for(
            PlayType.SYSTEMATIC_DEBUGGING, state
        ):
            claimed = await self._claim_candidate(state, candidate)
            if claimed is not None:
                return claimed
        return None

    async def _resolve_issue_pickup(self, state: OrchestratorState) -> PlayParams | None:
        """Pick the highest-priority eligible open issue.

        Eligibility filters (mirror the agentshore-issue-pickup skill's Step 2):

        - Skip CLOSED issues — should already be excluded by the open-issues
          query, but state may be stale right after a merge_pr.
        - Skip issues already covered by an open PR (in-progress).
        - Skip issues labeled with an AgentShore issue gate such as
          ``agentshore/blocked`` or ``agentshore/disallowed``.
        - Skip issues still labeled ``agentshore/needs-refinement`` — they have
          not been sized yet by ``agentshore-refine-tasks``.

        Ranking among remaining candidates:
        - When beads has ready tasks, prefer issues matching a bead's
          ``external_ref`` ("gh-{number}"). Falls back to priority ordering
          when no candidates match a ready bead.
        - ``priority/critical`` > ``high`` > ``medium`` > ``low`` > unset
        - then ``size/S`` > ``M`` > ``L`` > ``XL`` > unset
        - then lower issue number
        """
        for candidate in await self._candidate_service.candidates_for(PlayType.ISSUE_PICKUP, state):
            claimed = await self._claim_candidate(state, candidate)
            if claimed is not None:
                return claimed
        return None

    async def _eligible_issue_candidates(self, state: OrchestratorState) -> list[IssueSnapshot]:
        eligible_numbers = {
            candidate.params.issue_number
            for candidate in build_candidate_plan(state).candidates_for(PlayType.ISSUE_PICKUP)
            if candidate.params.issue_number is not None
        }
        return [issue for issue in state.open_issues if issue.issue_number in eligible_numbers]

    async def _resolve_refine_tasks(self, state: OrchestratorState) -> PlayParams | None:
        for candidate in await self._candidate_service.candidates_for(
            PlayType.REFINE_TASK_BREAKDOWN, state
        ):
            claimed = await self._claim_candidate(state, candidate)
            if claimed is not None:
                return claimed
        return None

    async def _resolve_code_review(self, state: OrchestratorState) -> PlayParams | None:
        """Pick the oldest pending review with an idle cross-identity reviewer.

        Deconfliction is identity-only: a candidate may review iff its
        ``github_identity`` differs from the PR author's GitHub login. Agent
        type plays no role — a human and an agent can share a login; two
        agents of the same type can have different logins. The resolver pins
        ``target_agent_id`` to a specific eligible agent and the executor
        re-checks the identity invariant at dispatch.

        When no eligible reviewer is idle for a PR, that PR is skipped (not
        dispatched with target=None) — dispatching against an ineligible pool
        only burns requeue attempts.
        """
        idle_reviewers = idle_can_review_agents(state)
        for candidate in await self._candidate_service.candidates_for(
            PlayType.CODE_REVIEW,
            state,
            idle_reviewers=idle_reviewers,
        ):
            claimed = await self._claim_candidate(state, candidate)
            if claimed is not None:
                return claimed
        return None

    async def _first_open_pr_with_reviewer(
        self,
        state: OrchestratorState,
        idle_reviewers: list[AgentSnapshot],
        *,
        excluded: set[int],
        limit: int = 5,
    ) -> PlayParams | None:
        """Code-review fallback: return the first open PR with a cross-identity reviewer.

        Mirrors ``_first_open_pr_matching`` but also pins ``target_agent_id``
        to the chosen reviewer. The general helper drops the pin and lets the
        selector pick freely, which can land on a same-identity agent and
        violate the anti-confirmation invariant.
        """
        if self._github is None:
            return None
        candidates = await self._candidate_service._github_code_review_candidates(
            state,
            idle_reviewers,
            excluded=excluded,
            source="github_fallback",
            limit=limit,
        )
        return candidates[0].params if candidates else None

    @overload
    async def _first_open_pr_matching(
        self,
        state: Callable[[PullRequestRecord], bool],
        predicate: None = None,
        *,
        limit: int = 5,
        log_key: str = "github_pr_resolve_failed",
    ) -> PlayParams | None: ...

    @overload
    async def _first_open_pr_matching(
        self,
        state: OrchestratorState,
        predicate: Callable[[PullRequestRecord], bool],
        *,
        limit: int = 5,
        log_key: str = "github_pr_resolve_failed",
    ) -> PlayParams | None: ...

    async def _first_open_pr_matching(
        self,
        state: OrchestratorState | Callable[[PullRequestRecord], bool],
        predicate: Callable[[PullRequestRecord], bool] | None = None,
        *,
        limit: int = 5,
        log_key: str = "github_pr_resolve_failed",
    ) -> PlayParams | None:
        """Return the first open GitHub PR for which *predicate* is True.

        Two call shapes (see ``@overload`` above):
        - ``(predicate)`` — predicate-only; synthesises a minimal state.
        - ``(state, predicate)`` — explicit state + predicate.

        Narrows via ``isinstance(state, OrchestratorState)`` instead of
        ``cast()`` so mypy verifies the assignment (GH #508 / desktop-1bv).
        """
        if self._github is None:
            return None
        candidate_state: OrchestratorState
        candidate_predicate: Callable[[PullRequestRecord], bool]
        if predicate is None:
            if isinstance(state, OrchestratorState):
                # Defensive: caller passed a state without a predicate.
                # Treat as "no predicate" → match every open PR.
                candidate_state = state
                candidate_predicate = lambda _pr: True  # noqa: E731
            else:
                candidate_state = OrchestratorState(
                    session_id="",
                    session_state=SessionState.RUNNING,
                    total_plays=0,
                    total_cost=0.0,
                )
                candidate_predicate = state
        else:
            if not isinstance(state, OrchestratorState):
                msg = (
                    "_first_open_pr_matching: when predicate is provided, "
                    "state must be a OrchestratorState"
                )
                raise TypeError(msg)
            candidate_state = state
            candidate_predicate = predicate
        candidates = await self._candidate_service._github_pr_candidates(
            candidate_state,
            PlayType.MERGE_PR,
            candidate_predicate,
            limit=limit,
            log_key=log_key,
        )
        return candidates[0].params if candidates else None

    async def _resolve_merge_pr(self, state: OrchestratorState) -> PlayParams | None:
        for candidate in await self._candidate_service.candidates_for(PlayType.MERGE_PR, state):
            claimed = await self._claim_candidate(state, candidate)
            if claimed is not None:
                return claimed
        return None

    async def _resolve_pr_from_github(self, state: OrchestratorState) -> PlayParams | None:
        """Query GitHub for open PRs when the DataStore has none cached."""
        candidates = await self._candidate_service._github_pr_candidates(
            state,
            PlayType.CODE_REVIEW,
            lambda _pr: True,
            limit=5,
            log_key="github_pr_resolve_failed",
        )
        return candidates[0].params if candidates else None

    async def _resolve_blocked_pr_from_github(
        self, state: OrchestratorState, *, exclude_pr_numbers: set[int] | None = None
    ) -> PlayParams | None:
        """Query GitHub for open PRs with requested changes, block labels, or failed checks."""
        _exclude = exclude_pr_numbers or set()

        candidates = await self._candidate_service._github_pr_candidates(
            state,
            PlayType.UNBLOCK_PR,
            lambda pr: pr.pr_number not in _exclude and pr_unblockable(pr),
            limit=20,
            log_key="github_blocked_pr_resolve_failed",
        )
        return candidates[0].params if candidates else None

    async def _resolve_run_qa(self, state: OrchestratorState) -> PlayParams | None:
        """Run QA against the default branch (the merged trunk).

        Per-PR validation lives in code_review; this play exercises the
        cumulative merged state. The skill auto-detects the repo's default
        branch when ``branch`` is unset, so we always return empty params
        — the cooldown gate and can_test capability still control rate.
        """
        return await self._claim_params(PlayType.RUN_QA, state, PlayParams())

    async def _resolve_browser_verification(self, state: OrchestratorState) -> PlayParams | None:
        branch = await self._store.get_most_recent_branch(state.session_id)
        if not branch:
            return None
        return await self._claim_params(
            PlayType.BROWSER_VERIFICATION, state, PlayParams(branch=branch)
        )
