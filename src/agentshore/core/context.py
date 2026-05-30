"""Dispatch context and state-data dataclasses used by the orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentshore.beads import ProjectGraph
    from agentshore.data.store import (
        GitHubIssueRecord,
        PlayRecord,
        PullRequestRecord,
        ReviewQueueRecord,
        TrajectorySnapshotRecord,
    )
    from agentshore.plays.base import PlayParams
    from agentshore.plays.override import OverrideKind
    from agentshore.state import OrchestratorState, PlayType


@dataclass(slots=True)
class _DispatchContext:
    dispatch_id: str
    play_type: PlayType
    params: PlayParams
    state_at_dispatch: OrchestratorState
    pending_step: object | None  # _PendingStep from PPOSelector
    dispatched_at: float
    # OverrideKind for plays dispatched from the override queue (bootstrap
    # recipe, user-request, retry, etc); None means PPO drove the selection.
    # Used by _process_completion to register the play_id in
    # _override_dispatched_play_ids so the loop detector can ignore it.
    override_kind: OverrideKind | None = None


@dataclass(slots=True)
class _StateData:
    """Snapshot of all DB-backed inputs needed to construct ``OrchestratorState``.

    ``_build_state`` is intentionally a near-pure transformation
    ``_StateData -> OrchestratorState``; all I/O happens up-front in
    ``_fetch_state_data`` so the state-construction logic is unit-testable
    without a live database.
    """

    issue_records: list[GitHubIssueRecord]
    pr_records: list[PullRequestRecord]
    pending_reviews: list[ReviewQueueRecord]
    play_history: list[PlayRecord]
    trajectory_record: TrajectorySnapshotRecord | None
    graph: ProjectGraph | None = None
    # V1 contract fields populated alongside the other DB reads.
    policy_checkpoint_id: str | None = None
    learnings_count: int = 0
    human_feedback_count: int = 0
