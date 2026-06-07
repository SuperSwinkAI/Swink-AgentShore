import React from "react";
import { createRoot, type Root } from "react-dom/client";
import { act } from "react";
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  DashboardCanvas,
  Dashboard,
  FeedbackModal,
  HUD,
  KanbanStage,
  PlaysPanel,
  notifyFeedbackModalHide,
  notifyFeedbackModalShow,
  notifyPlaysPanelUpdate,
} from "../src/index";
import EventDrawer, {
  notifyEventDrawerEvent,
  notifyEventDrawerReset,
  notifyEventDrawerStateUpdate,
} from "../src/components/EventDrawer";
import {
  SidePanelComponent,
  notifySidePanelUpdate,
} from "../src/components/SidePanel";
import type {
  AgentSnapshot,
  AgentShoreMessage,
  PlayEventCompleted,
  PlayEventStarted,
  StateUpdate,
} from "../src/types";
import type { ConnectionState, DashboardTransport } from "../src/ws";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  document.body.innerHTML = "";
  document.body.className = "";
  localStorage.clear();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  notifyEventDrawerReset();
  notifyFeedbackModalHide();
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
});

describe("DashboardCanvas", () => {
  it("mounts an HTMLCanvasElement (no longer a text placeholder)", async () => {
    await act(async () => {
      root.render(<DashboardCanvas />);
    });
    const el = container.querySelector("[data-agentshore-dashboard-canvas]");
    expect(el).not.toBeNull();
    expect(el?.tagName).toBe("CANVAS");
  });

  it("does not throw when the host environment lacks a 2D context (jsdom)", async () => {
    // jsdom's HTMLCanvasElement.getContext() returns null. The
    // component must mount the canvas element gracefully and let the
    // game loop short-circuit without surfacing an error to React.
    await act(async () => {
      root.render(<DashboardCanvas />);
    });
    const el = container.querySelector(
      "[data-agentshore-dashboard-canvas]",
    ) as HTMLCanvasElement | null;
    expect(el).not.toBeNull();
    // ``data-mounted`` is "false" when the loop couldn't attach (jsdom)
    // and "true" in a real browser environment with canvas support.
    expect(["true", "false"]).toContain(el?.getAttribute("data-mounted"));
  });
});

describe("Dashboard", () => {
  it("scopes dashboard body chrome while mounted", async () => {
    await act(async () => {
      root.render(<Dashboard showThemeToggle={false} />);
    });

    expect(document.body.classList.contains("dashboard-active")).toBe(true);

    await act(async () => {
      root.render(<div />);
    });

    expect(document.body.classList.contains("dashboard-active")).toBe(false);
  });

  it("uses the shared corrected top chrome without dropping dashboard panels", async () => {
    await act(async () => {
      root.render(<Dashboard showThemeToggle />);
    });

    expect(
      container.querySelector("#top-bar.dashboard-main-chrome"),
    ).not.toBeNull();
    expect(
      container.querySelector("#topbar-left-mount .session-state"),
    ).not.toBeNull();
    expect(
      container.querySelector("#topbar-left-mount #plays-count"),
    ).not.toBeNull();
    expect(container.querySelector("#left-panel")).not.toBeNull();
    expect(container.querySelector("#stage-tabs")).not.toBeNull();
    expect(container.querySelector("#side-panel")).not.toBeNull();
    expect(container.querySelector("#plays-panel")).not.toBeNull();
    expect(container.querySelector("#theme-toggle")).not.toBeNull();
  });

  it("fires onFirstStateUpdate on the first state_update even with no instantiate_agent (issue #10)", async () => {
    const transport = new FakeTransport();
    const onFirstStateUpdate = vi.fn();
    const onFirstAgentInstantiated = vi.fn();

    await act(async () => {
      root.render(
        <Dashboard
          transport={transport}
          showThemeToggle={false}
          onFirstStateUpdate={onFirstStateUpdate}
          onFirstAgentInstantiated={onFirstAgentInstantiated}
        />,
      );
    });

    // No-work session: bridge goes live and streams a state_update, but
    // no agent is ever instantiated. The overlay-dismiss callback must
    // still fire from the state_update alone.
    await act(async () => {
      transport.emit(stateUpdate({ agents: [] }));
    });

    expect(onFirstStateUpdate).toHaveBeenCalledOnce();
    expect(onFirstAgentInstantiated).not.toHaveBeenCalled();

    // Subsequent state_updates must not re-fire the once-per-mount callback.
    await act(async () => {
      transport.emit(stateUpdate({ agents: [], total_plays: 1 }));
    });

    expect(onFirstStateUpdate).toHaveBeenCalledOnce();
  });
});

describe("HUD", () => {
  it("mounts the HUD root element", async () => {
    await act(async () => {
      root.render(<HUD />);
    });
    expect(
      container.querySelector("[data-agentshore-dashboard-hud]"),
    ).not.toBeNull();
  });
});

describe("PlaysPanel", () => {
  it("renders real play-tray UI, not the empty stub", async () => {
    await act(async () => {
      root.render(<PlaysPanel />);
    });
    expect(container.textContent).toContain("PLAYS");
  });

  it("does not render the empty stub data attribute", async () => {
    await act(async () => {
      root.render(<PlaysPanel />);
    });
    expect(
      container.querySelector("[data-agentshore-dashboard-plays-panel]"),
    ).toBeNull();
  });

  it("renders the full 22-action tray with the reconcile and prune slots and two reserved slots", async () => {
    await act(async () => {
      root.render(<PlaysPanel />);
    });

    expect(container.querySelectorAll(".pp-card")).toHaveLength(22);
    expect(
      container.querySelector("[data-play-key='reconcile_state']"),
    ).not.toBeNull();
    // Slot 19 (formerly future_6) now hosts the active PRUNE play.
    expect(container.querySelector("[data-play-key='prune']")).not.toBeNull();
    expect(container.querySelector("[data-play-key='future_6']")).toBeNull();
    expect(
      container.querySelector("[data-play-key='future_7']"),
    ).not.toBeNull();
    expect(
      container.querySelector("[data-play-key='future_8']"),
    ).not.toBeNull();
    expect(container.textContent).toContain("Prune");
    expect(container.textContent).toContain("22 TOTAL");
  });

  it("wraps the existing budget meter with the draining message", async () => {
    await act(async () => {
      root.render(
        <PlaysPanel
          drainStatus={{
            visible: true,
            reason: "budget_reserve_reached",
            connectionLost: false,
          }}
        />,
      );
    });
    await act(async () => {
      notifyPlaysPanelUpdate(
        stateUpdate({
          total_cost: 55.01,
          budget: {
            enabled: true,
            total_budget: 60,
            spent: 55.01,
            remaining: 4.99,
            estimated_cost_per_play: 0.25,
          },
        }),
      );
    });

    expect(container.querySelector(".budget-drain-wrapper")).not.toBeNull();
    expect(container.querySelector("#drain-banner")).toBeNull();
    expect(container.textContent).toContain(
      "DRAINING (budget_reserve_reached)",
    );
    expect(container.textContent).toContain("$55.01 / $60.00");
    expect(container.textContent).toContain("waiting for agents to finish");
  });

  it("appends remaining time to the budget label when a time cap is set", async () => {
    await act(async () => {
      root.render(<PlaysPanel />);
    });
    await act(async () => {
      notifyPlaysPanelUpdate(
        stateUpdate({
          total_cost: 12.5,
          budget: {
            enabled: true,
            total_budget: 200,
            spent: 12.5,
            remaining: 187.5,
            estimated_cost_per_play: 0.25,
            time_enabled: true,
            time_total_minutes: 1440,
            time_elapsed_minutes: 86,
            time_remaining_minutes: 1354,
          },
        }),
      );
    });

    // 1354 minutes -> 22h 34m. Dollars and time-left are separate segments
    // with the meter physically between them: $ · [meter] · time-left.
    const bar = container.querySelector(".budget-bar") as HTMLElement;
    const dollar = bar.querySelector("#budget-label") as HTMLElement;
    const time = bar.querySelector(".budget-time") as HTMLElement;
    expect(dollar.textContent).toBe("$12.50 / $200.00");
    expect(time.textContent).toBe("22h 34m left");
    // Meter renders between the two figures.
    const kids = Array.from(bar.children);
    expect(kids.indexOf(dollar)).toBeLessThan(
      kids.indexOf(bar.querySelector(".budget-track") as HTMLElement),
    );
    expect(
      kids.indexOf(bar.querySelector(".budget-track") as HTMLElement),
    ).toBeLessThan(kids.indexOf(time));
    // Hover tooltip carries the full combined breakdown.
    expect(bar.getAttribute("title")).toBe("$12.50 / $200.00 · 22h 34m left");
  });

  it("omits the time suffix when no time cap is set (dollar-only)", async () => {
    await act(async () => {
      root.render(<PlaysPanel />);
    });
    await act(async () => {
      notifyPlaysPanelUpdate(
        stateUpdate({
          total_cost: 12.5,
          budget: {
            enabled: true,
            total_budget: 200,
            spent: 12.5,
            remaining: 187.5,
            estimated_cost_per_play: 0.25,
            time_enabled: false,
            time_total_minutes: null,
            time_elapsed_minutes: null,
            time_remaining_minutes: null,
          },
        }),
      );
    });

    expect(container.textContent).toContain("$12.50 / $200.00");
    expect(container.textContent).not.toContain("left");
  });

  it("shows time-only runs without a dollar cap and fills the meter from elapsed time", async () => {
    await act(async () => {
      root.render(<PlaysPanel />);
    });
    await act(async () => {
      notifyPlaysPanelUpdate(
        stateUpdate({
          total_cost: 0.05,
          budget: {
            enabled: false,
            total_budget: null,
            spent: 0.05,
            remaining: null,
            estimated_cost_per_play: 0.25,
            time_enabled: true,
            time_total_minutes: 60,
            time_elapsed_minutes: 45,
            time_remaining_minutes: 15,
          },
        }),
      );
    });

    // Dollars uncapped (no "(unlimited)" verbosity, no "∞" since time is capped),
    // and the time cap survives instead of being ellipsized.
    expect(
      (container.querySelector("#budget-label") as HTMLElement).textContent,
    ).toBe("$0.05");
    expect(
      (container.querySelector(".budget-time") as HTMLElement).textContent,
    ).toBe("15m left");
    expect(container.textContent).not.toContain("unlimited");
    // The meter tracks the binding (time) dimension: 45/60 = 75% -> warning.
    const fill = container.querySelector("#budget-fill") as HTMLElement;
    expect(fill.style.width).toBe("75%");
    expect(fill.className).toContain("warning");
  });

  it("collapses a fully-uncapped run to a compact infinity marker", async () => {
    await act(async () => {
      root.render(<PlaysPanel />);
    });
    await act(async () => {
      notifyPlaysPanelUpdate(
        stateUpdate({
          total_cost: 0.05,
          budget: {
            enabled: false,
            total_budget: null,
            spent: 0.05,
            remaining: null,
            estimated_cost_per_play: 0.25,
            time_enabled: false,
            time_total_minutes: null,
            time_elapsed_minutes: null,
            time_remaining_minutes: null,
          },
        }),
      );
    });

    expect(
      (container.querySelector("#budget-label") as HTMLElement).textContent,
    ).toBe("$0.05");
    expect(
      (container.querySelector(".budget-time") as HTMLElement).textContent,
    ).toBe("∞");
    expect(container.textContent).not.toContain("unlimited");
    expect(container.textContent).not.toContain("left");
    const fill = container.querySelector("#budget-fill") as HTMLElement;
    expect(fill.style.width).toBe("0%");
  });
});

describe("FeedbackModal", () => {
  it("dismisses after a terminal selection", async () => {
    const onContinue = vi.fn();

    await act(async () => {
      root.render(<FeedbackModal onContinue={onContinue} />);
    });
    await act(async () => {
      notifyFeedbackModalShow("loop_detected");
    });

    expect(container.querySelector("#feedback-modal")?.className).toBe(
      "visible",
    );

    await act(async () => {
      (
        container.querySelector("#feedback-continue") as HTMLButtonElement
      ).click();
    });

    expect(onContinue).toHaveBeenCalledOnce();
    expect(container.querySelector("#feedback-modal")?.className).toBe("");
  });

  it("dismisses after submitting a valid budget adjustment", async () => {
    const onAdjustBudget = vi.fn();

    await act(async () => {
      root.render(<FeedbackModal onAdjustBudget={onAdjustBudget} />);
    });
    await act(async () => {
      notifyFeedbackModalShow("budget_predictive");
    });
    await act(async () => {
      (
        container.querySelector("#feedback-add-budget") as HTMLButtonElement
      ).click();
    });
    await act(async () => {
      const input = container.querySelector(
        "#feedback-budget-amount",
      ) as HTMLInputElement;
      const valueSetter = Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype,
        "value",
      )?.set;
      valueSetter?.call(input, "25");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      (
        container.querySelector("#feedback-budget-confirm") as HTMLButtonElement
      ).click();
    });

    expect(onAdjustBudget).toHaveBeenCalledWith(25);
    expect(container.querySelector("#feedback-modal")?.className).toBe("");
  });
});

function stateUpdate(overrides: Partial<StateUpdate> = {}): StateUpdate {
  return {
    type: "state_update",
    session_id: "test-session",
    session_state: "running",
    policy_mode: "learning",
    total_plays: 0,
    total_cost: 0,
    agents: [],
    open_issues: [],
    pull_requests: [],
    budget: null,
    trajectory: null,
    active_play: null,
    same_type_failure_streak: 0,
    last_play_type: null,
    forced_mask_zeros: [],
    action_mask: Array(22).fill(true),
    mask_reasons: {},
    ...overrides,
  };
}

/** Minimal in-memory transport for driving Dashboard message handling. */
class FakeTransport implements DashboardTransport {
  onMessage: ((msg: AgentShoreMessage) => void) | null = null;
  onStateChange: ((state: ConnectionState) => void) | null = null;

  connect(): void {
    this.onStateChange?.("open");
  }
  send(): void {
    // no-op for tests
  }
  disconnect(): void {
    this.onStateChange?.("closed");
  }
  emit(msg: AgentShoreMessage): void {
    this.onMessage?.(msg);
  }
}

function hhmm(value: string): string {
  const date = new Date(value);
  return `${String(date.getHours()).padStart(2, "0")}:${String(
    date.getMinutes(),
  ).padStart(2, "0")}`;
}

describe("KanbanStage", () => {
  it("renders a real Issues header instead of the JSX placeholder text", async () => {
    await act(async () => {
      root.render(<KanbanStage />);
    });

    expect(container.textContent).toContain("Issues");
    expect(container.textContent).not.toContain("// issues");
  });
});

describe("EventDrawer", () => {
  it("renders unlabeled play cards with a start/end time row", async () => {
    const startedAt = "2026-01-01T00:19:00.000Z";
    const endedAt = "2026-01-01T00:45:00.000Z";
    const started = {
      type: "play_event",
      status: "started",
      play_type: "refine_task_breakdown",
      agent_id: "agent-1",
      play_id: 101,
      started_at: startedAt,
      issue_number: 189,
      pr_number: null,
      branch: null,
      trigger_agent_id: null,
      trigger_agent_type: null,
      trigger_error_class: null,
    } satisfies PlayEventStarted;
    const completed = {
      type: "play_event",
      status: "completed",
      play_type: "refine_task_breakdown",
      agent_id: "agent-1",
      success: true,
      duration_seconds: 26 * 60,
      dollar_cost: 0,
      token_cost: 0,
      artifacts: [],
      alignment_delta: 0,
      error: null,
      play_id: 101,
      skipped: false,
      skip_category: null,
      trigger_agent_id: null,
      trigger_agent_type: null,
      trigger_error_class: null,
      timestamp: endedAt,
    } satisfies PlayEventCompleted;

    await act(async () => {
      root.render(<EventDrawer />);
    });
    await act(async () => {
      notifyEventDrawerEvent(started);
    });

    expect(container.textContent).toContain(`${hhmm(startedAt)} -`);
    expect(container.textContent).not.toContain("Name");
    expect(container.textContent).not.toContain("Type");
    expect(container.textContent).not.toContain("Status/Result");

    await act(async () => {
      notifyEventDrawerEvent(completed);
    });

    expect(container.textContent).toContain(
      `${hhmm(startedAt)} - ${hhmm(endedAt)}`,
    );
  });

  it("renders a capped failure detail only for failed events", async () => {
    const longError = "x".repeat(160);
    const failed = {
      type: "play_event",
      status: "failed",
      play_type: "run_qa",
      agent_id: "agent-1",
      success: false,
      duration_seconds: 12,
      dollar_cost: 0,
      token_cost: 0,
      artifacts: [],
      alignment_delta: 0,
      error: longError,
      play_id: 202,
      skipped: false,
      skip_category: null,
      trigger_agent_id: null,
      trigger_agent_type: null,
      trigger_error_class: null,
      timestamp: "2026-01-01T00:45:00.000Z",
    } satisfies PlayEventCompleted;
    const completed = {
      ...failed,
      status: "completed",
      success: true,
      error: longError,
      play_id: 203,
    } satisfies PlayEventCompleted;

    await act(async () => {
      root.render(<EventDrawer />);
    });
    await act(async () => {
      notifyEventDrawerEvent(failed);
      notifyEventDrawerEvent(completed);
    });

    const failedCard = container.querySelector(".event-card.failed");
    const completedCard = container.querySelector(".event-card.completed");
    const errorMessage = failedCard?.querySelector(".event-error-message");
    const messageText =
      errorMessage?.textContent?.replace(/^FAILED:\s*/, "") ?? "";

    expect(errorMessage).not.toBeNull();
    expect(messageText).toHaveLength(140);
    expect(messageText.endsWith("…")).toBe(true);
    expect(completedCard?.querySelector(".event-error-message")).toBeNull();
  });

  it("does NOT auto-complete a Running card from an idle+null agent snapshot", async () => {
    // Regression for desktop-y3kq (2026-05-22): the reconciler used to
    // infer completion from ``agent.status === "idle"`` + null
    // ``current_play``. That path repeatedly mis-fired during active
    // plays — the orchestrator transitions through brief idle snapshots
    // while a play is genuinely in progress. Inferring completion from
    // agent state is a backstop for missed play_event "completed"
    // events; false-positive completions hide real bugs (a stuck-on-
    // running card is unambiguous, a wrong "Completed" looks correct).
    // The reducer now trusts play_event "completed" exclusively.
    const runningAgent: AgentSnapshot = {
      agent_id: "agent-1",
      agent_type: "codex" as const,
      display_name: "Codex: Runner",
      model_tier: "medium",
      status: "busy" as const,
      context_size: 0,
      total_cost: 0,
      total_tokens: 0,
      tasks_completed: 0,
      tasks_failed: 0,
      current_play: {
        play_type: "issue_pickup",
        play_id: 44,
        started_at: "2026-01-01T00:00:00.000Z",
        issue_number: 44,
        pr_number: null,
        branch: null,
      },
    };
    const started = {
      type: "play_event",
      status: "started",
      play_type: "issue_pickup",
      agent_id: "agent-1",
      play_id: 44,
      started_at: "2026-01-01T00:00:00.000Z",
      issue_number: 44,
      pr_number: null,
      branch: null,
      trigger_agent_id: null,
      trigger_agent_type: null,
      trigger_error_class: null,
    } satisfies PlayEventStarted;

    await act(async () => {
      root.render(<EventDrawer />);
    });
    await act(async () => {
      notifyEventDrawerStateUpdate(stateUpdate({ agents: [runningAgent] }));
      notifyEventDrawerEvent(started);
    });

    expect(container.textContent).toContain("Issue Pickup 44");
    expect(container.textContent).not.toContain(
      "Completed (status reconciled)",
    );

    // Agent goes idle with null current_play. Before: card flipped to
    // "Completed (status reconciled)". After: card stays as Running.
    await act(async () => {
      notifyEventDrawerStateUpdate(
        stateUpdate({
          agents: [
            {
              ...runningAgent,
              status: "idle",
              current_play: null,
            },
          ],
        }),
      );
    });

    expect(container.textContent).toContain("Issue Pickup 44");
    expect(container.textContent).not.toContain(
      "Completed (status reconciled)",
    );
  });

  it("does NOT reconcile a Running card when the agent is still busy but current_play is transiently empty", async () => {
    // Same false-positive guard, busy-agent variant.
    const runningAgent: AgentSnapshot = {
      agent_id: "agent-1",
      agent_type: "claude_code" as const,
      display_name: "Claude: Runner",
      model_tier: "large",
      status: "busy" as const,
      context_size: 0,
      total_cost: 0,
      total_tokens: 0,
      tasks_completed: 0,
      tasks_failed: 0,
      current_play: {
        play_type: "seed_project",
        play_id: 88,
        started_at: "2026-01-01T00:00:00.000Z",
        issue_number: null,
        pr_number: null,
        branch: null,
      },
    };
    const started = {
      type: "play_event",
      status: "started",
      play_type: "seed_project",
      agent_id: "agent-1",
      play_id: 88,
      started_at: "2026-01-01T00:00:00.000Z",
      issue_number: null,
      pr_number: null,
      branch: null,
      trigger_agent_id: null,
      trigger_agent_type: null,
      trigger_error_class: null,
    } satisfies PlayEventStarted;

    await act(async () => {
      root.render(<EventDrawer />);
    });
    await act(async () => {
      notifyEventDrawerStateUpdate(stateUpdate({ agents: [runningAgent] }));
      notifyEventDrawerEvent(started);
    });

    expect(container.textContent).toContain("Seed Project");
    expect(container.textContent).not.toContain(
      "Completed (status reconciled)",
    );

    // Transient empty current_play while the agent is still ``busy``.
    await act(async () => {
      notifyEventDrawerStateUpdate(
        stateUpdate({
          agents: [
            {
              ...runningAgent,
              status: "busy",
              current_play: null,
            },
          ],
        }),
      );
    });

    // Card must still be running. No "(status reconciled)" ghost.
    expect(container.textContent).toContain("Seed Project");
    expect(container.textContent).not.toContain(
      "Completed (status reconciled)",
    );
  });
});

describe("SidePanelComponent", () => {
  it("colors agent dots from the agent_type sprite palette", async () => {
    notifySidePanelUpdate(
      stateUpdate({
        agents: [
          {
            agent_id: "claude-small",
            agent_type: "claude_code",
            display_name: "Small Claude: A",
            model_tier: "small",
            status: "idle",
            context_size: 0,
            total_cost: 0,
            total_tokens: 0,
            tasks_completed: 0,
            tasks_failed: 0,
            current_play: null,
          },
          {
            agent_id: "claude-large",
            agent_type: "claude_code",
            display_name: "Large Claude: B",
            model_tier: "large",
            status: "busy",
            context_size: 0,
            total_cost: 0,
            total_tokens: 0,
            tasks_completed: 0,
            tasks_failed: 0,
            current_play: null,
          },
        ],
      }),
    );

    await act(async () => {
      root.render(<SidePanelComponent />);
    });

    const dots = Array.from(
      container.querySelectorAll<HTMLElement>(
        ".agent-status[data-agent-type='claude_code']",
      ),
    );
    expect(dots).toHaveLength(2);
    expect(dots[0].style.background).toBe(dots[1].style.background);
  });

  it("uses the Grok side-panel color for Grok agents", async () => {
    notifySidePanelUpdate(
      stateUpdate({
        agents: [
          {
            agent_id: "grok-medium",
            agent_type: "grok",
            display_name: "Grok: K",
            model_tier: "medium",
            status: "idle",
            context_size: 0,
            total_cost: 0,
            total_tokens: 0,
            tasks_completed: 0,
            tasks_failed: 0,
            current_play: null,
          },
        ],
      }),
    );

    await act(async () => {
      root.render(<SidePanelComponent />);
    });

    const dot = container.querySelector<HTMLElement>(
      ".agent-status[data-agent-type='grok']",
    );
    expect(dot).not.toBeNull();
    expect(dot?.style.background).toBe("rgb(20, 184, 166)");
  });

  it("renders the desktop-31h2 dispatch-share badge per agent", async () => {
    notifySidePanelUpdate(
      stateUpdate({
        agents: [
          {
            agent_id: "claude-a",
            agent_type: "claude_code",
            display_name: "Claude A",
            model_tier: "small",
            status: "idle",
            context_size: 0,
            total_cost: 0,
            total_tokens: 0,
            tasks_completed: 4,
            tasks_failed: 0,
            current_play: null,
            dispatch_count: 6,
            dispatch_share: 0.6,
          },
          {
            agent_id: "claude-b",
            agent_type: "claude_code",
            display_name: "Claude B",
            model_tier: "medium",
            status: "idle",
            context_size: 0,
            total_cost: 0,
            total_tokens: 0,
            tasks_completed: 2,
            tasks_failed: 0,
            current_play: null,
            dispatch_count: 4,
            dispatch_share: 0.4,
          },
        ],
      }),
    );

    await act(async () => {
      root.render(<SidePanelComponent />);
    });

    const badges = Array.from(
      container.querySelectorAll<HTMLElement>(".agent-dispatch-share"),
    );
    expect(badges).toHaveLength(2);
    // 0.6 → 60, 0.4 → 40
    const values = badges.map((el) =>
      el.getAttribute("data-agent-dispatch-share"),
    );
    expect(values).toContain("60");
    expect(values).toContain("40");
    // Visible label includes the percent sign.
    expect(badges[0].textContent).toMatch(/%$/);
  });

  it("falls back to 0% when dispatch_share is missing (older server)", async () => {
    notifySidePanelUpdate(
      stateUpdate({
        agents: [
          {
            agent_id: "legacy-agent",
            agent_type: "claude_code",
            display_name: "Legacy",
            status: "idle",
            context_size: 0,
            total_cost: 0,
            total_tokens: 0,
            tasks_completed: 0,
            tasks_failed: 0,
            current_play: null,
          },
        ],
      }),
    );

    await act(async () => {
      root.render(<SidePanelComponent />);
    });

    const badge = container.querySelector<HTMLElement>(".agent-dispatch-share");
    expect(badge).not.toBeNull();
    expect(badge?.getAttribute("data-agent-dispatch-share")).toBe("0");
  });
});
