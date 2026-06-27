"""DataStore package — split into domain-table mixins.

External callers continue to import ``DataStore`` and the row converters
from ``agentshore.data.store``; this package preserves those names exactly
as the prior single-module layout.
"""

from __future__ import annotations

from agentshore.data.models import (
    AgentRecord,
    ArchiveRecord,
    CheckpointRecord,
    ExperienceRecord,
    ExternalMutationRecord,
    GitHubIssueRecord,
    HandoffRecord,
    HumanFeedbackRecord,
    PlayRecord,
    PullRequestRecord,
    ReviewFeedbackPatternRecord,
    ReviewQueueRecord,
    ScopeDriftRecord,
    SessionRecord,
    TrajectorySnapshotRecord,
    WorkClaimRecord,
)
from agentshore.data.store.core import DataStore
from agentshore.data.store.rows import (
    _decode_artifacts,
    _row_to_agent_record,
    _row_to_archive_record,
    _row_to_experience,
    _row_to_github_issue,
    _row_to_handoff_record,
    _row_to_human_feedback,
    _row_to_play_record,
    _row_to_pull_request,
    _row_to_review_feedback_pattern,
    _row_to_review_queue,
    _row_to_scope_drift,
    _row_to_session_record,
    _row_to_trajectory,
    _row_to_work_claim,
    _seed_last_reviewed_sha,
)

__all__ = [
    "AgentRecord",
    "ArchiveRecord",
    "CheckpointRecord",
    "DataStore",
    "ExperienceRecord",
    "ExternalMutationRecord",
    "GitHubIssueRecord",
    "HandoffRecord",
    "HumanFeedbackRecord",
    "PlayRecord",
    "PullRequestRecord",
    "ReviewFeedbackPatternRecord",
    "ReviewQueueRecord",
    "ScopeDriftRecord",
    "SessionRecord",
    "TrajectorySnapshotRecord",
    "WorkClaimRecord",
    # Internal helpers + row converters retained for in-tree consumers.
    "_decode_artifacts",
    "_row_to_agent_record",
    "_row_to_archive_record",
    "_row_to_experience",
    "_row_to_github_issue",
    "_row_to_handoff_record",
    "_row_to_human_feedback",
    "_row_to_play_record",
    "_row_to_pull_request",
    "_row_to_review_feedback_pattern",
    "_row_to_review_queue",
    "_row_to_scope_drift",
    "_row_to_session_record",
    "_row_to_trajectory",
    "_row_to_work_claim",
    "_seed_last_reviewed_sha",
]
