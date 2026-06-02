"""Module-level row -> dataclass converters used by DataStore mixins."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agentshore.data.models import (
    AgentRecord,
    ArchiveRecord,
    CheckpointRecord,
    DispatchReplayRecord,
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
    SessionLearningRecord,
    SessionRecord,
    TrajectorySnapshotRecord,
    WorkClaimRecord,
)
from agentshore.github.pr_links import canonical_issue_numbers

if TYPE_CHECKING:
    import aiosqlite

    from agentshore.state import JsonArtifact


def _seed_last_reviewed_sha(pr: PullRequestRecord) -> str | None:
    """Pick the value to insert for ``last_reviewed_sha`` on a fresh row.

    GitHub's authoritative ``review_decision`` is the source of truth for
    pre-existing PRs that AgentShore did not author. When such a PR is already
    APPROVED at the moment we first see it, treat the current ``head_sha``
    as already-reviewed so the policy routes it to ``merge_pr`` rather than
    burning tokens on a redundant AgentShore re-review.

    Only takes effect on first insert: the ``ON CONFLICT DO UPDATE`` clause
    in ``record_pull_request`` / ``cache_pull_requests`` preserves any
    existing DB value via ``COALESCE(pull_requests.last_reviewed_sha, …)``.
    AgentShore-authored PRs are unaffected because we set
    ``last_reviewed_sha`` explicitly via ``update_pr_last_reviewed_sha``
    after a successful code_review.
    """
    if pr.last_reviewed_sha is not None:
        return pr.last_reviewed_sha
    if pr.review_decision == "APPROVED" and pr.head_sha is not None:
        return pr.head_sha
    return None


def _row_to_session_record(row: aiosqlite.Row) -> SessionRecord:
    return SessionRecord(
        session_id=row["session_id"],
        project_path=row["project_path"],
        started_at=row["started_at"],
        status=row["status"],
        ended_at=row["ended_at"],
        seed_path=row["seed_path"],
        initial_issue_count=row["initial_issue_count"],
        total_cost=row["total_cost"],
        total_plays=row["total_plays"],
        scope_estimate=row["scope_estimate"],
        scope_remaining=row["scope_remaining"],
        final_alignment=row["final_alignment"],
    )


def _row_to_agent_record(row: aiosqlite.Row) -> AgentRecord:
    # model_tier, display_name, dispatch_count all exist in every supported DB:
    # the baseline schema declares them and the migrations carry pre-existing
    # DBs forward. The single SELECT in ``get_agents`` projects the full column
    # set, so reading them directly (a loud KeyError on drift) is correct.
    return AgentRecord(
        agent_id=row["agent_id"],
        session_id=row["session_id"],
        agent_type=row["agent_type"],
        created_at=row["created_at"],
        terminated_at=row["terminated_at"],
        total_tokens=row["total_tokens"],
        total_cost=row["total_cost"],
        tasks_completed=row["tasks_completed"],
        tasks_failed=row["tasks_failed"],
        model_tier=row["model_tier"],
        display_name=row["display_name"],
        dispatch_count=row["dispatch_count"] if row["dispatch_count"] is not None else 0,
    )


def _row_to_handoff_record(row: aiosqlite.Row) -> HandoffRecord:
    """Convert a DB row into a ``HandoffRecord``."""
    return HandoffRecord(
        session_id=str(row["session_id"]),
        play_id=int(row["play_id"]),
        source_agent_id=str(row["source_agent_id"]),
        target_agent_id=str(row["target_agent_id"]),
        context_tokens_transferred=int(row["context_tokens_transferred"] or 0),
        ramp_up_duration_ms=(
            int(row["ramp_up_duration_ms"]) if row["ramp_up_duration_ms"] is not None else None
        ),
        context_loss_estimate=(
            float(row["context_loss_estimate"])
            if row["context_loss_estimate"] is not None
            else None
        ),
    )


def _row_to_scope_drift(row: aiosqlite.Row) -> ScopeDriftRecord:
    return ScopeDriftRecord(
        session_id=row["session_id"],
        play_id=row["play_id"],
        artifact=row["artifact"],
        reason=row["reason"],
        logged_at=row["logged_at"],
    )


def _row_to_archive_record(row: aiosqlite.Row) -> ArchiveRecord:
    return ArchiveRecord(
        archive_id=row["archive_id"],
        session_id=row["session_id"],
        archive_path=row["archive_path"],
        total_cost=row["total_cost"],
        final_alignment=row["final_alignment"],
        total_plays=row["total_plays"],
        created_at=row["created_at"],
        issues_closed=row["issues_closed"],
        issues_created=row["issues_created"],
    )


def _row_to_review_feedback_pattern(row: aiosqlite.Row) -> ReviewFeedbackPatternRecord:
    return ReviewFeedbackPatternRecord(
        pattern_id=row["pattern_id"],
        session_id=row["session_id"],
        play_id=row["play_id"],
        pattern=row["pattern"],
        category=row["category"],
        frequency=row["frequency"],
        injected=bool(row["injected"]),
        created_at=row["created_at"],
    )


def _row_to_play_record(row: aiosqlite.Row) -> PlayRecord:
    return PlayRecord(
        play_id=row["play_id"],
        session_id=row["session_id"],
        play_type=row["play_type"],
        agent_id=row["agent_id"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        duration_ms=row["duration_ms"],
        success=bool(row["success"]),
        partial=bool(row["partial"]),
        token_cost=row["token_cost"],
        dollar_cost=row["dollar_cost"],
        alignment_before=row["alignment_before"],
        alignment_after=row["alignment_after"],
        alignment_delta=row["alignment_delta"],
        reward=row["reward"],
        failure_category=row["failure_category"],
        error=row["error"],
        artifacts=_decode_artifacts(row["artifacts"]),
    )


def _decode_artifacts(raw_artifacts: str | None) -> list[JsonArtifact]:
    if not raw_artifacts:
        return []
    try:
        raw = json.loads(raw_artifacts)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []

    artifacts: list[JsonArtifact] = []
    for item in raw:
        if isinstance(item, str):
            artifacts.append(item)
            continue
        if isinstance(item, dict):
            artifact: dict[str, object] = {}
            for key, value in item.items():
                if not isinstance(key, str):
                    artifact = {}
                    break
                artifact[key] = value
            if artifact:
                artifacts.append(artifact)
                continue
        artifacts.append(str(item))
    return artifacts


def _load_json_str_list(raw: str | None) -> list[str]:
    """Decode a JSON string-list column defensively.

    Returns ``[]`` for NULL/empty input, malformed JSON, or a non-list value
    — the single failure policy shared by every label/list column (PR labels,
    issue labels). A bare ``json.loads`` here previously raised and blew up the
    whole ``get_open_issues`` / ``list_all_issues`` read on a malformed value.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _row_to_pull_request(row: aiosqlite.Row) -> PullRequestRecord:
    # Every PR read selects the full ``_PULL_REQUEST_COLUMNS`` projection, so
    # all columns are present in every row and are read directly. A missing
    # column raising ``KeyError`` is the desired loud failure — defensive
    # ``"x" in keys`` guards are what previously let ``base_ref`` rot into a
    # silent write-only column.
    labels = _load_json_str_list(row["labels"])
    linked_issue_numbers: tuple[int, ...] = ()
    linked_issue_numbers_raw = row["linked_issue_numbers"]
    if linked_issue_numbers_raw:
        try:
            parsed_links = json.loads(linked_issue_numbers_raw)
            if isinstance(parsed_links, list):
                linked_issue_numbers = canonical_issue_numbers(parsed_links)
        except json.JSONDecodeError:
            linked_issue_numbers = ()
    return PullRequestRecord(
        pr_number=row["pr_number"],
        session_id=row["session_id"],
        issue_number=row["issue_number"],
        linked_issue_numbers=linked_issue_numbers,
        branch=row["branch"],
        state=row["state"],
        title=row["title"],
        url=row["url"],
        github_author=row["github_author"],
        labels=labels,
        review_decision=row["review_decision"],
        status_check_summary=row["status_check_summary"],
        is_draft=bool(row["is_draft"]) if row["is_draft"] is not None else None,
        author_agent_id=row["author_agent_id"],
        author_agent_type=row["author_agent_type"],
        created_at=row["created_at"],
        merged_at=row["merged_at"],
        head_sha=row["head_sha"],
        mergeable=row["mergeable"],
        base_ref=row["base_ref"],
        last_reviewed_sha=row["last_reviewed_sha"],
        last_review_status=row["last_review_status"],
    )


def _row_to_human_feedback(row: aiosqlite.Row) -> HumanFeedbackRecord:
    return HumanFeedbackRecord(
        feedback_id=row["feedback_id"],
        session_id=row["session_id"],
        play_id=row["play_id"],
        trigger=row["trigger"],
        feedback_text=row["feedback_text"],
        action_taken=row["action_taken"],
        created_at=row["created_at"],
    )


def _row_to_learning(row: aiosqlite.Row) -> SessionLearningRecord:
    return SessionLearningRecord(
        learning_id=row["learning_id"],
        session_id=row["session_id"],
        pattern=row["pattern"],
        category=row["category"],
        source_play_id=row["source_play_id"],
        confidence=row["confidence"],
        reinforcement_count=row["reinforcement_count"],
        created_at=row["created_at"],
        last_reinforced_at=row["last_reinforced_at"],
    )


def _row_to_trajectory(row: aiosqlite.Row) -> TrajectorySnapshotRecord:
    return TrajectorySnapshotRecord(
        snapshot_id=row["snapshot_id"],
        session_id=row["session_id"],
        play_id=row["play_id"],
        projected_alignment_at_budget_end=row["projected_alignment_at_budget_end"],
        estimated_remaining_plays=row["estimated_remaining_plays"],
        estimated_remaining_cost=row["estimated_remaining_cost"],
        created_at=row["created_at"],
    )


def _row_to_github_issue(row: aiosqlite.Row) -> GitHubIssueRecord:
    return GitHubIssueRecord(
        issue_number=row["issue_number"],
        session_id=row["session_id"],
        title=row["title"],
        state=row["state"],
        priority=row["priority"],
        labels=_load_json_str_list(row["labels"]),
        source=row["source"],
        url=row["url"],
        created_at=row["created_at"],
        closed_at=row["closed_at"],
    )


def _row_to_review_queue(row: aiosqlite.Row) -> ReviewQueueRecord:
    return ReviewQueueRecord(
        queue_id=row["queue_id"],
        pr_number=row["pr_number"],
        session_id=row["session_id"],
        author_label=row["author_label"],
        enqueued_at=row["enqueued_at"],
        status=row["status"],
        claimed_by=row["claimed_by"],
        claimed_at=row["claimed_at"],
        completed_at=row["completed_at"],
    )


def _row_to_work_claim(row: aiosqlite.Row) -> WorkClaimRecord:
    return WorkClaimRecord(
        claim_id=row["claim_id"],
        claim_group_id=row["claim_group_id"],
        session_id=row["session_id"],
        play_type=row["play_type"],
        resource_key=row["resource_key"],
        status=row["status"],
        agent_id=row["agent_id"],
        play_id=row["play_id"],
        request_mutation_key=row["request_mutation_key"],
        review_queue_id=row["review_queue_id"],
        created_at=row["created_at"],
        claimed_at=row["claimed_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _row_to_external_mutation(row: aiosqlite.Row) -> ExternalMutationRecord:
    return ExternalMutationRecord(
        session_id=row["session_id"],
        idempotency_key=row["idempotency_key"],
        mutation_type=row["mutation_type"],
        target=row["target"],
        status=row["status"],
        created_at=row["created_at"],
        play_id=row["play_id"],
        request_json=row["request_json"],
        response_json=row["response_json"],
    )


def _row_to_checkpoint(row: aiosqlite.Row) -> CheckpointRecord:
    return CheckpointRecord(
        checkpoint_id=row["checkpoint_id"],
        session_id=row["session_id"],
        created_at=row["created_at"],
        play_count=row["play_count"],
        weights_path=row["weights_path"],
        avg_reward=row["avg_reward"],
    )


def _row_to_dispatch_replay(row: aiosqlite.Row) -> DispatchReplayRecord:
    return DispatchReplayRecord(
        session_id=row["session_id"],
        claim_group_id=row["claim_group_id"],
        play_id=row["play_id"],
        skill_name=row["skill_name"],
        params_json=row["params_json"],
        prompt=row["prompt"],
        branch=row["branch"],
        created_at=row["created_at"],
    )


def _row_to_experience(row: aiosqlite.Row) -> ExperienceRecord:
    return ExperienceRecord(
        experience_id=row["experience_id"],
        session_id=row["session_id"],
        play_id=row["play_id"],
        state_vector=bytes(row["state_vector"]),
        action=row["action"],
        reward=row["reward"],
        next_state=bytes(row["next_state"]),
        done=row["done"],
        old_log_prob=row["old_log_prob"],
        value_estimate=row["value_estimate"],
        action_mask=bytes(row["action_mask"]) if row["action_mask"] is not None else None,
        mask_reason=row["mask_reason"],
        policy_version=row["policy_version"],
        action_space_version=row["action_space_version"],
        config_hash=row["config_hash"],
        step_index=row["step_index"],
    )
