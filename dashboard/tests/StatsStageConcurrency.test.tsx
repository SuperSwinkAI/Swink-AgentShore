import React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { AGENT_REGISTRY } from "../src/agentRegistry";
import StatsStage, {
  appendConcurrencySample,
  buildConcurrencyChartModel,
  colorForConcurrencyAgentType,
  CONCURRENCY_MAX_WINDOW_MS,
  deriveBusyAgentCounts,
  formatConcurrencyWindowDuration,
  notifyStatsStageUpdate,
  notifyStatsStageVisible,
  orderConcurrencyAgentTypes,
  pruneConcurrencySamples,
  resolveConcurrencyWindowMs,
  resetStatsStageForTests,
  type ConcurrencySample,
} from "../src/components/StatsStage";
import type { AgentSnapshot, StateUpdate } from "../src/types";

function agent(
  agentId: string,
  agentType: string,
  status: AgentSnapshot["status"],
): AgentSnapshot {
  return {
    agent_id: agentId,
    agent_type: agentType as AgentSnapshot["agent_type"],
    status,
    context_size: 0,
    total_cost: 0,
    total_tokens: 0,
    tasks_completed: 0,
    tasks_failed: 0,
    current_play: null,
  };
}

function state(
  sessionId: string,
  timestampMs: number,
  agents: AgentSnapshot[],
): StateUpdate {
  return {
    type: "state_update",
    session_id: sessionId,
    session_state: "running",
    policy_mode: "learning",
    total_plays: 0,
    total_cost: 0,
    agents,
    open_issues: [],
    pull_requests: [],
    budget: null,
    trajectory: null,
    active_play: null,
    same_type_failure_streak: 0,
    last_play_type: null,
    forced_mask_zeros: [],
    action_mask: [],
    mask_reasons: {},
    graph: null,
    timestamp: new Date(timestampMs).toISOString(),
  };
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  resetStatsStageForTests();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  resetStatsStageForTests();
});

describe("StatsStage fleet concurrency helpers", () => {
  it("groups only busy agents by agent type", () => {
    expect(
      deriveBusyAgentCounts([
        agent("a1", "claude_code", "busy"),
        agent("a2", "claude_code", "idle"),
        agent("a3", "codex", "busy"),
        agent("a4", "grok", "error"),
        agent("a5", "codex", "terminated"),
      ]),
    ).toEqual({ claude_code: 1, codex: 1 });
  });

  it("prunes samples outside the three hour cap", () => {
    const now = 5 * 60 * 60 * 1000;
    const samples: ConcurrencySample[] = [
      { timestampMs: now - CONCURRENCY_MAX_WINDOW_MS - 1, counts: { codex: 1 } },
      { timestampMs: now - CONCURRENCY_MAX_WINDOW_MS, counts: { codex: 2 } },
      { timestampMs: now, counts: { codex: 3 } },
    ];

    expect(pruneConcurrencySamples(samples, now)).toEqual(samples.slice(1));
  });

  it("deduplicates unchanged samples in the same bucket", () => {
    const samples = appendConcurrencySample(
      [{ timestampMs: 10_000, counts: { codex: 1 } }],
      { timestampMs: 10_250, counts: { codex: 1 } },
      10_250,
      CONCURRENCY_MAX_WINDOW_MS,
      1000,
    );

    expect(samples).toHaveLength(1);
    expect(samples[0].timestampMs).toBe(10_000);
  });

  it("orders known agent types by registry, then unknown types alphabetically", () => {
    expect(
      orderConcurrencyAgentTypes([
        { timestampMs: 0, counts: { zebra: 1, codex: 1, aardvark: 1 } },
        { timestampMs: 1, counts: { claude_code: 1 } },
      ]),
    ).toEqual(["claude_code", "codex", "aardvark", "zebra"]);
  });

  it("uses registry colors for known types and deterministic fallback colors for unknowns", () => {
    expect(colorForConcurrencyAgentType("codex")).toBe(AGENT_REGISTRY.codex.colorFill);
    expect(colorForConcurrencyAgentType("unknown-agent")).toBe(
      colorForConcurrencyAgentType("unknown-agent"),
    );
  });

  it("builds stacked totals from grouped samples", () => {
    const model = buildConcurrencyChartModel(
      [
        { timestampMs: 0, counts: { claude_code: 1, codex: 1 } },
        { timestampMs: 60_000, counts: { claude_code: 2, codex: 1 } },
      ],
      60_000,
    );

    expect(model.peakTotal).toBe(3);
    expect(model.currentTotal).toBe(3);
    expect(model.series).toHaveLength(2);
    expect(model.totalLinePoints).toHaveLength(2);
    expect(model.series[1].points[1].value).toBe(1);
  });

  it("uses the full shorter session span for the concurrency window", () => {
    const now = 2 * 60 * 60 * 1000;
    const samples: ConcurrencySample[] = [
      { timestampMs: now - 90 * 60 * 1000, counts: { claude_code: 1 } },
      { timestampMs: now, counts: { claude_code: 2 } },
    ];

    const model = buildConcurrencyChartModel(samples, now);

    expect(resolveConcurrencyWindowMs(samples, now)).toBe(90 * 60 * 1000);
    expect(model.windowStartMs).toBe(now - 90 * 60 * 1000);
    expect(model.totalLinePoints).toHaveLength(2);
    expect(formatConcurrencyWindowDuration(model.windowDurationMs)).toBe("1h 30m");
  });

  it("caps the concurrency window at three hours for longer sessions", () => {
    const now = 5 * 60 * 60 * 1000;
    const samples: ConcurrencySample[] = [
      { timestampMs: now - 4 * 60 * 60 * 1000, counts: { claude_code: 1 } },
      { timestampMs: now - 2 * 60 * 60 * 1000, counts: { claude_code: 2 } },
      { timestampMs: now, counts: { claude_code: 3 } },
    ];

    const model = buildConcurrencyChartModel(samples, now);

    expect(model.windowDurationMs).toBe(CONCURRENCY_MAX_WINDOW_MS);
    expect(model.windowStartMs).toBe(now - CONCURRENCY_MAX_WINDOW_MS);
    expect(model.totalLinePoints).toHaveLength(2);
    expect(formatConcurrencyWindowDuration(model.windowDurationMs)).toBe("3h");
  });
});

describe("StatsStage fleet concurrency rendering", () => {
  it("renders Fleet Concurrency after Agents with visible series", () => {
    const now = Date.now();
    act(() => {
      root.render(<StatsStage />);
    });
    act(() => {
      notifyStatsStageVisible(true);
      notifyStatsStageUpdate(
        state("session-a", now - 60_000, [
          agent("a1", "claude_code", "busy"),
          agent("a2", "codex", "idle"),
        ]),
      );
      notifyStatsStageUpdate(
        state("session-a", now, [
          agent("a1", "claude_code", "busy"),
          agent("a2", "codex", "busy"),
        ]),
      );
    });

    const headings = [...container.querySelectorAll(".stats-section h2")].map(
      (heading) => heading.textContent,
    );
    expect(headings.indexOf("Agents")).toBeGreaterThan(-1);
    expect(headings.indexOf("Fleet Concurrency")).toBeGreaterThan(
      headings.indexOf("Agents"),
    );
    expect(container.querySelector(".stats-concurrency-svg")).not.toBeNull();
    expect(container.querySelectorAll(".stats-concurrency-band")).toHaveLength(2);
    expect(container.textContent).toContain("Claude Code busy");
    expect(container.textContent).toContain("Codex CLI busy");
    expect(container.textContent).toContain("rolling 1m window");
  });

  it("resets accumulated history when the session changes", () => {
    const now = Date.now();
    act(() => {
      root.render(<StatsStage />);
    });
    act(() => {
      notifyStatsStageVisible(true);
      notifyStatsStageUpdate(
        state("session-a", now - 60_000, [agent("a1", "claude_code", "busy")]),
      );
      notifyStatsStageUpdate(
        state("session-a", now, [agent("a1", "codex", "busy")]),
      );
    });
    expect(container.querySelector(".stats-concurrency-svg")).not.toBeNull();

    act(() => {
      notifyStatsStageUpdate(
        state("session-b", now + 60_000, [agent("b1", "codex", "busy")]),
      );
    });

    expect(container.querySelector(".stats-concurrency-svg")).toBeNull();
    expect(container.textContent).toContain("Waiting for fleet history");
  });
});
