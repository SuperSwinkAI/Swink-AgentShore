"""Agent selection helpers — pure rule chain for the AgentManager.

Rule chain (applied in order):
  0a. Required-id pin (hard): if ``target_agent_id`` is set, narrow to that
      single handle. Used by the resolver to pin code-review dispatch to a
      specific agent whose GH identity has been verified upstream.
  0b. Required-type pin (hard): if ``target_agent_type`` is set (and no id pin),
      narrow to that type. Used by ``instantiate_agent`` and similar
      type-specific plays.
  1. Anti-confirmation bias (hard): exclude the PR author from CodeReview.
     QA runs against the merged trunk and has no anti-confirmation; any
     can_test agent may execute it.
  2. Exclude list (hard): drop agent types listed in ``preferences.exclude``
     for this play type.
  3. Tier eligibility (hard): drop agents whose ``model_tier`` isn't in the
     allowed set for this play type. Small tier is blocked from any coding
     or strategic play; large tier is blocked from cheap mechanical plays
     where the play is explicitly tier-limited.
  4. AntiConfirmationViolation if no candidates remain after hard filters.
  5. Branch exposure affinity (soft): promote agents with prior exposure to *branch*.
  6. Type affinity (soft): promote agents whose type matches
     ``preferences.affinity`` for this play type.
  7. Tier cost (soft): prefer cheaper eligible tiers when affinity is tied.
  8. Least-busy tiebreaker: sort by ascending task history length.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from agentshore.agents.model_tiers import DEFAULT_MODEL_TIER
from agentshore.errors import AntiConfirmationViolation
from agentshore.identity_names import same_identity
from agentshore.state import AgentStatus, PlayType, is_agent_circuit_broken

if TYPE_CHECKING:
    from agentshore.agents.handle import AgentHandle
    from agentshore.config import AgentPreferencesConfig

_logger = structlog.get_logger(__name__)

_REVIEW_PLAYS: frozenset[PlayType] = frozenset({PlayType.CODE_REVIEW})

_PLAY_ALLOWED_TIERS: dict[PlayType, frozenset[str]] = {
    PlayType.CLEANUP: frozenset({"small", "medium"}),
    PlayType.PRUNE: frozenset({"small", "medium"}),
    PlayType.MERGE_PR: frozenset({"small", "medium"}),
    PlayType.ISSUE_PICKUP: frozenset({"medium", "large"}),
    PlayType.UNBLOCK_PR: frozenset({"large", "medium"}),
    PlayType.REFINE_TASK_BREAKDOWN: frozenset({"medium", "large"}),
    PlayType.CODE_REVIEW: frozenset({"large"}),
    PlayType.RUN_QA: frozenset({"large"}),
    PlayType.WRITE_IMPLEMENTATION_PLAN: frozenset({"large"}),
    PlayType.SYSTEMATIC_DEBUGGING: frozenset({"medium", "large"}),
    PlayType.SEED_PROJECT: frozenset({"large"}),
    PlayType.DESIGN_AUDIT: frozenset({"large"}),
    PlayType.GROOM_BACKLOG: frozenset({"medium", "large"}),
    PlayType.CALIBRATE_ALIGNMENT: frozenset({"large"}),
    PlayType.RECONCILE_STATE: frozenset({"medium", "large"}),
}


def allowed_tiers_for(play_type: PlayType) -> frozenset[str] | None:
    """Return the allowed tier set for *play_type*, or None if unrestricted."""
    return _PLAY_ALLOWED_TIERS.get(play_type)


def select_agent_for(
    play_type: PlayType,
    handles: dict[str, AgentHandle],
    *,
    pr_github_author: str | None = None,
    branch_exposure: dict[str, str] | None = None,
    preferences: AgentPreferencesConfig | None = None,
    branch: str | None = None,
    required_agent_type: str | None = None,
    required_agent_id: str | None = None,
) -> AgentHandle:
    """Return the best available handle for *play_type* using the rule chain.

    Raises ``AntiConfirmationViolation`` if all candidates are blocked by
    hard constraints (anti-confirmation or exclude rules).

    Raises ``AntiConfirmationViolation`` (with a distinct message) if there
    are no IDLE agents at all.
    """
    branch_exposure = branch_exposure or {}

    candidates: list[AgentHandle] = [h for h in handles.values() if h.status == AgentStatus.IDLE]

    if not candidates:
        raise AntiConfirmationViolation("No IDLE agents available for selection")

    if required_agent_id is not None:
        candidates = [h for h in candidates if h.agent_id == required_agent_id]
        if not candidates:
            raise AntiConfirmationViolation(f"Pinned agent {required_agent_id!r} is no longer IDLE")

    elif required_agent_type is not None:
        candidates = [h for h in candidates if h.agent_type.value == required_agent_type]
        if not candidates:
            raise AntiConfirmationViolation(
                f"No IDLE agents of required type {required_agent_type!r} available"
            )

    initial_count = len(candidates)

    eliminated: dict[str, list[str]] = {
        "anti_confirmation": [],
        "exclude": [],
        "tier": [],
    }

    blocked_ids: set[str] = set()

    if play_type in _REVIEW_PLAYS and pr_github_author is not None:
        for h in candidates:
            if same_identity(h.github_identity, pr_github_author):
                blocked_ids.add(h.agent_id)

    survivors: list[AgentHandle] = []
    for h in candidates:
        if h.agent_id in blocked_ids:
            eliminated["anti_confirmation"].append(h.agent_id)
        else:
            survivors.append(h)
    candidates = survivors

    excluded_types: set[str] = set()
    if preferences is not None:
        for exc_type in preferences.exclude.get(play_type.value, []):
            excluded_types.add(exc_type)

    if excluded_types:
        survivors = []
        for h in candidates:
            if h.agent_type.value in excluded_types:
                eliminated["exclude"].append(h.agent_id)
            else:
                survivors.append(h)
        candidates = survivors

    allowed_tiers = _PLAY_ALLOWED_TIERS.get(play_type)
    if allowed_tiers is not None:
        survivors = []
        for h in candidates:
            if (h.model_tier or DEFAULT_MODEL_TIER) not in allowed_tiers:
                eliminated["tier"].append(h.agent_id)
            else:
                survivors.append(h)
        candidates = survivors

    if not candidates:
        _logger.warning(
            "agent_selection_blocked",
            play_type=play_type.value,
            eliminated=eliminated,
            candidate_count_in=initial_count,
        )
        raise AntiConfirmationViolation(
            f"All agents blocked for {play_type.value!r} — "
            "anti-confirmation, exclude, or tier-eligibility rules eliminated all candidates"
        )

    preferred_type: str | None = None
    if preferences is not None:
        preferred_type = preferences.affinity.get(play_type.value)

    branch_exposed_ids: set[str] = set()
    if branch is not None:
        exposed = branch_exposure.get(branch)
        if exposed:
            branch_exposed_ids.add(exposed)

    tier_rank = {"small": 0, "medium": 1, "large": 2}

    def _score(h: AgentHandle) -> tuple[int, int, int, int, int]:
        task_history = h.task_history
        successes = sum(1 for t in task_history if t.success)
        failures = len(task_history) - successes
        circuit_broken_score = (
            1
            if is_agent_circuit_broken(
                tasks_completed=successes,
                tasks_failed=failures,
                timeout_count=h.timeout_count,
                consecutive_timeouts=h.consecutive_timeouts,
            )
            else 0
        )
        branch_exposure_score = 0 if h.agent_id in branch_exposed_ids else 1
        type_score = 0 if (preferred_type and h.agent_type.value == preferred_type) else 1
        tier_score = tier_rank.get(h.model_tier or DEFAULT_MODEL_TIER, tier_rank["medium"])
        busy_score = len(h.task_history)
        return (circuit_broken_score, branch_exposure_score, type_score, tier_score, busy_score)

    candidates.sort(key=_score)
    return candidates[0]
