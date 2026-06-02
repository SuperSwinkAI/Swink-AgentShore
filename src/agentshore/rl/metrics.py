"""MetricsEngine — builds ObservationContext from OrchestratorState + DataStore history.

Full recomputation per snapshot call. Designed to complete in <50ms.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from agentshore.rl.constants import STAGNATION_ENTROPY_MULTIPLIER
from agentshore.rl.observation import ObservationContext
from agentshore.state import AgentPlaySpecializationSnapshot, AgentStatus, PlayType

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agentshore.data.models import (
        GitHubIssueRecord,
        HandoffRecord,
        PullRequestRecord,
        SessionLearningRecord,
    )
    from agentshore.data.store import DataStore, PlayRecord
    from agentshore.state import OrchestratorState

_logger = structlog.get_logger(__name__)

_HIST_LEN = 5
_ROLLING_WINDOW = 10  # plays to average over
_AGENTSHORE_ISSUE_SOURCE_LABELS = frozenset(
    {"agentshore/intake", "agentshore/qa", "agentshore/review"}
)


def _is_agentshore_created_issue(issue: GitHubIssueRecord) -> bool:
    """Return whether an issue should count as AgentShore-created this session."""
    source = (issue.source or "").strip().lower()
    if source in _AGENTSHORE_ISSUE_SOURCE_LABELS:
        return True
    return any(label.strip().lower() in _AGENTSHORE_ISSUE_SOURCE_LABELS for label in issue.labels)


def _count_session_created_issues(issues: Sequence[GitHubIssueRecord]) -> int:
    return sum(1 for issue in issues if _is_agentshore_created_issue(issue))


def compute_agent_specialization(
    history: Sequence[PlayRecord],
    *,
    rolling_window: int = _ROLLING_WINDOW,
) -> list[AgentPlaySpecializationSnapshot]:
    """Group play history by (agent_id, play_type) into specialization cells.

    Plays with ``agent_id is None`` are skipped (no agent attribution possible).
    The result is sorted by ``(agent_id, play_type_value)`` so encoders and
    serialisers see a deterministic order regardless of insertion sequence.
    Unknown play-type strings are preserved as strings (rather than coerced) so
    legacy ``plays.play_type`` values stay visible in reports.
    """
    buckets: dict[tuple[str, str], list[bool]] = {}
    for play in history:
        agent_id = play.agent_id
        play_type_raw = play.play_type
        # Hard-fail-safe: reject anything that isn't a real (str, str). Tests
        # often stub the play history with MagicMock — those objects make every
        # attribute a Mock, which then breaks tuple sorting downstream.
        if not isinstance(agent_id, str) or not isinstance(play_type_raw, str):
            continue
        buckets.setdefault((agent_id, play_type_raw), []).append(bool(play.success))

    cells: list[AgentPlaySpecializationSnapshot] = []
    for (agent_id, raw_play_type), outcomes in buckets.items():
        total = len(outcomes)
        successful = sum(1 for ok in outcomes if ok)
        failed = total - successful
        recent = outcomes[-rolling_window:] if total > rolling_window else outcomes
        rolling = (sum(1 for ok in recent if ok) / len(recent)) if recent else 0.0
        play_type: PlayType | str
        try:
            play_type = PlayType(raw_play_type)
        except ValueError:
            play_type = raw_play_type
        cells.append(
            AgentPlaySpecializationSnapshot(
                agent_id=agent_id,
                play_type=play_type,
                total=total,
                successful=successful,
                failed=failed,
                success_rate=successful / total if total else 0.0,
                rolling_success_rate=rolling,
            )
        )

    def _sort_key(cell: AgentPlaySpecializationSnapshot) -> tuple[str, str]:
        pt = cell.play_type.value if isinstance(cell.play_type, PlayType) else cell.play_type
        return (cell.agent_id, pt)

    cells.sort(key=_sort_key)
    return cells


class MetricsEngine:
    """Computes ObservationContext from session history.

    Usage::

        engine = MetricsEngine(store=store, session_id=sid)
        ctx = await engine.snapshot(state)
    """

    def __init__(
        self,
        *,
        store: DataStore,
        session_id: str,
        stagnation_warn_after: int = 5,
        velocity_provider: Callable[[int], float] | None = None,
        executor_skip_rate_provider: Callable[[], float] | None = None,
    ) -> None:
        self._store = store
        self._session_id = session_id
        self._stagnation_warn_after = stagnation_warn_after
        self._velocity_provider = velocity_provider
        # v0.15 Phase 5: orchestrator-owned rate over the last 50 executor outcomes.
        # Skipped plays aren't persisted to the play history, so this can't be
        # reconstructed from ``DataStore`` — it lives on the orchestrator.
        self._executor_skip_rate_provider = executor_skip_rate_provider

    async def snapshot(self, state: OrchestratorState) -> ObservationContext:
        """Fetch history and compute ObservationContext.  <50ms; safe to call every play."""
        try:
            history = await self._store.get_play_history(self._session_id)
        except (OSError, ValueError, RuntimeError) as exc:
            _logger.warning(
                "metrics_query_failed",
                query="play_history",
                exc_type=type(exc).__name__,
                error=str(exc),
            )
            history = []

        try:
            prs_open = await self._store.list_open_pull_requests(self._session_id)
        except (OSError, ValueError, RuntimeError) as exc:
            _logger.warning(
                "metrics_query_failed",
                query="open_pull_requests",
                exc_type=type(exc).__name__,
                error=str(exc),
            )
            prs_open = []

        try:
            prs_approved = await self._store.list_approved_pull_requests(self._session_id)
        except (OSError, ValueError, RuntimeError) as exc:
            _logger.warning(
                "metrics_query_failed",
                query="approved_pull_requests",
                exc_type=type(exc).__name__,
                error=str(exc),
            )
            prs_approved = []

        try:
            learnings = await self._store.list_learnings(self._session_id)
        except (OSError, ValueError, RuntimeError) as exc:
            _logger.warning(
                "metrics_query_failed",
                query="learnings",
                exc_type=type(exc).__name__,
                error=str(exc),
            )
            learnings = []

        try:
            handoffs = await self._store.list_handoffs(self._session_id, limit=_ROLLING_WINDOW)
        except (OSError, ValueError, RuntimeError) as exc:
            _logger.warning(
                "metrics_query_failed",
                query="handoffs",
                exc_type=type(exc).__name__,
                error=str(exc),
            )
            handoffs = []

        try:
            issues = await self._store.list_all_issues(self._session_id)
        except (OSError, ValueError, RuntimeError) as exc:
            _logger.warning(
                "metrics_query_failed",
                query="all_issues",
                exc_type=type(exc).__name__,
                error=str(exc),
            )
            issues = []

        velocity = (
            self._velocity_provider(state.total_plays)
            if self._velocity_provider is not None
            else 0.0
        )
        busy_count = sum(1 for a in state.agents if a.status == AgentStatus.BUSY)

        # Emit epic-level metrics from beads graph (Track 5).
        # These replace the old cluster_* metrics; keyed under epic_closure_ratio_mean,
        # epics_total, and tasks_ready so dashboards and log queries can grep for them.
        if state.graph is not None:
            epic_ratios = [e.closure_ratio for e in state.graph.epics]
            epic_closure_ratio_mean = sum(epic_ratios) / len(epic_ratios) if epic_ratios else 0.0
            _logger.debug(
                "epic_metrics",
                epic_closure_ratio_mean=epic_closure_ratio_mean,
                epics_total=len(state.graph.epics),
                tasks_ready=state.graph.tasks_ready,
                global_closure_ratio=state.graph.global_closure_ratio,
            )

        executor_skip_rate = (
            self._executor_skip_rate_provider()
            if self._executor_skip_rate_provider is not None
            else 0.0
        )
        return _build_context(
            state,
            history,
            prs_open,
            prs_approved,
            learnings,
            handoffs,
            velocity,
            busy_count,
            issues_created_this_session=_count_session_created_issues(issues),
            stagnation_warn_after=self._stagnation_warn_after,
            executor_skip_rate_recent_50=executor_skip_rate,
        )


# ---------------------------------------------------------------------------
# Pure computation (testable without DB)
# ---------------------------------------------------------------------------


def _build_context(
    state: OrchestratorState,
    history: list[PlayRecord],
    prs_open: Sequence[PullRequestRecord],
    prs_approved: Sequence[PullRequestRecord],
    learnings: Sequence[SessionLearningRecord],
    handoffs: Sequence[HandoffRecord] = (),
    rolling_velocity: float = 0.0,
    busy_agent_count: int = 0,
    issues_created_this_session: int | None = None,
    *,
    stagnation_warn_after: int = 5,
    executor_skip_rate_recent_50: float = 0.0,
) -> ObservationContext:
    n = len(history)
    recent = history[-_ROLLING_WINDOW:] if n >= _ROLLING_WINDOW else history

    # Rolling stats
    rolling_success = sum(1 for p in recent if p.success) / len(recent) if recent else 0.0
    rolling_cost = sum(p.dollar_cost for p in recent) / len(recent) if recent else 0.0
    rolling_dur = (
        sum((p.duration_ms or 0) / 1000.0 for p in recent) / len(recent) if recent else 0.0
    )

    # Last _HIST_LEN play types + success flags (oldest → newest)
    last_n = history[-_HIST_LEN:]
    last_types: list[PlayType | None] = [None] * _HIST_LEN
    last_success: list[bool | None] = [None] * _HIST_LEN
    offset = _HIST_LEN - len(last_n)
    for i, p in enumerate(last_n):
        try:
            last_types[offset + i] = PlayType(p.play_type)
        except ValueError:
            last_types[offset + i] = None
        last_success[offset + i] = p.success

    # Issues closed/created this session.
    issues_closed = 0
    issues_created = issues_created_this_session if issues_created_this_session is not None else 0
    for p in history:
        if _play_closed_issue(p):
            issues_closed += 1

    # Issue churn rate over the same rolling window.
    churn_recent = history[-_ROLLING_WINDOW:] if n > _ROLLING_WINDOW else history
    recent_closed = sum(1 for p in churn_recent if _play_closed_issue(p))
    total_issues = len(state.open_issues)
    issue_churn_rate = recent_closed / max(1, total_issues)

    # Time-since metrics (seconds from last matching play)
    last = history[-1] if history else None
    now_epoch = _parse_iso_seconds(last.ended_at or last.started_at) if last else 0.0
    minutes_since_alignment = _minutes_since(history, {"calibrate_alignment"}, now_epoch)
    minutes_since_intake = _minutes_since(history, {"seed_project"}, now_epoch)

    # Stagnation = whole minutes that ALL agents have been idle.
    # Any BUSY agent resets to 0 — work in progress is not stagnation.
    # With no agents the session can't make progress either, so the same
    # wall-clock-since-last-play rule applies.
    any_busy = any(a.status == AgentStatus.BUSY for a in state.agents)
    if any_busy:
        stagnation = 0
    else:
        latest_end = 0.0
        for p in history:
            if p.ended_at:
                ts = _parse_iso_seconds(p.ended_at)
                if ts > latest_end:
                    latest_end = ts
        stagnation = max(0, int((time.time() - latest_end) // 60)) if latest_end > 0.0 else 0

    # Cluster drift: per-epic imbalance (std-dev of closure ratios across epics).
    # High drift means uneven progress — one epic racing ahead while others stall.
    if state.graph is not None and len(state.graph.epics) >= 2:
        ratios = [e.closure_ratio for e in state.graph.epics]
        mean_r = sum(ratios) / len(ratios)
        cluster_drift = (sum((r - mean_r) ** 2 for r in ratios) / len(ratios)) ** 0.5
    else:
        cluster_drift = 0.0

    # PR counts
    prs_open_count = len(prs_open)
    # Awaiting review: open/review PRs without GitHub approval or AgentShore's
    # current-head PASS verdict.
    prs_awaiting = sum(
        1
        for pr in prs_open
        if pr.state in ("open", "review")
        and getattr(pr, "review_decision", None) != "APPROVED"
        and not (
            getattr(pr, "last_review_status", None) == "PASS"
            and getattr(pr, "last_reviewed_sha", None) is not None
            and getattr(pr, "head_sha", None) is not None
            and getattr(pr, "last_reviewed_sha", None) == getattr(pr, "head_sha", None)
        )
    )
    prs_approved_count = len(prs_approved)

    # Handoff rolling stats over the latest handoff rows in this session.
    rolling_context_loss_values = [
        handoff.context_loss_estimate
        for handoff in handoffs
        if handoff.context_loss_estimate is not None
    ]
    rolling_rampup_ms_values = [
        float(handoff.ramp_up_duration_ms)
        for handoff in handoffs
        if handoff.ramp_up_duration_ms is not None
    ]
    rolling_context_loss = (
        sum(rolling_context_loss_values) / len(rolling_context_loss_values)
        if rolling_context_loss_values
        else 0.0
    )
    rolling_rampup_ms = (
        sum(rolling_rampup_ms_values) / len(rolling_rampup_ms_values)
        if rolling_rampup_ms_values
        else 0.0
    )

    # Learnings
    learning_count = len(learnings)
    confidences = [item.confidence for item in learnings]
    learning_avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
    learning_injection_rate = min(1.0, learning_count / 50.0)

    return ObservationContext(
        same_type_failure_streak=state.same_type_failure_streak,
        stagnation_counter=stagnation,
        issues_closed_this_session=issues_closed,
        issues_created_this_session=issues_created,
        last_play_types=tuple(last_types),
        last_play_success=tuple(last_success),
        rolling_success_rate=rolling_success,
        rolling_avg_cost=rolling_cost,
        rolling_avg_duration_s=rolling_dur,
        rolling_avg_context_loss=rolling_context_loss,
        rolling_avg_rampup_ms=rolling_rampup_ms,
        open_pr_count=prs_open_count,
        prs_awaiting_review=prs_awaiting,
        prs_approved_unmerged=prs_approved_count,
        minutes_since_last_alignment_check=minutes_since_alignment,
        minutes_since_last_intake=minutes_since_intake,
        cluster_drift=min(1.0, cluster_drift),
        learning_count=learning_count,
        learning_avg_confidence=learning_avg_conf,
        learning_injection_rate=learning_injection_rate,
        issue_churn_rate=min(1.0, issue_churn_rate),
        rolling_velocity=rolling_velocity,
        busy_agent_count=busy_agent_count,
        stagnation_entropy_multiplier=(
            STAGNATION_ENTROPY_MULTIPLIER if stagnation >= stagnation_warn_after else 1.0
        ),
        agent_specialization=tuple(compute_agent_specialization(history)),
        executor_skip_rate_recent_50=executor_skip_rate_recent_50,
    )


def _parse_iso_seconds(ts: str | None) -> float:
    """Convert an ISO datetime string to seconds since epoch.  Returns 0.0 on error."""
    if not ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except (ValueError, OverflowError) as exc:
        _logger.warning("metrics_query_failed", query="parse_iso_seconds", error=str(exc))
        return 0.0


def _minutes_since(
    history: list[PlayRecord],
    play_types: set[str],
    now_epoch: float,
) -> float:
    """Return minutes since the most recent play of one of the given types."""
    for p in reversed(history):
        if p.play_type in play_types:
            ts = _parse_iso_seconds(p.ended_at or p.started_at)
            if ts > 0.0 and now_epoch > 0.0:
                return max(0.0, (now_epoch - ts) / 60.0)
            return 0.0
    return 480.0  # never ran → report max staleness


def _play_closed_issue(play: PlayRecord) -> bool:
    """Return True when a successful play should count as an issue closure.

    For ``merge_pr`` plays, an explicit ``pr_merged_issue_numbers`` artifact —
    written by ``MergePRPlay.execute()`` from the body-validated issue list —
    is authoritative: a non-empty ``issue_numbers`` list counts as throughput,
    an empty list does not (doc-only PR, hotfix without ``Closes #N``). Plays
    persisted before that artifact existed fall through to the legacy
    "every successful merge counts" behavior so historical sessions keep their
    metrics.
    """
    if not play.success:
        return False

    merge_pr_artifact_present = False
    for artifact in play.artifacts:
        if not isinstance(artifact, dict):
            continue
        artifact_type = artifact.get("type")
        if artifact_type == "issue_closed":
            return True
        if artifact_type == "issues_closed":
            issue_numbers = artifact.get("issue_numbers")
            if isinstance(issue_numbers, list) and len(issue_numbers) > 0:
                return True
        if artifact_type == "pr_merged_issue_numbers":
            merge_pr_artifact_present = True
            issue_numbers = artifact.get("issue_numbers")
            if isinstance(issue_numbers, list) and len(issue_numbers) > 0:
                return True

    return play.play_type == "merge_pr" and not merge_pr_artifact_present
