// AgentType is derived from the canonical AGENT_REGISTRY so it stays in sync
// automatically when new agent types are added.  Import it here for use within
// this file, and re-export it so downstream consumers still find it in types.ts.
import type { AgentType } from "./agentRegistry";

export type { AgentType };
export type AgentStatus = "idle" | "busy" | "error" | "terminated";
export type SessionState =
  | "initializing"
  | "running"
  | "paused"
  | "draining"
  | "shutting_down";
export type PlayType =
  | "instantiate_agent"
  | "unblock_pr"
  | "write_implementation_plan"
  | "end_agent"
  | "issue_pickup"
  | "code_review"
  | "merge_pr"
  | "run_qa"
  | "systematic_debugging"
  | "design_audit"
  | "end_session"
  | "reconcile_state"
  | "refine_task_breakdown"
  | "cleanup"
  | "future_4"
  | "take_break"
  | "groom_backlog"
  | "seed_project"
  | "calibrate_alignment"
  | "prune"
  | "future_7"
  | "future_8";
export type PRState = "open" | "closed" | "merged";
export type IssueState = "open" | "closed";

export interface ActivePlay {
  play_type: PlayType;
  play_id: number | null;
  started_at: string | null;
  issue_number: number | null;
  pr_number: number | null;
  branch: string | null;
  // Required to match the wire contract (serializer ActivePlayPayload /
  // PlayEventStarted). Every field is always present on the wire — nullable,
  // never absent. Construction sites that don't have a full set (fixtures,
  // demoTransport mocks, hud playBar local builders) should go through
  // {@link makeActivePlay} so the contract isn't weakened for tests.
  agent_id: string | null;
  phase: string | null;
  trigger_agent_id: string | null;
  trigger_agent_type: AgentType | string | null;
  trigger_error_class: string | null;
}

/**
 * Build an {@link ActivePlay} from a partial, filling every wire field with its
 * null default. Lets fixtures, the demo transport, and local HUD builders
 * construct an ActivePlay without restating the full nullable field set — so the
 * interface can stay strict (matching the wire) without the boilerplate leaking
 * into every call site.
 */
export function makeActivePlay(
  partial: Pick<ActivePlay, "play_type"> & Partial<ActivePlay>,
): ActivePlay {
  return {
    play_id: null,
    started_at: null,
    issue_number: null,
    pr_number: null,
    branch: null,
    agent_id: null,
    phase: null,
    trigger_agent_id: null,
    trigger_agent_type: null,
    trigger_error_class: null,
    ...partial,
  };
}

export interface AgentSnapshot {
  agent_id: string;
  agent_type: AgentType;
  display_name?: string;
  model?: string | null;
  model_tier?: string | null;
  reasoning_effort?: string | null;
  status: AgentStatus;
  context_size: number;
  total_cost: number;
  total_tokens: number;
  tasks_completed: number;
  tasks_failed: number;
  current_play: ActivePlay | null;
  last_error_class?: string | null;
  // desktop-31h2: cumulative dispatch count for this agent + its share of
  // the live fleet-wide dispatch total. Both default to 0 when the server
  // is older than this field set (back-compat with sessions that predate
  // the agents.dispatch_count column).
  dispatch_count?: number;
  dispatch_share?: number;
  // TNQA critical: these were on AgentSnapshot server-side but missing from
  // the wire payload until the serializer parity fix. Optional for
  // back-compat with older servers.
  timeout_count?: number;
  consecutive_timeouts?: number;
  github_identity?: string | null;
}

export interface PullRequestSnapshot {
  pr_number: number;
  title: string;
  state: PRState;
  branch: string | null;
  issue_number: number | null;
  labels: string[];
  review_decision: string | null;
  status_check_summary: string | null;
  is_draft: boolean;
  blocked: boolean;
  blocked_reasons: string[];
  url: string | null;
  github_author: string | null;
  author_agent_id: string | null;
  author_agent_type: AgentType | null;
  // TNQA critical: on PullRequestSnapshot server-side but missing from the
  // wire payload until the serializer parity fix. Optional for back-compat.
  head_sha?: string | null;
  mergeable?: string | null;
  base_ref?: string | null;
  last_reviewed_sha?: string | null;
  last_review_status?: string | null;
}

export interface EpicStatus {
  bead_id: string;
  title: string;
  total_tasks: number;
  closed_tasks: number;
  closure_ratio: number;
}

export interface GraphTask {
  bead_id: string;
  title: string;
  status: string;
  parent_id: string | null;
  epic_id: string | null;
  epic_title: string | null;
  external_ref: string | null;
  issue_number: number | null;
  ready: boolean;
  closed_at?: string | null;
  updated_at?: string | null;
  // TNQA critical: were on GraphTask server-side but missing from the wire
  // payload until the serializer parity fix. Optional for back-compat.
  depends_on_ids?: string[];
  blocked_by_ids?: string[];
}

export interface ProjectGraph {
  epics: EpicStatus[];
  tasks: GraphTask[];
  tasks_ready: number;
  tasks_total: number;
  global_closure_ratio: number;
  // TNQA critical: was on ProjectGraph server-side but missing from the wire
  // payload until the serializer parity fix. Optional for back-compat.
  tasks_blocked?: number;
}

export interface BudgetSnapshot {
  enabled: boolean;
  total_budget: number | null;
  spent: number;
  remaining: number | null;
  estimated_cost_per_play: number;
  // Wall-clock soft cap (minutes), independent of the dollar cap above.
  // null when time_enabled is false (no wall-clock limit).
  time_enabled: boolean;
  time_total_minutes: number | null;
  time_elapsed_minutes: number | null;
  time_remaining_minutes: number | null;
}

export interface TrajectorySnapshot {
  projected_alignment_at_budget_end: number;
  estimated_remaining_plays: number;
  estimated_remaining_cost: number;
}

export interface PlayTypeStatsSnapshot {
  play_type: PlayType | string;
  total: number;
  successful: number;
  failed: number;
  success_rate: number;
  total_cost: number;
  avg_duration_seconds: number;
}

export interface AgentPlaySpecializationSnapshot {
  agent_id: string;
  play_type: PlayType | string;
  total: number;
  successful: number;
  failed: number;
  success_rate: number;
  rolling_success_rate: number;
}

export interface SessionStatsSnapshot {
  total_plays: number;
  successful_plays: number;
  failed_plays: number;
  success_rate: number;
  total_cost: number;
  avg_cost_per_play: number;
  total_tokens: number;
  avg_duration_seconds: number;
  by_play_type: PlayTypeStatsSnapshot[];
  agent_specialization?: AgentPlaySpecializationSnapshot[];
}

export interface IssueSnapshot {
  issue_number: number;
  title: string;
  state: IssueState;
  priority: number | null;
  labels: string[];
  source: string | null;
  url: string | null;
  created_at: string | null;
  closed_at: string | null;
  bead_id: string | null;
  bead_epic_id: string | null;
  bead_epic_title: string | null;
  bead_status: string | null;
  bead_ready: boolean;
  bead_mirror_status: "mirrored" | "missing" | "unlinked" | string;
  // TNQA critical: was on IssueSnapshot server-side but missing from the wire
  // payload until the serializer parity fix. Optional for back-compat.
  github_author?: string | null;
}

export interface WorkAvailability {
  tracked_issue_count: number;
  github_open_issue_count: number;
  workable_issue_count: number;
  blocked_issue_count: number;
  disallowed_issue_count: number;
  covered_by_open_pr_count: number;
  resolved_by_merged_pr_count: number;
  in_flight_issue_count: number;
  planning_eligible_count: number;
  implementation_eligible_count: number;
  refinement_eligible_count: number;
  debugging_eligible_count: number;
  reviewable_pr_count: number;
  mergeable_pr_count: number;
  unblockable_pr_count: number;
  actionable_pr_work_count: number;
  // Open PRs hidden because their base branch != the session target_branch
  // (Piece C target-branch filter). Drives the "(N hidden)" board badge.
  // Optional: absent on older servers that predate the filter.
  pull_requests_hidden_count?: number;
  terminal_no_work: boolean;
}

export interface MessageEnvelope {
  /** UUID4 message identifier. Present on all wire messages; may be absent on
   *  synthetic client-side messages (demo transport, bridge-generated events). */
  id?: string;
  /** ISO-8601 UTC timestamp of message creation. Present on all wire messages;
   *  may be absent on synthetic client-side messages. */
  timestamp?: string;
  /** Monotonically increasing sequence number assigned by the server. Present on
   *  all server-emitted wire messages. The client discards messages where
   *  seq <= lastSeenSeq to prevent stale or out-of-order messages from corrupting
   *  state. May be absent on synthetic client-side messages (demo transport). */
  seq?: number;
  /** Session this frame belongs to (Tier 1 contract). Stamped on every
   *  server-emitted frame so the client can detect a session boundary on any
   *  message type and reset cleanly. StateUpdate narrows this to required;
   *  synthetic client messages (ConnectionLost/Restored) omit it. */
  session_id?: string;
}

export interface StateUpdate extends MessageEnvelope {
  type: "state_update";
  session_id: string;
  session_state: SessionState;
  policy_mode: "learning" | "audit-replay";
  total_plays: number;
  total_cost: number;
  agents: AgentSnapshot[];
  open_issues: IssueSnapshot[];
  pull_requests: PullRequestSnapshot[];
  work_availability?: WorkAvailability;
  budget: BudgetSnapshot | null;
  trajectory: TrajectorySnapshot | null;
  active_play: ActivePlay | null;
  stats?: SessionStatsSnapshot | null;
  same_type_failure_streak: number;
  last_play_type: PlayType | null;
  forced_mask_zeros: string[];
  action_mask: boolean[];
  mask_reasons: Record<string, string>;
  graph?: ProjectGraph | null;
}

export interface PlayEventStarted extends MessageEnvelope {
  type: "play_event";
  status: "started";
  play_type: PlayType;
  agent_id: string | null;
  play_id: number | null;
  started_at: string | null;
  issue_number: number | null;
  pr_number: number | null;
  branch: string | null;
  trigger_agent_id: string | null;
  trigger_agent_type: AgentType | string | null;
  trigger_error_class: string | null;
}

export interface PlayEventCompleted extends MessageEnvelope {
  type: "play_event";
  status: "completed" | "failed";
  play_type: PlayType;
  agent_id: string | null;
  success: boolean;
  duration_seconds: number;
  dollar_cost: number;
  token_cost: number;
  artifacts: unknown[];
  alignment_delta: number;
  error: string | null;
  play_id: number | null;
  skipped: boolean;
  skip_category: "no_target" | "staffing" | "masked" | "invalid_config" | null;
  // Same contract as ActivePlay: always emitted, may be null. A future cleanup
  // could split PlayEvent{Started,Completed} into per-play_type variants
  // (issue plays, PR plays, agent-triggered plays) so the type system
  // narrows on play_type — that needs a coordinated server change since
  // agentshore.state.ActivePlay emits a single shape today.
  trigger_agent_id: string | null;
  trigger_agent_type: AgentType | string | null;
  trigger_error_class: string | null;
}

export type PlayEvent = PlayEventStarted | PlayEventCompleted;

export interface AgentChanged extends MessageEnvelope {
  type: "agent_changed";
  agent_id: string;
  status: AgentStatus;
}

export interface FeedbackRequested extends MessageEnvelope {
  type: "feedback_requested";
  reason: string;
  trigger: string;
}

export interface SessionPaused extends MessageEnvelope {
  type: "session_paused";
  reason: string;
}

export interface ConnectionLost {
  type: "connection_lost";
}

export interface SessionEnded extends MessageEnvelope {
  type: "session_ended";
  reason: string;
}

export interface SessionDraining extends MessageEnvelope {
  type: "session_draining";
  reason: string;
}

export interface ConnectionRestored {
  type: "connection_restored";
}

export interface ActivePlayReplay extends MessageEnvelope {
  type: "active_play_replay";
  active_play: ActivePlay | null;
}

export interface EventHistoryReplay extends MessageEnvelope {
  type: "event_history_replay";
  events: PlayEvent[];
}

export interface AuthToken extends MessageEnvelope {
  type: "auth_token";
  token: string;
}

export interface ReadOnly extends MessageEnvelope {
  type: "read_only";
}

export interface ErrorMessage extends MessageEnvelope {
  type: "error";
  error: string;
}

export interface BootstrapPhase extends MessageEnvelope {
  type: "bootstrap_phase";
  phase: string;
  status: "started" | "completed";
  elapsed_ms: number;
}

/**
 * Budget-only heartbeat frame. Emitted by the orchestrator on a fixed cadence
 * so the remaining-time countdown keeps ticking down during quiet stretches
 * (idle fleet, or one long-running play) when no full ``state_update`` fires.
 * Deliberately budget-only: consumers refresh just the budget bar and never
 * re-process agents, so the office sprites don't jitter.
 */
export interface BudgetUpdate extends MessageEnvelope {
  type: "budget_update";
  budget: BudgetSnapshot;
}

export type AgentShoreMessage =
  | StateUpdate
  | BudgetUpdate
  | PlayEvent
  | AgentChanged
  | FeedbackRequested
  | SessionPaused
  | SessionEnded
  | SessionDraining
  | ConnectionLost
  | ConnectionRestored
  | ActivePlayReplay
  | EventHistoryReplay
  | AuthToken
  | ReadOnly
  | ErrorMessage
  | BootstrapPhase;
