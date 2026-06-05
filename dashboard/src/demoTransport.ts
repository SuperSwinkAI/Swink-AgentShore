import type {
  ActivePlay,
  AgentSnapshot,
  AgentShoreMessage,
  GraphTask,
  PullRequestSnapshot,
  StateUpdate,
} from "./types";
import type { ConnectionState, DashboardTransport } from "./ws";

const BASE_TIME = "2026-01-01T00:00:00.000Z";

export type DemoScenario =
  | "empty"
  | "active"
  | "feedback"
  | "disconnected"
  | "stress"
  | "bootstrap";

export class DemoTransport implements DashboardTransport {
  state: ConnectionState = "closed";
  token: string | null = "demo-token";
  readOnly = false;
  onMessage: ((msg: AgentShoreMessage) => void) | null = null;
  onStateChange: ((state: ConnectionState) => void) | null = null;
  onReadOnlyChange: ((readOnly: boolean) => void) | null = null;

  constructor(private scenario: DemoScenario) {}

  connect(): void {
    this.setState("connecting");
    window.setTimeout(() => {
      this.setState("open");
      this.onMessage?.({ type: "auth_token", token: "demo-token" });
      this.emitScenario();
    }, 20);
  }

  send(command: Record<string, unknown>): void {
    if (command.command === "pause") {
      this.onMessage?.({ ...this.baseState(), session_state: "paused" });
    } else if (
      command.command === "resume" ||
      command.command === "feedback_response"
    ) {
      this.onMessage?.({ ...this.baseState(), session_state: "running" });
    } else if (command.command === "drain") {
      this.onMessage?.({ type: "session_draining", reason: "user_request" });
      this.onMessage?.({ ...this.baseState(), session_state: "draining" });
      setTimeout(() => {
        this.onMessage?.({ type: "session_ended", reason: "drain_complete" });
      }, 1500);
    } else if (command.command === "hard_stop") {
      this.onMessage?.({ ...this.baseState(), session_state: "shutting_down" });
      this.onMessage?.({ type: "session_ended", reason: "hard_stop" });
    } else if (command.command === "adjust_budget") {
      // No-op in demo: budget is static. A real server would update and re-emit state.
    }
  }

  disconnect(): void {
    this.setState("closed");
  }

  private emitScenario(): void {
    const state = this.baseState();
    if (this.scenario === "empty") {
      this.onMessage?.({
        ...state,
        total_plays: 0,
        total_cost: 0,
        agents: [],
        open_issues: [],
        stats: this.emptyStats(),
      });
      return;
    }

    if (this.scenario === "active" || this.scenario === "feedback") {
      const activePlay: ActivePlay = {
        play_type: "issue_pickup",
        agent_id: "agent-claude",
        play_id: 47,
        issue_number: 47,
        pr_number: null,
        branch: null,
        started_at: BASE_TIME,
      };
      this.onMessage?.({
        ...state,
        active_play: activePlay,
        agents: state.agents.map((agent) =>
          agent.agent_id === "agent-claude"
            ? {
                ...agent,
                status: "busy",
                current_play: {
                  play_type: "issue_pickup",
                  play_id: 47,
                  started_at: BASE_TIME,
                  issue_number: 47,
                  pr_number: null,
                  branch: null,
                },
              }
            : agent,
        ),
      });
      this.onMessage?.({
        type: "play_event",
        status: "started",
        play_type: "issue_pickup",
        agent_id: "agent-claude",
        play_id: 47,
        started_at: BASE_TIME,
        issue_number: 47,
        pr_number: null,
        branch: null,
        trigger_agent_id: null,
        trigger_agent_type: null,
        trigger_error_class: null,
      });
    } else {
      this.onMessage?.(state);
    }

    if (this.scenario === "feedback") {
      this.onMessage?.({
        type: "feedback_requested",
        reason: "budget_predictive",
        trigger: "budget_exhaustion",
      });
    }

    if (this.scenario === "bootstrap") {
      // Walks the modal through a representative phase sequence so the
      // BootstrapModal can be inspected without a live core process.
      const phases = [
        "init_datastore",
        "init_ppo_selector",
        "fetch_issues",
        "ensure_labels",
        "queue_agent_instantiation",
      ];
      let idx = 0;
      const advance = (): void => {
        if (idx >= phases.length) {
          this.onMessage?.({
            type: "bootstrap_phase",
            phase: "ready",
            status: "completed",
            elapsed_ms: 0,
          });
          return;
        }
        const phase = phases[idx++];
        this.onMessage?.({
          type: "bootstrap_phase",
          phase,
          status: "started",
          elapsed_ms: 0,
        });
        window.setTimeout(() => {
          this.onMessage?.({
            type: "bootstrap_phase",
            phase,
            status: "completed",
            elapsed_ms: 1500,
          });
          window.setTimeout(advance, 200);
        }, 1500);
      };
      advance();
    }

    if (this.scenario === "disconnected") {
      this.onMessage?.({ type: "connection_lost" });
      this.setState("reconnecting");
    }

    if (this.scenario === "stress") {
      this.onMessage?.(this.stressState());
    }
  }

  private stressState(): StateUpdate {
    // 10 todo, 8 in_progress, 7 reviewing, 12 done
    const epics = [
      {
        bead_id: "bd-auth",
        title: "Authentication System",
        total_tasks: 8,
        closed_tasks: 7,
        closure_ratio: 0.875,
      },
      {
        bead_id: "bd-ui",
        title: "Dashboard UI",
        total_tasks: 12,
        closed_tasks: 9,
        closure_ratio: 0.75,
      },
      {
        bead_id: "bd-api",
        title: "API Integration",
        total_tasks: 6,
        closed_tasks: 4,
        closure_ratio: 0.667,
      },
      {
        bead_id: "bd-infra",
        title: "Infrastructure",
        total_tasks: 11,
        closed_tasks: 5,
        closure_ratio: 0.455,
      },
    ];
    const epicIds = epics.map((epic) => epic.bead_id);

    const issueFor = (
      issueNumber: number,
      title: string,
      state: "open" | "closed",
      i: number,
    ) => {
      const epic = epics[i % epics.length];
      const beadStatus = state === "closed" ? "closed" : "open";
      return {
        issue_number: issueNumber,
        title,
        state,
        priority: (i % 3) + 1,
        labels: ["backend", "very-long-label-name-for-truncation-checks"],
        source: "github",
        url: `https://github.com/example/agentshore/issues/${issueNumber}`,
        created_at: BASE_TIME,
        closed_at: state === "closed" ? BASE_TIME : null,
        bead_id: `task-${issueNumber}`,
        bead_epic_id: epic.bead_id,
        bead_epic_title: epic.title,
        bead_status: beadStatus,
        bead_ready: beadStatus === "open",
        bead_mirror_status: "mirrored",
      };
    };

    // 8 busy agents each working a different in_progress issue (issues 101–108)
    const busyAgents: AgentSnapshot[] = Array.from({ length: 8 }, (_, i) => ({
      agent_id: `agent-stress-${i}`,
      agent_type: i % 2 === 0 ? "claude_code" : "codex",
      display_name: `Agent ${i + 1}`,
      status: "busy",
      context_size: 10000 + i * 1500,
      total_cost: 0.1 + i * 0.08,
      total_tokens: 20000 + i * 5000,
      tasks_completed: i,
      tasks_failed: 0,
      current_play: {
        play_type: "issue_pickup",
        play_id: 101 + i,
        started_at: BASE_TIME,
        issue_number: 101 + i,
        pr_number: null,
        branch: null,
      },
    }));

    // PRs for 7 reviewing issues (issues 111–117)
    const reviewingPrs: PullRequestSnapshot[] = Array.from(
      { length: 7 },
      (_, i) => ({
        pr_number: 200 + i,
        title: `PR for issue ${111 + i}`,
        state: "open",
        branch: `feature/issue-${111 + i}`,
        issue_number: 111 + i,
        labels: [],
        review_decision: null,
        status_check_summary: "success",
        is_draft: false,
        blocked: false,
        blocked_reasons: [],
        url: null,
        github_author: null,
        author_agent_id: null,
        author_agent_type: null,
      }),
    );

    const issues = [
      // 10 todo (121–130)
      ...Array.from({ length: 10 }, (_, i) =>
        issueFor(121 + i, `Todo task ${i + 1}`, "open", i),
      ),
      // 8 in_progress (101–108)
      ...Array.from({ length: 8 }, (_, i) =>
        issueFor(101 + i, `In-progress task ${i + 1}`, "open", i + 10),
      ),
      // 7 reviewing (111–117)
      ...Array.from({ length: 7 }, (_, i) =>
        issueFor(111 + i, `Review task ${i + 1}`, "open", i + 18),
      ),
      // 12 done (131–142, state closed)
      ...Array.from({ length: 12 }, (_, i) =>
        issueFor(131 + i, `Done task ${i + 1}`, "closed", i + 25),
      ),
    ];
    const tasks: GraphTask[] = issues.map((issue, i) => ({
      bead_id: issue.bead_id,
      title: issue.title,
      status: issue.bead_status ?? "open",
      parent_id: epicIds[i % epicIds.length],
      epic_id: issue.bead_epic_id,
      epic_title: issue.bead_epic_title,
      external_ref: `gh-${issue.issue_number}`,
      issue_number: issue.issue_number,
      ready: issue.bead_ready,
    }));
    tasks.push({
      bead_id: "task-unlinked",
      title: "Beads-native cleanup task",
      status: "open",
      parent_id: "bd-infra",
      epic_id: "bd-infra",
      epic_title: "Infrastructure",
      external_ref: null,
      issue_number: null,
      ready: true,
    });

    return {
      type: "state_update",
      session_id: "stress-session",
      session_state: "running",
      policy_mode: "learning",
      total_plays: 42,
      total_cost: 12.5,
      stats: {
        total_plays: 42,
        successful_plays: 34,
        failed_plays: 8,
        success_rate: 34 / 42,
        total_cost: 12.5,
        avg_cost_per_play: 12.5 / 42,
        total_tokens: 560000,
        avg_duration_seconds: 142,
        by_play_type: [
          {
            play_type: "issue_pickup",
            total: 18,
            successful: 14,
            failed: 4,
            success_rate: 14 / 18,
            total_cost: 6.1,
            avg_duration_seconds: 188,
          },
          {
            play_type: "code_review",
            total: 10,
            successful: 8,
            failed: 2,
            success_rate: 0.8,
            total_cost: 2.7,
            avg_duration_seconds: 96,
          },
          {
            play_type: "run_qa",
            total: 8,
            successful: 6,
            failed: 2,
            success_rate: 0.75,
            total_cost: 1.8,
            avg_duration_seconds: 121,
          },
          {
            play_type: "cleanup",
            total: 6,
            successful: 6,
            failed: 0,
            success_rate: 1,
            total_cost: 1.9,
            avg_duration_seconds: 64,
          },
        ],
      },
      agents: [...this.demoAgents(), ...busyAgents],
      open_issues: issues,
      pull_requests: reviewingPrs,
      budget: {
        enabled: true,
        total_budget: 50,
        spent: 12.5,
        remaining: 37.5,
        estimated_cost_per_play: 0.32,
      },
      trajectory: {
        projected_alignment_at_budget_end: 0.85,
        estimated_remaining_plays: 117,
        estimated_remaining_cost: 37.5,
      },
      active_play: null,
      same_type_failure_streak: 0,
      last_play_type: "run_qa",
      forced_mask_zeros: [],
      action_mask: Array.from({ length: 22 }, () => true),
      mask_reasons: {},
      graph: {
        epics,
        tasks,
        tasks_ready: 10,
        tasks_total: 37,
        global_closure_ratio: 0.676,
      },
    };
  }

  private baseState(): StateUpdate {
    const epics = [
      {
        bead_id: "bd-auth",
        title: "Authentication System",
        total_tasks: 8,
        closed_tasks: 5,
        closure_ratio: 0.625,
      },
      {
        bead_id: "bd-ui",
        title: "Dashboard UI",
        total_tasks: 12,
        closed_tasks: 3,
        closure_ratio: 0.25,
      },
      {
        bead_id: "bd-api",
        title: "API Integration",
        total_tasks: 6,
        closed_tasks: 0,
        closure_ratio: 0.0,
      },
    ];
    const openIssues = [
      {
        issue_number: 47,
        title:
          "Implement session budget guard with a deliberately long title for card truncation",
        state: "open" as const,
        priority: 1,
        labels: ["backend", "priority-critical", "needs-dashboard-review"],
        source: "github",
        url: "https://github.com/example/agentshore/issues/47",
        created_at: BASE_TIME,
        closed_at: null,
        bead_id: "task-47",
        bead_epic_id: "bd-auth",
        bead_epic_title: "Authentication System",
        bead_status: "open",
        bead_ready: true,
        bead_mirror_status: "mirrored",
      },
      {
        issue_number: 60,
        title: "Add dashboard visual checks",
        state: "open" as const,
        priority: 2,
        labels: ["dashboard", "visual-regression", "long-label-for-truncation"],
        source: "github",
        url: "https://github.com/example/agentshore/issues/60",
        created_at: BASE_TIME,
        closed_at: null,
        bead_id: "task-60",
        bead_epic_id: "bd-ui",
        bead_epic_title: "Dashboard UI",
        bead_status: "open",
        bead_ready: true,
        bead_mirror_status: "mirrored",
      },
    ];
    const tasks: GraphTask[] = openIssues.map((issue) => ({
      bead_id: issue.bead_id,
      title: issue.title,
      status: issue.bead_status,
      parent_id: issue.bead_epic_id,
      epic_id: issue.bead_epic_id,
      epic_title: issue.bead_epic_title,
      external_ref: `gh-${issue.issue_number}`,
      issue_number: issue.issue_number,
      ready: issue.bead_ready,
    }));
    return {
      type: "state_update",
      session_id: "demo-session",
      session_state: "running",
      policy_mode: "learning",
      total_plays: 12,
      total_cost: 1.84,
      stats: {
        total_plays: 12,
        successful_plays: 9,
        failed_plays: 3,
        success_rate: 0.75,
        total_cost: 1.84,
        avg_cost_per_play: 1.84 / 12,
        total_tokens: 133000,
        avg_duration_seconds: 94,
        by_play_type: [
          {
            play_type: "issue_pickup",
            total: 5,
            successful: 4,
            failed: 1,
            success_rate: 0.8,
            total_cost: 0.82,
            avg_duration_seconds: 118,
          },
          {
            play_type: "code_review",
            total: 4,
            successful: 3,
            failed: 1,
            success_rate: 0.75,
            total_cost: 0.61,
            avg_duration_seconds: 83,
          },
          {
            play_type: "run_qa",
            total: 3,
            successful: 2,
            failed: 1,
            success_rate: 2 / 3,
            total_cost: 0.41,
            avg_duration_seconds: 76,
          },
        ],
      },
      agents: this.demoAgents(),
      open_issues: openIssues,
      budget: {
        enabled: true,
        total_budget: 5,
        spent: 1.84,
        remaining: 3.16,
        estimated_cost_per_play: 0.32,
      },
      trajectory: {
        projected_alignment_at_budget_end: 0.72,
        estimated_remaining_plays: 9,
        estimated_remaining_cost: 2.88,
      },
      pull_requests: [],
      active_play: null,
      same_type_failure_streak: 0,
      last_play_type: "run_qa",
      forced_mask_zeros: [],
      // V1_ACTION_ORDER: instantiate_agent, unblock_pr, write_implementation_plan,
      // end_agent, issue_pickup, code_review, merge_pr, run_qa,
      // systematic_debugging, design_audit, end_session, reconcile_state,
      // refine_task_breakdown, cleanup, future_4, take_break,
      // groom_backlog, seed_project, calibrate_alignment, prune,
      // future_7, future_8
      action_mask: [
        true, // instantiate_agent
        true, // unblock_pr
        true, // write_implementation_plan
        true, // end_agent
        true, // issue_pickup
        true, // code_review
        false, // merge_pr — no open PR
        true, // run_qa
        true, // systematic_debugging
        true, // design_audit
        false, // end_session — goals not aligned
        true, // reconcile_state
        true, // refine_task_breakdown
        true, // cleanup
        false, // future_4 — reserved
        false, // take_break — rate-limit cooldown
        true, // groom_backlog
        false, // seed_project — already seeded
        true, // calibrate_alignment
        false, // prune — no prune-worthy debt yet
        false, // future_7 — reserved
        false, // future_8 — reserved
      ],
      mask_reasons: {
        merge_pr: "No open PR ready to merge",
        end_session: "Goals not yet aligned",
        take_break: "Reserved for rate-limit cooldown",
        seed_project: "Beads graph already seeded",
        prune: "No prune-worthy debt yet",
        future_4: "Reserved action slot",
        future_7: "Reserved action slot",
        future_8: "Reserved action slot",
      },
      graph: {
        epics,
        tasks,
        tasks_ready: 4,
        tasks_total: 26,
        global_closure_ratio: 0.308,
      },
    };
  }

  private demoAgents(): AgentSnapshot[] {
    return [
      {
        agent_id: "agent-claude",
        agent_type: "claude_code",
        display_name: "Claude: Quantum Sentinel",
        model_tier: "large",
        status: "idle",
        context_size: 18320,
        total_cost: 0.42,
        total_tokens: 92000,
        tasks_completed: 3,
        tasks_failed: 1,
        current_play: null,
      },
      {
        agent_id: "agent-codex",
        agent_type: "codex",
        display_name: "Codex: Static Ranger",
        model_tier: "medium",
        status: "idle",
        context_size: 8100,
        total_cost: 0.18,
        total_tokens: 41000,
        tasks_completed: 2,
        tasks_failed: 0,
        current_play: null,
      },
    ];
  }

  private emptyStats(): StateUpdate["stats"] {
    return {
      total_plays: 0,
      successful_plays: 0,
      failed_plays: 0,
      success_rate: 0,
      total_cost: 0,
      avg_cost_per_play: 0,
      total_tokens: 0,
      avg_duration_seconds: 0,
      by_play_type: [],
    };
  }

  private setState(state: ConnectionState): void {
    this.state = state;
    this.onStateChange?.(state);
  }
}

export function createDemoTransport(params: URLSearchParams): DemoTransport {
  const rawScenario = params.get("scenario");
  const scenario: DemoScenario =
    rawScenario === "empty" ||
    rawScenario === "active" ||
    rawScenario === "feedback" ||
    rawScenario === "disconnected" ||
    rawScenario === "stress" ||
    rawScenario === "bootstrap"
      ? rawScenario
      : "active";
  return new DemoTransport(scenario);
}
