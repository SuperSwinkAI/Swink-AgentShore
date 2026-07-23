"""Structured diagnostics for ``PPOSelector.select()`` — all-masked, resolver-
exhausted, terminal no-work, and reverse-failsafe logs.

Split out of ``rl/selector.py`` (TNQA wave-2 line-count reduction) so the
selector module stays focused on the select/confirm/repick loop. These are
pure functions parameterized on whatever ``PPOSelector`` already holds
(registry, orchestrator cfg, config index) rather than instance methods —
the one piece of real instance state they touch (the masked-plays-map dedup
digest) is threaded through as the ``masked_plays_log_field`` callback so
``PPOSelector`` keeps owning that cache.

``_diagnostic_snapshot`` dedupes the candidate-plan/mask-reasons/Counter
field building that ``log_all_masked``, ``log_resolver_exhausted``, and
``log_reverse_failsafe`` each used to repeat independently; every caller adds
only its own extra fields on top. Emitted log-event names and field sets are
unchanged from the pre-split versions.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from agentshore.plays.candidates import PlayCandidatePlan, build_candidate_plan
from agentshore.rl.action_space import V1_ACTION_ORDER
from agentshore.rl.mask import (
    TerminalNoWorkDecision,
    compute_config_mask,
    compute_mask_reasons,
    compute_terminal_no_work_decision,
)
from agentshore.rl.mask_reason import MaskReason, MaskSource
from agentshore.state import AgentStatus, PlayType, SessionState

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy as np
    from numpy.typing import NDArray

    from agentshore.config.models import RuntimeConfig
    from agentshore.plays.registry import PlayRegistry
    from agentshore.rl.config_head import ConfigKey
    from agentshore.state import OrchestratorState

_logger = structlog.get_logger(__name__)

_CAPACITY_WAIT_SOURCES = frozenset({MaskSource.ELIGIBILITY, MaskSource.CONFIG, MaskSource.SPAWN})

if TYPE_CHECKING:
    # Callback signature for the selector's stateful masked-plays-map dedup
    # cache (``PPOSelector._masked_plays_log_field``); kept on the selector
    # since the dedup digest is per-instance state, not a pure function of
    # its arguments.
    MaskedPlaysLogField = Callable[[dict[PlayType, MaskReason]], dict[str, object]]


def _is_capacity_wait(reason: MaskReason) -> bool:
    """Return True if ``reason`` represents a staffing or spawn-rate constraint.

    Eligibility gates (no idle agent, tier/exclude mismatches), config gates
    (no eligible configuration), and spawn-cooldown gates (``SPAWN`` source)
    are all considered capacity waits — the selector cannot do more until
    staffing or cooldown state changes. Reserved slots are structural noise
    and are handled by the caller.
    """
    return reason.source in _CAPACITY_WAIT_SOURCES


def _only_capacity_waiting(reason_counts: list[dict[str, object]]) -> bool:
    """Return True when all reported blockers are staffing/capacity waits."""
    if not reason_counts:
        return False
    saw_capacity = False
    for item in reason_counts:
        reason = item.get("reason")
        if not isinstance(reason, MaskReason):
            return False
        if reason.source == MaskSource.RESERVED:
            continue
        if _is_capacity_wait(reason):
            saw_capacity = True
            continue
        return False
    return saw_capacity


def _mask_reasons_by_play(reasons: dict[PlayType, MaskReason]) -> dict[str, str]:
    """Full, untruncated ``play -> reason`` map for the all-masked diagnostics.

    ``top_mask_reasons`` runs ``Counter(reasons.values()).most_common(5)``, which
    (a) collapses the ``play -> reason`` mapping into bare reason-string counts —
    discarding *which* play each reason blocked — and (b) truncates to five, so
    the reasons for lower-frequency but high-value work plays (``issue_pickup``,
    ``code_review``, ``merge_pr``, ``end_session`` …) silently vanish whenever the
    fleet is fully wedged (many distinct reasons, each count 1). That is exactly
    what made a real fleet-idle stall (noodle ``edab7597``) undiagnosable from the
    log alone. This map is keyed by play and bounded by ``NUM_ACTIONS`` (<= 22
    entries), so it is emitted in full — no truncation — and sorted for stable,
    greppable output.
    """
    return {
        pt.value: (
            f"{r.text} [{r.classification.value}/{r.source.value}]"
            if isinstance(r, MaskReason)
            # Defensive: this diagnostic runs in the select() hot path and must
            # never raise on a malformed reason (e.g. a non-typed precondition).
            else str(r)
        )
        for pt, r in sorted(reasons.items(), key=lambda kv: kv[0].value)
    }


@dataclass(frozen=True, slots=True)
class _DiagnosticSnapshot:
    """Shared candidate-plan/mask-reasons/Counter data for the mask diagnostics."""

    candidate_plan: PlayCandidatePlan
    reasons: dict[PlayType, MaskReason]
    reason_counts: list[dict[str, object]]


def _diagnostic_snapshot(
    state: OrchestratorState,
    registry: PlayRegistry,
    *,
    cfg: RuntimeConfig | None,
    config_index: tuple[ConfigKey, ...] | None,
    candidate_plan: PlayCandidatePlan | None = None,
    apply_reverse_failsafe: bool = False,
) -> _DiagnosticSnapshot:
    """Build the candidate plan, full mask-reasons map, and top-5 Counter once.

    Every diagnostic logger below needs the same three derived values; this is
    the one place that computes them so the callers only add their own extra
    log fields on top.
    """
    plan = candidate_plan or build_candidate_plan(state)
    reasons = compute_mask_reasons(
        state,
        registry,
        cfg=cfg,
        config_index=config_index,
        apply_reverse_failsafe=apply_reverse_failsafe,
        candidate_plan=plan,
    )
    reason_counts = [
        {"reason": reason, "count": count}
        for reason, count in Counter(reasons.values()).most_common(5)
    ]
    return _DiagnosticSnapshot(candidate_plan=plan, reasons=reasons, reason_counts=reason_counts)


def log_all_masked(
    state: OrchestratorState,
    registry: PlayRegistry,
    *,
    cfg: RuntimeConfig | None,
    config_index: tuple[ConfigKey, ...] | None,
    masked_plays_log_field: MaskedPlaysLogField,
) -> None:
    """Emit an actionable all-masked diagnostic with severity based on capacity."""
    idle_count = sum(1 for a in state.agents if a.status == AgentStatus.IDLE)
    busy_count = sum(1 for a in state.agents if a.status == AgentStatus.BUSY)
    error_count = sum(1 for a in state.agents if a.status == AgentStatus.ERROR)
    terminated_count = sum(1 for a in state.agents if a.status == AgentStatus.TERMINATED)
    candidate_plan = build_candidate_plan(state)
    terminal_no_work = compute_terminal_no_work_decision(
        state,
        registry,
        cfg=cfg,
        config_index=config_index,
        candidate_plan=candidate_plan,
    )
    work = (
        terminal_no_work.availability
        if terminal_no_work is not None
        else candidate_plan.work_availability
    )

    snapshot = _diagnostic_snapshot(
        state, registry, cfg=cfg, config_index=config_index, candidate_plan=candidate_plan
    )
    reasons = snapshot.reasons
    reason_counts = snapshot.reason_counts

    spawnable_config_count: int | None = None
    if cfg is not None and config_index:
        spawnable_config_count = int(compute_config_mask(state, cfg, config_index).sum())

    log = (
        _logger.debug
        if _only_capacity_waiting(reason_counts) or (idle_count == 0 and busy_count > 0)
        else _logger.warning
    )
    log(
        "ppo_selector.all_masked",
        idle_agents=idle_count,
        busy_agents=busy_count,
        error_agents=error_count,
        terminated_agents=terminated_count,
        tracked_issues=work.tracked_issue_count,
        github_open_issues=work.github_open_issue_count,
        workable_issues=work.workable_issue_count,
        implementation_eligible=work.implementation_eligible_count,
        bead_in_progress_issues=work.bead_in_progress_issue_count,
        ready_tasks=work.ready_task_count,
        backlog_sync_work=work.backlog_sync_work_count,
        beads_blocks_issue_pickup=work.beads_blocks_issue_pickup,
        actionable_pr_work=work.actionable_pr_work_count,
        open_issues=len(state.open_issues),
        open_prs=len(state.pull_requests),
        spawnable_config_count=spawnable_config_count,
        top_mask_reasons=reason_counts,
        **masked_plays_log_field(reasons),
    )


def log_terminal_no_work(
    state: OrchestratorState,
    decision: TerminalNoWorkDecision,
) -> None:
    """Emit a structured terminal no-work mask diagnostic."""
    availability = decision.availability
    _logger.info(
        "ppo_selector.terminal_no_work",
        terminal_reason="no_workable_work_remaining",
        terminal_mask_mode=decision.mode,
        tracked_issues=availability.tracked_issue_count,
        github_open_issues=availability.github_open_issue_count,
        workable_issues=availability.workable_issue_count,
        implementation_eligible=availability.implementation_eligible_count,
        bead_in_progress_issues=availability.bead_in_progress_issue_count,
        ready_tasks=availability.ready_task_count,
        backlog_sync_work=availability.backlog_sync_work_count,
        actionable_pr_work=availability.actionable_pr_work_count,
        qa_plays_since_last=decision.qa_plays_since_last,
        in_flight_plays=len(state.in_flight_plays),
    )


def log_resolver_exhausted(
    state: OrchestratorState,
    registry: PlayRegistry,
    mask: NDArray[np.bool_],
    *,
    cfg: RuntimeConfig | None,
    config_index: tuple[ConfigKey, ...] | None,
    attempted_plays: list[str],
    reason: str,
    attempt: int,
    candidate_plan: PlayCandidatePlan | None,
    masked_plays_log_field: MaskedPlaysLogField,
) -> None:
    """Warn when the policy had legal actions but no resolver produced parameters."""
    snapshot = _diagnostic_snapshot(
        state,
        registry,
        cfg=cfg,
        config_index=config_index,
        candidate_plan=candidate_plan,
        apply_reverse_failsafe=False,
    )
    work = snapshot.candidate_plan.work_availability
    reasons = snapshot.reasons
    reason_counts = snapshot.reason_counts
    allowed_plays = [pt.value for i, pt in enumerate(V1_ACTION_ORDER) if bool(mask[i])]
    # During drain the only legal action is end_agent; if no agent qualifies
    # (e.g. one is still finishing its last play) the resolver harmlessly
    # cycles every selector tick. Emit at info-level so the drain doesn't
    # produce a warning storm.
    is_draining = state.session_state in (
        SessionState.DRAINING,
        SessionState.SHUTTING_DOWN,
    )
    log_fn = _logger.info if is_draining else _logger.warning
    log_fn(
        "ppo_selector.resolver_exhausted",
        reason=reason,
        attempt=attempt,
        attempted_plays=attempted_plays,
        mask_allowed_plays=allowed_plays,
        tracked_issues=work.tracked_issue_count,
        github_open_issues=work.github_open_issue_count,
        workable_issues=work.workable_issue_count,
        implementation_eligible=work.implementation_eligible_count,
        bead_in_progress_issues=work.bead_in_progress_issue_count,
        ready_tasks=work.ready_task_count,
        backlog_sync_work=work.backlog_sync_work_count,
        actionable_pr_work=work.actionable_pr_work_count,
        top_mask_reasons=reason_counts,
        **masked_plays_log_field(reasons),
    )


def log_reverse_failsafe(
    state: OrchestratorState,
    registry: PlayRegistry,
    mask: NDArray[np.bool_],
    *,
    cfg: RuntimeConfig | None,
    config_index: tuple[ConfigKey, ...] | None,
    auto_enabled: bool,
    no_available_play_ticks: int,
    masked_plays_log_field: MaskedPlaysLogField,
) -> None:
    """Emit diagnostics when the all-masked reverse failsafe opens fallback actions."""
    snapshot = _diagnostic_snapshot(
        state,
        registry,
        cfg=cfg,
        config_index=config_index,
        apply_reverse_failsafe=False,
    )
    reasons = snapshot.reasons
    reason_counts = snapshot.reason_counts
    unmasked = [pt.value for i, pt in enumerate(V1_ACTION_ORDER) if mask[i]]
    _logger.warning(
        "ppo_selector.reverse_failsafe_unmasked",
        idle_agents=sum(1 for a in state.agents if a.status == AgentStatus.IDLE),
        busy_agents=sum(1 for a in state.agents if a.status == AgentStatus.BUSY),
        error_agents=sum(1 for a in state.agents if a.status == AgentStatus.ERROR),
        open_issues=len(state.open_issues),
        open_prs=len(state.pull_requests),
        top_mask_reasons=reason_counts,
        **masked_plays_log_field(reasons),
        unmasked_plays=unmasked,
        auto_enabled=auto_enabled,
        no_available_play_ticks=no_available_play_ticks,
    )
