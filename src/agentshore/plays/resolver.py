"""Parameter resolver — derives PlayParams from current session state."""

from __future__ import annotations

import dataclasses
from collections import Counter
from typing import TYPE_CHECKING

from agentshore.agents.model_tiers import (
    DEFAULT_MODEL_TIER,
    MODEL_TIER_PRIORITY,
    effective_model_tier_config,
    enabled_model_tiers,
)
from agentshore.agents.worktree import TRUNK_MUTATING_PLAYS, TRUNK_SCOPED_PLAYS
from agentshore.logging import get_logger
from agentshore.plays.base import PlayParams
from agentshore.plays.candidates import (
    PlayCandidate,
    PlayCandidateService,
    idle_can_review_agents,
    pick_reviewer_for_pr,
    pr_resource_keys,
    pr_resource_keys_for_pr,
)
from agentshore.plays.internal.end_agent import _MIN_PLAYS_PER_AGENT
from agentshore.state import (
    RECOVERABLE_ERROR_CLASSES,
    AgentStatus,
    AgentType,
    OrchestratorState,
    PlayType,
    SessionState,
)

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.agents.manager import AgentManager
    from agentshore.config import RuntimeConfig
    from agentshore.data.models import PullRequestRecord, ReviewQueueRecord
    from agentshore.data.store import DataStore
    from agentshore.github.adapter import GitHubAdapter
    from agentshore.state import AgentSnapshot, PullRequestSnapshot

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

# Trunk-scoped plays that do NOT mutate trunk and are not issue/PR-scoped
# (run_qa, design_audit, calibrate_alignment, groom_backlog, seed_project).
# They still serialize against *themselves* via a session-scoped key — so two
# of the same metadata play can't race on beads/issue writes — but they no
# longer take the exclusive trunk:main_repo writer lock, which used to starve
# merge_pr for 10–20 min at a stretch (issue #17). The issue-scoped planning
# plays (write_implementation_plan, refine_task_breakdown) are excluded: they
# serialize per-issue via their issue:<n> key and must stay parallel.
_SELF_SERIALIZED_TRUNK_PLAYS = (TRUNK_SCOPED_PLAYS - TRUNK_MUTATING_PLAYS) - _ISSUE_WORK_PLAY_TYPES

_idle_can_review_agents = idle_can_review_agents
_pick_reviewer_for_pr = pick_reviewer_for_pr


# ---------------------------------------------------------------------------
# Override-resolution dispatch table (mirrors _SkillSpec in dispatch.py)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _OverrideSpec:
    """Per-play-type override-resolution rule.

    ``required_field``: PlayParams attribute that must be non-None for the
    targeted path; when None the path falls through to a re-resolve from scratch
    (unless ``branch_bypass`` applies).
    ``use_specific_pr``: True → call ``_resolve_specific_pr``; False → ``_claim_params``.
    ``branch_bypass``: True → when ``required_field`` is None but ``branch`` is
    set, re-run with ``bypass_preconditions=True`` (SYSTEMATIC_DEBUGGING).
    """

    required_field: str
    use_specific_pr: bool = False
    branch_bypass: bool = False


_OVERRIDE_SPECS: dict[PlayType, _OverrideSpec] = {
    # PR-work plays require pr_number; missing → fresh resolve.
    **{pt: _OverrideSpec("pr_number", use_specific_pr=True) for pt in _PR_WORK_PLAY_TYPES},
    # Issue-work plays require issue_number; missing → fresh resolve.
    **{pt: _OverrideSpec("issue_number") for pt in _ISSUE_WORK_PLAY_TYPES},
    # SYSTEMATIC_DEBUGGING: issue_number primary; branch-only path bypasses preconditions.
    PlayType.SYSTEMATIC_DEBUGGING: _OverrideSpec("issue_number", branch_bypass=True),
}


def _claim_group_id(params: PlayParams) -> str | None:
    raw = params.extras.get("claim_group_id")
    return raw if isinstance(raw, str) and raw else None


def _review_queue_id(params: PlayParams) -> int | None:
    raw = params.extras.get("review_queue_id")
    return raw if isinstance(raw, int) else None


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

    @property
    def project_path(self) -> Path | None:
        """Repository path for live reads (e.g. beads ``load_graph``)."""
        return self._project_path

    async def release_claim(self, state: OrchestratorState, params: PlayParams) -> None:
        """Release the work-claim group held by ``params``, if any.

        Used by the selector when a live ``confirm()`` rejects a play whose
        target was already claimed during resolution — the claim must be freed
        so the resource isn't held by a play that will never dispatch. A no-op
        when ``params`` carries no claim group.
        """
        claim_group_id = _claim_group_id(params)
        if claim_group_id is None:
            return
        await self._store.release_work_claim_group(state.session_id, claim_group_id)

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
            # -- skill-backed plays (candidate-loop resolution) --------------
            case (
                PlayType.REFINE_TASK_BREAKDOWN
                | PlayType.UNBLOCK_PR
                | PlayType.WRITE_IMPLEMENTATION_PLAN
                | PlayType.SYSTEMATIC_DEBUGGING
                | PlayType.ISSUE_PICKUP
                | PlayType.MERGE_PR
            ):
                return await self._resolve_via_candidates(play_type, state)
            case PlayType.CODE_REVIEW:
                return await self._resolve_via_candidates(
                    play_type, state, idle_reviewers=idle_can_review_agents(state)
                )
            case PlayType.RUN_QA:
                return await self._resolve_run_qa(state)
            # -- no-arg plays -------------------------------------------------
            case _:
                # END_SESSION, CLEANUP, TAKE_BREAK, GROOM_BACKLOG,
                # CALIBRATE_ALIGNMENT, DESIGN_AUDIT, RECONCILE_STATE, PRUNE,
                # FUTURE_4/7/8
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
            and agent.last_error_class in RECOVERABLE_ERROR_CLASSES
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
        # Count only live agents (not ERROR / TERMINATED), mirroring the
        # capacity definition in instantiate_agent.execute() and the eligibility
        # config mask. Counting dead agents here let the resolver both skip
        # reclaimable cells and deterministically re-pick a cell that is already
        # at its live per-tier max — the latter is #159's instantiate spin
        # (mask allows INSTANTIATE_AGENT, then execute() rejects "at per-tier
        # max"). The resolver must only ever return a cell execute() will accept.
        existing = Counter(
            (s.agent_type.value, s.model_tier or DEFAULT_MODEL_TIER)
            for s in state.agents
            if s.status.value not in ("error", "terminated")
        )
        idle_configs = {
            (s.agent_type.value, s.model_tier or DEFAULT_MODEL_TIER)
            for s in state.agents
            if s.status == AgentStatus.IDLE
        }
        enabled_agents = []
        tier_caps: dict[tuple[str, str], int] = {}
        for agent_key, agent_cfg in self._cfg.agents.items():
            try:
                agent_type = AgentType(agent_key)
            except ValueError:
                continue
            if not agent_cfg.enabled:
                continue
            enabled_tiers = enabled_model_tiers(agent_type, agent_cfg)
            enabled_agents.append((agent_type, enabled_tiers))
            for tier in enabled_tiers:
                tier_caps[(agent_type.value, tier)] = effective_model_tier_config(
                    agent_type, agent_cfg, tier
                ).max

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
            available_candidates = [
                pair
                for pair in candidates
                if pair not in idle_configs and existing[pair] < tier_caps.get(pair, 1)
            ]
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

        # A non-recoverable ERROR agent (auth/invalid_model/crash_*/timeout/...)
        # has no recovery path; the mask unmasks END_AGENT for it
        # (``_has_terminal_error_agent``). Target it directly and bypass the
        # min-plays / two-agent precondition so the PPO's retire choice
        # dispatches instead of resolving to no target (#20).
        terminal_error = [
            s
            for s in state.agents
            if s.status == AgentStatus.ERROR and s.last_error_class not in RECOVERABLE_ERROR_CLASSES
        ]
        if terminal_error:
            return PlayParams(agent_id=terminal_error[0].agent_id, bypass_preconditions=True)

        if state.session_state == SessionState.DRAINING:
            # During drain, recovery (take_break) is masked, so a recoverable-
            # ERROR agent (e.g. a BUSY agent reaped mid-play -> exit 143 ->
            # ERROR/"unknown") never reaches IDLE or recovery_exhausted. Left
            # alone it wedges drain forever, since should_terminate requires all
            # agents TERMINATED (#30). Retire any ERROR agent immediately —
            # clear() tears it down whatever status it is in.
            errored = [s for s in state.agents if s.status == AgentStatus.ERROR]
            if errored:
                return PlayParams(agent_id=errored[0].agent_id, bypass_preconditions=True)
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
        """Treat override params as target constraints, then claim them.

        Validity (issue open/available, debuggable, PR merge-ready/unblockable/
        review-needed) is owned by the EligibilityAuthority's confirm(); this
        path no longer re-decides eligibility — it enumerates the target and
        acquires the work claim, returning None on a claim-CAS loss so the
        selector treats it as a clean re-pick.
        """
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

        spec = _OVERRIDE_SPECS.get(play_type)
        if spec is not None:
            field_val = getattr(override, spec.required_field)
            if field_val is None:
                if spec.branch_bypass and override.branch is not None:
                    # SYSTEMATIC_DEBUGGING with branch-only: bypass preconditions.
                    override = dataclasses.replace(override, bypass_preconditions=True)
                    return await self._claim_params(play_type, state, override)
                return await self.resolve(play_type, state, override=PlayParams())
            if spec.use_specific_pr:
                return await self._resolve_specific_pr(play_type, state, override)
            return await self._claim_params(play_type, state, override)

        return await self._claim_params(play_type, state, override)

    async def _resolve_specific_pr(
        self, play_type: PlayType, state: OrchestratorState, params: PlayParams
    ) -> PlayParams | None:
        pr_number = params.pr_number
        if pr_number is None:
            return None
        pr = await self._find_pr(state, pr_number)
        # Target enumeration only: locate the PR so claim resource keys can be
        # derived. PR validity (merge-ready, unblockable, review-needed, draft)
        # is owned by the EligibilityAuthority's confirm(); the resolver no
        # longer re-decides it here.
        if pr is None:
            return None

        if play_type == PlayType.MERGE_PR:
            return await self._claim_params(play_type, state, params)

        if play_type == PlayType.UNBLOCK_PR:
            return await self._claim_params(play_type, state, params)

        if play_type == PlayType.CODE_REVIEW:
            # Reviewer selection is target enumeration for the claim key, not an
            # eligibility decision; the executor's _select_skill_agent remains
            # the anti-confirmation backstop. Returning None on no idle reviewer
            # is a clean re-pick, not an eligibility rejection.
            idle_reviewers = idle_can_review_agents(state)
            reviewer = pick_reviewer_for_pr(pr.github_author, idle_reviewers)
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
                claimed = await self._store.claim_review_with_work_claims(
                    session_id=state.session_id,
                    queue_id=pending.queue_id,
                    agent_id=reviewer_agent_id,
                    play_type=PlayType.CODE_REVIEW.value,
                    resource_keys=resource_keys,
                )
                if claimed is not None:
                    return dataclasses.replace(
                        params,
                        extras={
                            **params.extras,
                            "claim_group_id": claimed,
                            "resource_keys": resource_keys,
                            "review_queue_id": pending.queue_id,
                        },
                    )
                return None

        claimed_params = await self._claim_params(
            PlayType.CODE_REVIEW, state, params, resource_keys=resource_keys
        )
        if claimed_params is None:
            return None

        if review_queue_id is None:
            claimed_review = await self._store.claim_pending_review_for_pr(
                state.session_id, params.pr_number, reviewer_agent_id
            )
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

        claimed = await self._store.acquire_work_claims(state.session_id, play_type.value, keys)
        if claimed is None:
            return None
        return dataclasses.replace(
            params,
            extras={**params.extras, "claim_group_id": claimed, "resource_keys": keys},
        )

    async def _claim_group_is_active(self, session_id: str, claim_group_id: str) -> bool:
        return await self._store.work_claim_group_is_active(session_id, claim_group_id)

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
        elif play_type in _SELF_SERIALIZED_TRUNK_PLAYS:
            keys.append(f"session:{play_type.value}")

        # Trunk-*mutating* plays (merge_pr, cleanup, reconcile_state) serialize
        # on a synthetic trunk:main_repo key — prevents concurrent mutations
        # that leave trunk dirty/half-merged. Read-only trunk-scoped plays
        # (run_qa, design_audit, calibrate_alignment, groom_backlog,
        # seed_project) deliberately do NOT claim it: they only read the
        # checkout / update beads + issues, and holding the writer lock for
        # their multi-minute duration starved merge_pr (issue #17).
        if play_type in TRUNK_MUTATING_PLAYS and _TRUNK_RESOURCE_KEY not in keys:
            keys.append(_TRUNK_RESOURCE_KEY)
        return keys

    async def _find_pr(
        self, state: OrchestratorState, pr_number: int
    ) -> PullRequestSnapshot | PullRequestRecord | None:
        for pr in state.pull_requests:
            if pr.pr_number == pr_number:
                return pr
        record = await self._store.get_pull_request(state.session_id, pr_number)
        return record if record is not None and record.pr_number == pr_number else None

    async def _pending_review_for_pr(
        self, session_id: str, pr_number: int
    ) -> ReviewQueueRecord | None:
        pending = await self._store.list_pending_reviews(session_id)
        for row in pending:
            if row.pr_number == pr_number:
                return row
        return None

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

    async def _resolve_via_candidates(
        self,
        play_type: PlayType,
        state: OrchestratorState,
        *,
        idle_reviewers: list[AgentSnapshot] | None = None,
    ) -> PlayParams | None:
        """Resolve a skill-backed play by claiming the first claimable candidate.

        The candidate ordering and eligibility live entirely in
        ``PlayCandidateService.candidates_for`` (see ``candidates.py``); this
        loop only walks that ranked list and acquires the work claim for the
        first candidate that can be claimed. ``CODE_REVIEW`` passes its idle
        cross-identity reviewer pool so the candidate service can pin a
        ``target_agent_id`` per PR.
        """
        for candidate in await self._candidate_service.candidates_for(
            play_type, state, idle_reviewers=idle_reviewers
        ):
            claimed = await self._claim_candidate(state, candidate)
            if claimed is not None:
                return claimed
        return None

    async def _resolve_run_qa(self, state: OrchestratorState) -> PlayParams | None:
        """Run QA against the default branch (the merged trunk).

        Per-PR validation lives in code_review; this play exercises the
        cumulative merged state. The skill auto-detects the repo's default
        branch when ``branch`` is unset, so we always return empty params
        — the cooldown gate and can_test capability still control rate.
        """
        return await self._claim_params(PlayType.RUN_QA, state, PlayParams())
