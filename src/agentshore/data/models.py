"""Record dataclass DTOs for the data layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from agentshore.github.pr_links import issue_numbers_for_pr

if TYPE_CHECKING:
    from agentshore.state import JsonArtifact

WorktreeStatus = Literal["active", "stale", "reaping", "reaped", "failed"]

_ACTIVE_WORKTREE_STATUSES: frozenset[WorktreeStatus] = frozenset({"active", "reaping"})
_VALID_WORKTREE_STATUSES: frozenset[WorktreeStatus] = frozenset(
    {"active", "stale", "reaping", "reaped", "failed"}
)


@dataclass(slots=True)
class SessionRecord:
    """Row in the ``sessions`` table."""

    session_id: str
    project_path: str
    started_at: str
    status: str = "running"
    ended_at: str | None = None
    seed_path: str | None = None
    initial_issue_count: int | None = None
    total_cost: float = 0.0
    total_plays: int = 0
    scope_estimate: float | None = None
    scope_remaining: float | None = None
    final_alignment: float | None = None


@dataclass(slots=True)
class PlayRecord:
    """Row in the ``plays`` table."""

    session_id: str
    play_type: str
    started_at: str
    success: bool
    play_id: int | None = None
    agent_id: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    partial: bool = False
    token_cost: int = 0
    dollar_cost: float = 0.0
    alignment_before: float | None = None
    alignment_after: float | None = None
    alignment_delta: float | None = None
    reward: float | None = None
    failure_category: str | None = None
    error: str | None = None
    artifacts: list[JsonArtifact] = field(default_factory=list)


@dataclass(slots=True)
class AgentRecord:
    """Row in the ``agents`` table."""

    agent_id: str
    session_id: str
    agent_type: str
    created_at: str
    terminated_at: str | None = None
    total_tokens: int = 0
    total_cost: float = 0.0
    tasks_completed: int = 0
    tasks_failed: int = 0
    # desktop-j8b: persisted at agent_instantiated so the ESR play log can
    # render a human-readable agent name (model_tier + display_name) instead
    # of the raw UUID. Nullable for back-compat with old DB rows.
    model_tier: str | None = None
    display_name: str | None = None
    # desktop-31h2: cumulative count of plays dispatched to this agent in the
    # current session, incremented at dispatch-claim time (regardless of
    # success/failure/timeout). Surfaced as `dispatch_share` in agent
    # performance rollups so fleet-utilisation imbalance is visible.
    dispatch_count: int = 0


@dataclass(slots=True)
class HandoffRecord:
    """Row in the ``agent_handoffs`` table."""

    session_id: str
    play_id: int
    source_agent_id: str
    target_agent_id: str
    context_tokens_transferred: int = 0
    ramp_up_duration_ms: int | None = None
    context_loss_estimate: float | None = None


@dataclass(slots=True)
class PullRequestRecord:
    """Row in the ``pull_requests`` table."""

    pr_number: int
    session_id: str
    state: str
    created_at: str
    issue_number: int | None = None
    linked_issue_numbers: tuple[int, ...] = ()
    branch: str | None = None
    title: str = ""
    url: str | None = None
    github_author: str | None = None
    labels: list[str] = field(default_factory=list)
    review_decision: str | None = None
    status_check_summary: str | None = None
    is_draft: bool | None = None
    author_agent_id: str | None = None
    # Agent backend that authored the PR ("claude_code", "codex", "api_*").
    # Stored separately from author_agent_id so anti-confirmation in
    # code_review can reject reviewers of the *same backend type* even when
    # the original author has been terminated and replaced.
    author_agent_type: str | None = None
    merged_at: str | None = None
    head_sha: str | None = None
    mergeable: str | None = None
    base_ref: str | None = None
    last_reviewed_sha: str | None = None
    # AgentShore's verdict at last_reviewed_sha: "PASS" | "BLOCK" | None.
    # Lapses automatically when head_sha advances past last_reviewed_sha.
    last_review_status: str | None = None

    def __post_init__(self) -> None:
        links = issue_numbers_for_pr(self)
        self.linked_issue_numbers = links
        if self.issue_number is None and links:
            self.issue_number = links[0]


@dataclass(slots=True)
class ExternalMutationRecord:
    """Row in the ``external_mutations`` table."""

    session_id: str
    idempotency_key: str
    mutation_type: str
    target: str
    status: str
    created_at: str
    play_id: int | None = None
    request_json: str | None = None
    response_json: str | None = None


@dataclass(slots=True)
class WorkClaimRecord:
    """Row in the ``work_claims`` table."""

    claim_group_id: str
    session_id: str
    play_type: str
    resource_key: str
    status: str
    created_at: str
    claim_id: int | None = None
    agent_id: str | None = None
    play_id: int | None = None
    request_mutation_key: str | None = None
    review_queue_id: int | None = None
    claimed_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(slots=True)
class DispatchReplayRecord:
    """Row in the ``dispatch_replay`` table — a deterministic retry payload."""

    session_id: str
    claim_group_id: str
    play_id: int
    skill_name: str
    params_json: str
    prompt: str
    created_at: str
    branch: str | None = None


@dataclass(slots=True)
class ScopeDriftRecord:
    """Row in the ``scope_drift_log`` table."""

    session_id: str
    artifact: str
    logged_at: str
    play_id: int | None = None
    reason: str | None = None


@dataclass(slots=True)
class HumanFeedbackRecord:
    """Row in the ``human_feedback`` table."""

    session_id: str
    play_id: int
    trigger: str
    action_taken: str
    created_at: str
    feedback_id: int | None = None
    feedback_text: str | None = None


@dataclass(slots=True)
class TrajectorySnapshotRecord:
    """Row in the ``trajectory_snapshots`` table."""

    session_id: str
    play_id: int
    projected_alignment_at_budget_end: float
    estimated_remaining_plays: int
    estimated_remaining_cost: float
    created_at: str
    snapshot_id: int | None = None


@dataclass(slots=True)
class ReviewFeedbackPatternRecord:
    """Row in the ``review_feedback_patterns`` table."""

    session_id: str
    play_id: int
    pattern: str
    category: str
    created_at: str
    pattern_id: int | None = None
    frequency: int = 1
    injected: bool = False


@dataclass(slots=True)
class ArchiveRecord:
    """Row in the ``session_archives`` table."""

    archive_id: str
    session_id: str
    archive_path: str
    total_cost: float
    final_alignment: float
    total_plays: int
    created_at: str
    issues_closed: int = 0
    issues_created: int = 0


@dataclass(slots=True)
class GitHubIssueRecord:
    """Row in the ``github_issues`` table."""

    issue_number: int
    session_id: str
    title: str
    state: str
    created_at: str
    priority: int | None = None
    labels: list[str] = field(default_factory=list)
    source: str | None = None
    url: str | None = None
    closed_at: str | None = None
    github_author: str | None = None


@dataclass(slots=True)
class ExperienceRecord:
    """Row in the ``rl_experience`` table — one PPO experience step."""

    session_id: str
    play_id: int
    state_vector: bytes
    action: int
    reward: float
    next_state: bytes
    done: int
    action_space_version: int = 1
    experience_id: int | None = None
    old_log_prob: float | None = None
    value_estimate: float | None = None
    action_mask: bytes | None = None
    mask_reason: str | None = None
    policy_version: str | None = None
    config_hash: str | None = None
    step_index: int | None = None


@dataclass(slots=True)
class CheckpointRecord:
    """Row in the ``policy_checkpoints`` table."""

    session_id: str
    created_at: str
    play_count: int
    weights_path: str
    checkpoint_id: int | None = None
    avg_reward: float | None = None


@dataclass(slots=True)
class ReviewQueueRecord:
    """Row in the ``review_queue`` table."""

    pr_number: int
    session_id: str
    enqueued_at: str
    queue_id: int | None = None
    author_label: str | None = None
    status: str = "pending"
    claimed_by: str | None = None
    claimed_at: str | None = None
    completed_at: str | None = None


@dataclass(frozen=True, slots=True)
class WorktreeRow:
    """One row from the ``worktrees`` table."""

    worktree_id: int
    session_id: str
    branch_name: str | None
    pre_branch_key: str | None
    worktree_path: str
    status: WorktreeStatus
    original_play_type: str
    head_sha: str | None
    base_ref: str
    created_at: str
    last_used_at: str
    reaped_at: str | None
    failure_reason: str | None
