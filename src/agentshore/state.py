"""StateProvider protocol and OrchestratorState dataclass shared by TUI and IPC."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from agentshore.config.models import PolicyMode, RunMode
from agentshore.errors import ErrorClass
from agentshore.github.pr_links import issue_numbers_for_pr

if TYPE_CHECKING:
    from agentshore.beads import ProjectGraph
    from agentshore.errors import FailureKind
    from agentshore.plays.base import PlayParams
    from agentshore.rl.mask_reason import MaskReason

JsonObject = dict[str, object]
JsonArtifact = str | JsonObject
JsonIssueRef = int | JsonObject
SkipCategory = Literal[
    "no_target",
    "staffing",
    "masked",
    "invalid_config",
]

# desktop-85ex: structured reason taxonomy for the ``play_skipped`` loop
# event. Distinct from ``SkipCategory`` which classifies executor-time
# skips on dispatched plays; ``PlaySkipReason`` classifies *why* the
# selector tick did not produce a dispatch in the first place. Pinned to
# this finite set so log post-processing (agentshore.log → metrics) can
# diagnose fleet idle storms without grep-and-pray.
#
#   all_masked              — at least one play type was eligible but
#                             every action_mask slot is False; payload
#                             includes top mask_reasons.
#   no_eligible_targets     — mask permits a play type, but no concrete
#                             target (issue / PR / agent) resolves.
#   cooldown_active         — masked specifically due to per-play
#                             cooldown / recency caps.
#   value_dominated_by_idle — DEPRECATED post-rni0 (idle was a play; now
#                             it's a loop wait, never a selector pick).
#                             Reserved so log consumers see a stable
#                             enum surface during the rollout window.
#   engine_paused           — session_state ∈ {paused, draining,
#                             shutting_down}.
#   selector_returned_none  — PPO ran but produced no pickable action;
#                             the catch-all when no narrower reason
#                             fits. This is the post-rni0 "wait" path.
PlaySkipReason = Literal[
    "all_masked",
    "no_eligible_targets",
    "cooldown_active",
    "value_dominated_by_idle",
    "engine_paused",
    "selector_returned_none",
]

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PlayType(enum.Enum):
    """Canonical 22-slot action space aligned with ``V1_ACTION_ORDER``.

    ``FUTURE_4``, ``FUTURE_7``, and ``FUTURE_8`` are reserved and structurally
    masked.
    """

    INSTANTIATE_AGENT = "instantiate_agent"
    UNBLOCK_PR = "unblock_pr"
    WRITE_IMPLEMENTATION_PLAN = "write_implementation_plan"
    END_AGENT = "end_agent"
    ISSUE_PICKUP = "issue_pickup"
    CODE_REVIEW = "code_review"
    MERGE_PR = "merge_pr"
    RUN_QA = "run_qa"
    SYSTEMATIC_DEBUGGING = "systematic_debugging"
    DESIGN_AUDIT = "design_audit"
    END_SESSION = "end_session"
    RECONCILE_STATE = "reconcile_state"
    REFINE_TASK_BREAKDOWN = "refine_task_breakdown"
    CLEANUP = "cleanup"
    FUTURE_4 = "future_4"
    TAKE_BREAK = "take_break"
    GROOM_BACKLOG = "groom_backlog"
    SEED_PROJECT = "seed_project"
    CALIBRATE_ALIGNMENT = "calibrate_alignment"
    PRUNE = "prune"
    FUTURE_7 = "future_7"
    FUTURE_8 = "future_8"


# Plays that are internal orchestrator activity, not user-visible work.
# Currently empty: idle waits and post-error recovery are loop-side now,
# not policy decisions, so no PlayType is dispatched as bookkeeping.
INTERNAL_PLAY_TYPES: frozenset[PlayType] = frozenset()


class AgentType(enum.Enum):
    """Supported coding agent backends."""

    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    GEMINI = "gemini"
    GROK = "grok"


# Single canonical definition of which AgentType values are CLI (subprocess)
# agents — used by the health monitor to check liveness and by the agent
# manager to gate identity resolution. Define it once here, adjacent to the
# enum, so adding a new CLI agent type requires only one edit.
CLI_AGENT_TYPES: frozenset[AgentType] = frozenset(
    {AgentType.CLAUDE_CODE, AgentType.CODEX, AgentType.GEMINI, AgentType.GROK}
)


class AgentStatus(enum.Enum):
    """Runtime status of a managed agent."""

    IDLE = "idle"
    BUSY = "busy"
    ERROR = "error"
    TERMINATED = "terminated"


# Error classes (from ``cli_agent._classify_error``) that a TAKE_BREAK recovery
# can plausibly clear, so an ERROR agent in one of these gets a recovery-first
# path (TAKE_BREAK → recovery-exhausted → END_AGENT). An ERROR agent in ANY
# other class (auth, invalid_model, crash_oom, crash_signal, timeout,
# codex_rollout, or None) is terminal: no recovery path exists, so END_AGENT is
# unmasked for it immediately rather than leaving it leaked until end_session
# (#20). Kept here so the eligibility mask and the END_AGENT resolver agree.
# "transient_network" (socket close / connection reset, #23) is recoverable —
# it is a precise carve-out of the old "unknown" bucket, which was recoverable.
RECOVERABLE_ERROR_CLASSES: frozenset[ErrorClass] = frozenset(
    {ErrorClass.RATE_LIMIT, ErrorClass.UNKNOWN, ErrorClass.TRANSIENT_NETWORK}
)

# Per-agent circuit breaker (#22): an agent that has produced ZERO successful
# plays this session and has either hit a dispatch timeout or accumulated
# repeated failures is treated as non-functional and masked/deprioritized from
# work selection until it succeeds. Guards against routing critical plays
# (e.g. code_review) to a known-dead agent (the gemini-ETIMEDOUT case, where a
# single failed dispatch burned a full ~30-min idle timeout). The mask lifts
# automatically the moment the agent completes any play (``tasks_completed > 0``).
CIRCUIT_BREAKER_FAILURE_LIMIT = 2


def is_agent_circuit_broken(
    *,
    tasks_completed: int,
    tasks_failed: int,
    timeout_count: int,
) -> bool:
    """Return True when an agent should be benched as non-functional (#22)."""
    if tasks_completed > 0:
        return False
    return timeout_count >= 1 or tasks_failed >= CIRCUIT_BREAKER_FAILURE_LIMIT


class SessionState(enum.Enum):
    """Lifecycle state of an AgentShore session."""

    INITIALIZING = "initializing"
    RUNNING = "running"
    PAUSED = "paused"
    DRAINING = "draining"
    SHUTTING_DOWN = "shutting_down"


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlayOutcome:
    """Result of executing a single play."""

    play_type: PlayType
    agent_id: str | None
    success: bool
    partial: bool
    duration_seconds: float
    token_cost: int
    dollar_cost: float
    artifacts: list[JsonArtifact]
    alignment_delta: float | None
    error: str | None = None
    play_id: int | None = None
    inflation_raised: bool = False
    skipped: bool = False
    skip_category: SkipCategory | None = None
    retry_requested: bool = False
    # Typed cause set at the failure site when the play knows it. The persisted
    # ``failure_category`` string is derived from this (see executor
    # ``_infer_failure_category``); the substring inferer is the fallback when a
    # play leaves this None.
    failure_kind: FailureKind | None = None

    @classmethod
    def failed(
        cls,
        play_type: PlayType,
        error: str,
        agent_id: str | None = None,
        dollar_cost: float = 0.0,
        partial: bool = False,
        retry_requested: bool = False,
        failure_kind: FailureKind | None = None,
    ) -> PlayOutcome:
        """Convenience constructor for a zero-cost failure outcome."""
        return cls(
            play_type=play_type,
            agent_id=agent_id,
            success=False,
            partial=partial,
            duration_seconds=0.0,
            token_cost=0,
            dollar_cost=dollar_cost,
            artifacts=[],
            alignment_delta=0.0,
            error=error,
            retry_requested=retry_requested,
            failure_kind=failure_kind,
        )

    @classmethod
    def skipped_outcome(
        cls,
        play_type: PlayType,
        category: SkipCategory,
        *,
        error: str | None = None,
        agent_id: str | None = None,
    ) -> PlayOutcome:
        """Convenience constructor for a zero-cost non-dispatch outcome."""
        return cls(
            play_type=play_type,
            agent_id=agent_id,
            success=True,
            partial=True,
            duration_seconds=0.0,
            token_cost=0,
            dollar_cost=0.0,
            artifacts=[],
            alignment_delta=0.0,
            error=error,
            skipped=True,
            skip_category=category,
        )


@dataclass(frozen=True, slots=True)
class SkillResult:
    """Parsed JSON result block extracted from coding-agent output.

    Every skill-backed play ends with a JSON block that the agent emits.
    ``result_parser.parse_skill_result`` turns the raw agent text into one of
    these.
    """

    success: bool
    artifacts: list[JsonArtifact] = field(default_factory=list)
    issues_created: list[JsonIssueRef] = field(default_factory=list)
    requested_mutations: list[JsonObject] = field(default_factory=list)
    error: str | None = None
    spec_compliance: str | None = None
    blocking_findings: int | None = None
    # Prior-comment verdict surfaced by the agentshore-code-review skill on its
    # dedup short-circuit. Lets the play backfill last_review_status from an
    # existing AGENTSHORE_CODE_REVIEW comment when the current dispatch returns
    # SKIP — without it, PRs whose first review crashed or filed as COMMENTED
    # sit at NULL forever and never become merge_pr-eligible.
    prior_verdict: str | None = None
    prior_blocking_findings: int | None = None
    # Issues that the skill marked closed during this play (top-level
    # ``issues_closed`` in the result block). agentshore-merge-pr emits this
    # list for issues referenced by ``Closes #N``/``Fixes #N``/``Resolves #N``
    # in the merged PR; the merge_pr play uses it to write through closed-state
    # to the SQLite cache so the dashboard's DONE column populates without
    # waiting for the next periodic GitHub refresh (which only re-fetches
    # state="open" and therefore can't update closed issues).
    issues_closed: list[int] = field(default_factory=list)
    # Issue-pickup publish reconciliation signals. Agents may complete local
    # work/tests and then fail while creating the PR; the executor uses these
    # fields to find or create the missing PR before scoring the play.
    issue_picked_up: int | None = None
    branch: str | None = None
    tests_passed: bool | None = None
    verification_evidence: list[JsonObject] = field(default_factory=list)
    review_patterns: list[JsonObject] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AgentSnapshot:
    """Point-in-time view of a managed agent for UI/state updates."""

    agent_id: str
    agent_type: AgentType
    status: AgentStatus
    context_size: int
    total_cost: float
    total_tokens: int
    # Plays this agent has completed (success / failure) in the current session,
    # derived from play_history at state-build time. Used by the end_agent
    # precondition to gate termination on a minimum lifetime, and by the
    # _resolve_end_agent scoring to prefer worst-performing idle agents.
    tasks_completed: int
    tasks_failed: int
    display_name: str = ""
    model: str | None = None
    model_tier: str | None = None
    reasoning_effort: str | None = None
    current_play_type: PlayType | None = None
    current_play_id: int | None = None
    current_play_started_at: str | None = None
    current_play_issue_number: int | None = None
    current_play_pr_number: int | None = None
    current_play_branch: str | None = None
    last_error_class: ErrorClass | None = None
    timeout_count: int = 0
    github_identity: str | None = None
    # desktop-31h2: cumulative dispatch count and the agent's share of the
    # fleet-wide total. Built from the agents.dispatch_count column at
    # snapshot time so dashboards can flag fleet-utilisation imbalance
    # (e.g. one agent at 60% dispatch_share while another sits at 0%
    # despite work being available).
    dispatch_count: int = 0
    dispatch_share: float = 0.0


@dataclass(frozen=True, slots=True)
class IssueSnapshot:
    """Lightweight view of a cached GitHub issue."""

    issue_number: int
    title: str
    state: str
    priority: int | None
    labels: list[str]
    source: str | None
    url: str | None = None
    created_at: str | None = None
    closed_at: str | None = None
    github_author: str | None = None
    bead_id: str | None = None
    bead_epic_id: str | None = None
    bead_epic_title: str | None = None
    bead_status: str | None = None
    bead_ready: bool = False
    bead_mirror_status: str = "missing"


@dataclass(frozen=True, slots=True)
class PullRequestSnapshot:
    """Lightweight view of a cached GitHub pull request for UI/IPC consumers."""

    pr_number: int
    title: str
    state: str
    branch: str | None
    issue_number: int | None
    labels: list[str]
    review_decision: str | None
    status_check_summary: str | None
    is_draft: bool
    blocked: bool
    blocked_reasons: list[str]
    linked_issue_numbers: tuple[int, ...] = ()
    url: str | None = None
    github_author: str | None = None
    author_agent_id: str | None = None
    author_agent_type: str | None = None
    head_sha: str | None = None
    mergeable: str | None = None
    base_ref: str | None = None
    last_reviewed_sha: str | None = None
    last_review_status: str | None = None

    def __post_init__(self) -> None:
        links = issue_numbers_for_pr(self)
        object.__setattr__(self, "linked_issue_numbers", links)
        if self.issue_number is None and links:
            object.__setattr__(self, "issue_number", links[0])


@dataclass(frozen=True, slots=True)
class PendingReviewSnapshot:
    """Lightweight view of a queued code-review request for mask/resolver consumers."""

    queue_id: int
    pr_number: int
    author_label: str | None
    enqueued_at: str


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """Current budget state for display and decision-making.

    Two independent soft-cap dimensions: dollars (``total_budget``/``spent``/
    ``remaining``) and wall-clock time (``time_*`` fields, in minutes). The time
    fields are ``None`` when ``time_enabled`` is False (no wall-clock cap).
    """

    total_budget: float
    spent: float
    remaining: float
    estimated_cost_per_play: float
    enabled: bool = True
    time_enabled: bool = False
    time_total_minutes: float | None = None
    time_elapsed_minutes: float | None = None
    time_remaining_minutes: float | None = None

    def dollar_reserve_reached(self) -> bool:
        """Return ``True`` when dollar spend is inside the reserve window."""
        from agentshore.budget import budget_reserve_reached

        return self.enabled and budget_reserve_reached(
            spent=self.spent, total_budget=self.total_budget
        )

    def time_reserve_reached(self) -> bool:
        """Return ``True`` when elapsed wall-clock time is inside the reserve window."""
        from agentshore.budget import time_budget_reserve_reached

        return (
            self.time_enabled
            and self.time_total_minutes is not None
            and self.time_elapsed_minutes is not None
            and time_budget_reserve_reached(
                elapsed_minutes=self.time_elapsed_minutes,
                total_minutes=self.time_total_minutes,
            )
        )

    def reserve_reason(self) -> str | None:
        """Return the drain-reason string if either reserve is reached, else ``None``."""
        if self.dollar_reserve_reached():
            return "budget_reserve_reached"
        if self.time_reserve_reached():
            return "time_budget_reserve_reached"
        return None


@dataclass(frozen=True, slots=True)
class TrajectorySnapshot:
    """Projected session trajectory at a point in time."""

    projected_alignment_at_budget_end: float
    estimated_remaining_plays: int
    estimated_remaining_cost: float


@dataclass(frozen=True, slots=True)
class ActivePlay:
    """Snapshot of the play currently executing in the session.

    Mirrors the IPC ``active_play`` schema (see ``docs/design/ipc/DESIGN.md``).
    Replaces the previous loosely-typed ``dict[str, object]`` so IPC consumers
    can rely on a stable shape with ``play_type``, ``agent_id``, ``started_at``,
    ``play_id``, and optional ``issue_number``/``pr_number``/``branch``/``phase``.
    """

    play_type: PlayType
    agent_id: str | None
    started_at: str
    play_id: int | None = None
    issue_number: int | None = None
    pr_number: int | None = None
    branch: str | None = None
    phase: str | None = None
    trigger_agent_id: str | None = None
    trigger_agent_type: str | None = None
    trigger_error_class: str | None = None


@dataclass(frozen=True, slots=True)
class PlayTypeStatsSnapshot:
    """Aggregated play performance for one play type in the current session."""

    play_type: PlayType | str
    total: int
    successful: int
    failed: int
    success_rate: float
    total_cost: float
    avg_duration_seconds: float


@dataclass(frozen=True, slots=True)
class AgentPlaySpecializationSnapshot:
    """Per-agent / per-play-type effectiveness cell.

    Derived on demand from session play history — no separate table. Carries
    both all-session counts and a rolling success rate over the last
    ``rolling_window`` matching plays so small samples don't dominate the view.
    """

    agent_id: str
    play_type: PlayType | str
    total: int
    successful: int
    failed: int
    success_rate: float
    rolling_success_rate: float


@dataclass(frozen=True, slots=True)
class SessionStatsSnapshot:
    """Aggregated session performance stats for dashboard consumers."""

    total_plays: int
    successful_plays: int
    failed_plays: int
    success_rate: float
    total_cost: float
    avg_cost_per_play: float
    total_tokens: int
    avg_duration_seconds: float
    by_play_type: list[PlayTypeStatsSnapshot] = field(default_factory=list)
    agent_specialization: list[AgentPlaySpecializationSnapshot] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class WorkQueueItem:
    """An issue together with the pull request (if any) representing its work."""

    issue: IssueSnapshot
    pr: PullRequestSnapshot | None = None


@dataclass(frozen=True, slots=True)
class WorkQueueView:
    """Lifecycle-grouped view of the issue/PR backlog.

    Single source of truth for "what phase is each issue/PR in", computed once
    per snapshot by :meth:`OrchestratorState.work_queue`. UI consumers (the
    issue work-queue screen and the dashboard summary widget) are pure
    formatters over this view rather than reimplementing the grouping.

    ``orphan_review_prs`` holds open PRs with no matching open issue; each entry
    pairs the PR with whether it is currently queued for review. They are
    rendered alongside ``in_review`` issues by the work-queue screen.
    """

    todo: list[WorkQueueItem] = field(default_factory=list)
    in_progress: list[WorkQueueItem] = field(default_factory=list)
    in_review: list[WorkQueueItem] = field(default_factory=list)
    done: list[WorkQueueItem] = field(default_factory=list)
    orphan_review_prs: list[tuple[PullRequestSnapshot, bool]] = field(default_factory=list)
    next_issue: IssueSnapshot | None = None


def loop_level_for_streak(streak: int) -> int:
    """Map failure streak to escalation level: 0 (none), 1 (warn), 2 (force), 3 (escalation).

    Single source of truth for the loop-escalation ladder. Lives here (core,
    UI-free) so ``StateBuilder`` can precompute ``OrchestratorState.loop_level``
    without a core->ui import inversion; the TUI widgets import it from here too.
    """
    if streak >= 7:
        return 3
    if streak >= 5:
        return 2
    if streak >= 3:
        return 1
    return 0


@dataclass(slots=True)
class OrchestratorState:
    """Complete snapshot of the AgentShore session pushed to UI/IPC consumers."""

    session_id: str
    session_state: SessionState
    total_plays: int
    total_cost: float
    policy_mode: PolicyMode = PolicyMode.LEARNING
    # Configured merge/PR target branch (``cfg.project.target_branch``). Carried
    # on state so the candidate analyzer, mask, and merge_pr preconditions can
    # deterministically refuse to merge a PR whose base != target — independent
    # of whether the authoring/merging agent honored the skill's base step.
    target_branch: str | None = None
    agents: list[AgentSnapshot] = field(default_factory=list)
    open_issues: list[IssueSnapshot] = field(default_factory=list)
    pull_requests: list[PullRequestSnapshot] = field(default_factory=list)
    # Count of open PRs dropped from ``pull_requests`` this tick because their
    # base branch != the configured ``target_branch`` (Piece C target-branch
    # filter). The PRs themselves are removed from the shared collection so they
    # never reach the dashboard, candidate pool, or backpressure; this scalar is
    # surfaced via WorkAvailability so the dashboard can render an "(N hidden)"
    # badge. Zero when no filter is active or nothing was filtered.
    ignored_pr_count: int = 0
    pending_review_queue: list[PendingReviewSnapshot] = field(default_factory=list)
    budget: BudgetSnapshot | None = None
    trajectory: TrajectorySnapshot | None = None
    # Live beads project graph loaded each tick. None when beads is not
    # initialised for the project (no .beads/ directory).
    graph: ProjectGraph | None = None
    stats: SessionStatsSnapshot | None = None
    # Typed snapshot of the play currently executing. ``None`` between plays.
    active_play: ActivePlay | None = None
    same_type_failure_streak: int = 0
    last_play_type: PlayType | None = None
    # Precomputed loop-escalation level for ``same_type_failure_streak`` via
    # :func:`loop_level_for_streak` (0 none, 1 warn, 2 force, 3 escalation).
    # Computed once in StateBuilder so UI/IPC consumers read it directly instead
    # of each re-applying the ladder.
    loop_level: int = 0
    # Snapshot of the main-repo dispatch-pause latch
    # (``MainRepoGuard.dispatch_paused``). When True the mask hides every play
    # except END_AGENT and RECONCILE_STATE from PPO; ``dispatch_play`` gate 1
    # keeps the live recheck as a backstop since state can flip between
    # selection and dispatch.
    main_repo_dispatch_paused: bool = False
    # Snapshot of whether END_SESSION is already started or in-flight. When True
    # the mask hides END_SESSION from PPO; ``dispatch_play`` gate 2 keeps the
    # live recheck as a backstop.
    end_session_in_flight: bool = False
    # Agent IDs whose break-recovery counter has reached
    # ``BREAK_RECOVERY_FAILURE_LIMIT``. END_AGENT is unmasked for these agents
    # even when the normal min-plays / two-agent gate would block it, so the
    # PPO can choose to retire a wedged agent. Derived each tick from the
    # in-memory recovery counter intersected with currently-live agents.
    recovery_exhausted_agent_ids: frozenset[str] = field(default_factory=frozenset)
    # True when the most recent completed play returned ``skipped_outcome("masked")``
    # from the executor — i.e. preconditions failed at dispatch time due to state
    # divergence between selection and execution. Surfaced as a diagnostic so
    # operators can see the divergence in IPC state.
    recent_executor_skip: bool = False
    in_flight_plays: list[PlayType] = field(default_factory=list)
    in_flight_issues: list[int] = field(default_factory=list)
    # Issues that have had a WRITE_IMPLEMENTATION_PLAN dispatched this session.
    # Persists after completion to prevent re-planning before the GH label refresh.
    planned_issues: frozenset[int] = field(default_factory=frozenset)
    # Opt-in issue-author trust gating (trusted_ids.restrict_issues_to_trusted_authors).
    # Resolved once per tick at state assembly from config — so the state-only
    # candidate analyzer can exclude issues opened by non-trusted authors without
    # threading config through every call site. When the toggle is off the flag is
    # False and the author set is empty (no gating).
    restrict_issues_to_trusted_authors: bool = False
    trusted_issue_authors: frozenset[str] = field(default_factory=frozenset)
    # Resource keys (``pr:<n>`` / ``issue:<n>``) parked for the session because a
    # worktree allocation against them failed repeatedly (Piece A backstop). The
    # candidate analyzer treats these like in-flight resources and excludes every
    # play that touches them, so a structurally-unallocatable PR can't be
    # re-selected every tick (the unblock_pr hot-loop, issue #60). Snapshotted
    # each tick from the orchestrator's in-memory park set; empty by default.
    parked_resource_keys: frozenset[str] = field(default_factory=frozenset)
    # Consecutive plays of the same type regardless of success/failure. Catches
    # PPO collapses onto a cheap repeated play (where every play succeeds and
    # `same_type_failure_streak` stays at 0). Used for masking + reward penalty
    # at a higher threshold than failure streaks, since a few same-type plays
    # in a row is often legitimate (e.g., reviewing 3 PRs).
    same_type_streak: int = 0
    # Plays since the most recent successful INSTANTIATE_AGENT (None if no
    # instantiate has happened yet). Used by InstantiateAgentPlay's cooldown
    # precondition to keep the policy from spawning fleets every step.
    plays_since_last_instantiate: int | None = None
    # Per-play-type "plays since last execution" counter, populated for any
    # play that has appeared in play_history. House-keeping and cooldown-gated
    # plays use this so PPO can't collapse onto repeated low-cost actions.
    # Keys are play_type enum values.
    plays_since_last_play_type: dict[PlayType, int] = field(default_factory=dict)
    # Latest completed success/failure value per play type. Work-availability
    # gates use this for plays whose failure means the apparent backlog is not
    # trustworthy enough to declare the session complete.
    last_play_success_by_type: dict[PlayType, bool] = field(default_factory=dict)
    # Whether each play type's most-recent outcome was a no-op ``skip:*`` (vs a
    # genuine failure). ``ArmedByFailureGate`` uses this so a skip — which is
    # recorded success=False but is not a wedge — does not arm a self-heal play
    # (the write_impl skip ↔ reconcile arm/run loop that drove the no-op spin).
    last_play_skipped_by_type: dict[PlayType, bool] = field(default_factory=dict)
    # Tail run of consecutive non-productive (fail OR skip) outcomes per play
    # type. The 3-strikes circuit breaker (rl/mask.py) masks a work play once
    # this reaches its threshold, until the cooldown lifts — so a play that can
    # only skip (e.g. write_implementation_plan losing the resolve-time TOCTOU
    # race) stops being re-selected instead of spinning.
    consecutive_nonproductive_by_type: dict[PlayType, int] = field(default_factory=dict)
    # Action mask and reasons for IPC consumers (e.g. dashboard Plays Panel).
    # Populated by core after _build_state(); empty when registry is unavailable.
    action_mask: tuple[bool, ...] = field(default_factory=tuple)
    # ``mask_reasons`` holds typed MaskReason values (carrying classification);
    # the IPC serializer and UI consumers ``str()`` them at the surface
    # boundary. Kept as ``dict`` (not ``Mapping``) for ergonomic mutation
    # during state assembly.
    mask_reasons: dict[PlayType, MaskReason] = field(default_factory=dict)
    drain_reason: str | None = None
    # V1 contract fields (V1_CONTRACT.md §"AgentShore State Snapshot"). These let
    # IPC consumers (TUI status bar, dashboard HUD, desktop session.status)
    # render the contract surface.
    run_mode: RunMode = RunMode.SOLO
    action_space_version: int = 0
    policy_version: str = ""
    policy_checkpoint_id: str | None = None
    # Plays since the most-recent successful SEED_PROJECT. ``None`` until the
    # session has at least one successful seed in play_history.
    seed_freshness: int | None = None
    learnings_count: int = 0
    human_feedback_count: int = 0

    def work_queue(self) -> WorkQueueView:
        """Group issues and PRs into todo / in-progress / in-review / done.

        Lifecycle classification is a property of orchestrator state, not of any
        renderer; this is the single derivation both the issue work-queue screen
        and the dashboard summary widget format over.
        """
        prs_by_issue: dict[int, PullRequestSnapshot] = {}
        for pr in self.pull_requests:
            for issue_number in issue_numbers_for_pr(pr):
                existing = prs_by_issue.get(issue_number)
                if existing is None or (existing.state != "open" and pr.state == "open"):
                    prs_by_issue[issue_number] = pr

        pending_review_prs = {item.pr_number for item in self.pending_review_queue}
        reviewing_issues = {
            issue_number
            for pr in self.pull_requests
            if pr.state == "open" or pr.pr_number in pending_review_prs
            for issue_number in issue_numbers_for_pr(pr)
        }
        in_progress_issues = {
            agent.current_play_issue_number
            for agent in self.agents
            if agent.current_play_issue_number is not None and agent.current_play_type is not None
        }

        todo: list[WorkQueueItem] = []
        in_progress: list[WorkQueueItem] = []
        in_review: list[WorkQueueItem] = []
        done: list[WorkQueueItem] = []
        for issue in self.open_issues:
            item = WorkQueueItem(issue=issue, pr=prs_by_issue.get(issue.issue_number))
            if issue.state.lower() == "closed":
                done.append(item)
            elif issue.issue_number in in_progress_issues:
                in_progress.append(item)
            elif issue.issue_number in reviewing_issues:
                in_review.append(item)
            else:
                todo.append(item)

        known_issue_numbers = {issue.issue_number for issue in self.open_issues}
        orphan_review_prs: list[tuple[PullRequestSnapshot, bool]] = []
        for pr in self.pull_requests:
            if pr.state != "open":
                continue
            if known_issue_numbers.intersection(issue_numbers_for_pr(pr)):
                continue
            orphan_review_prs.append((pr, pr.pr_number in pending_review_prs))

        open_issues = [issue for issue in self.open_issues if issue.state.lower() == "open"]
        next_issue = (
            min(
                open_issues,
                key=lambda issue: (
                    issue.priority if issue.priority is not None else 999,
                    issue.issue_number,
                ),
            )
            if open_issues
            else None
        )

        return WorkQueueView(
            todo=todo,
            in_progress=in_progress,
            in_review=in_review,
            done=done,
            orphan_review_prs=orphan_review_prs,
            next_issue=next_issue,
        )


# ---------------------------------------------------------------------------
# StateProvider protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class StateProvider(Protocol):
    """Abstraction that decouples core from the UI layer.

    Implementations:
    - TuiStateProvider  -- feeds Textual widgets
    - IpcStateProvider  -- serializes NDJSON over local IPC
    - NullStateProvider -- headless / testing
    """

    async def on_state_update(self, state: OrchestratorState) -> None:
        """Push a full state snapshot after each play cycle."""
        ...

    async def on_play_started(self, play_type: PlayType, params: PlayParams) -> None:
        """Notify that a play has started executing."""
        ...

    async def on_play_completed(self, play: PlayOutcome) -> None:
        """Notify that a play has finished executing."""
        ...

    async def on_agent_changed(self, agent_id: str, status: AgentStatus) -> None:
        """Notify that an agent's status has changed."""
        ...

    async def on_agent_subprocess_spawned(
        self, agent_id: str, agent_type: AgentType, pid: int
    ) -> None:
        """Notify that a managed CLI subprocess has started."""
        ...

    async def on_agent_subprocess_exited(
        self, agent_id: str, agent_type: AgentType, pid: int, exit_code: int | None
    ) -> None:
        """Notify that a managed CLI subprocess has exited."""
        ...

    async def on_feedback_requested(self, reason: str) -> None:
        """Notify that the session requires human feedback before continuing."""
        ...

    async def on_session_paused(self, reason: str) -> None:
        """Notify that the session has been paused."""
        ...

    async def on_session_draining(self, reason: str) -> None:
        """Notify that graceful drain has begun; PPO will only dispatch end_agent."""
        ...

    async def on_session_ended(self, reason: str) -> None:
        """Notify that the session has completed (not a crash or disconnect)."""
        ...

    async def on_bootstrap_phase(self, phase: str, status: str, elapsed_ms: float) -> None:
        """Notify progress through a startup phase.

        ``phase`` is the step name (e.g. ``"init_ppo_selector"``,
        ``"ensure_labels"``). ``status`` is ``"started"`` or ``"completed"``.
        ``elapsed_ms`` is 0 on start, the measured duration on completion.

        Lets dashboards render a loading modal during the 60-120s startup
        window when no plays have been selected yet and the office board is
        otherwise indistinguishable from a stuck/broken session.
        """
        ...


class NullStateProvider:
    """No-op StateProvider for headless / testing use."""

    async def on_state_update(self, state: OrchestratorState) -> None:
        pass

    async def on_play_started(self, play_type: PlayType, params: PlayParams) -> None:
        pass

    async def on_play_completed(self, play: PlayOutcome) -> None:
        pass

    async def on_agent_changed(self, agent_id: str, status: AgentStatus) -> None:
        pass

    async def on_agent_subprocess_spawned(
        self, agent_id: str, agent_type: AgentType, pid: int
    ) -> None:
        pass

    async def on_agent_subprocess_exited(
        self, agent_id: str, agent_type: AgentType, pid: int, exit_code: int | None
    ) -> None:
        pass

    async def on_feedback_requested(self, reason: str) -> None:
        pass

    async def on_session_paused(self, reason: str) -> None:
        pass

    async def on_session_draining(self, reason: str) -> None:
        pass

    async def on_session_ended(self, reason: str) -> None:
        pass

    async def on_bootstrap_phase(self, phase: str, status: str, elapsed_ms: float) -> None:
        pass
