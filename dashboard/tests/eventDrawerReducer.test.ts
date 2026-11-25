import { describe, expect, it } from "vitest";

import {
  INITIAL_STATE,
  reducer,
  type DrawerState,
} from "../src/components/EventDrawer";
import type { ActivePlay, AgentSnapshot, PlayEvent } from "../src/types";









function agent(overrides: Partial<AgentSnapshot> = {}): AgentSnapshot {
  return {
    agent_id: "163080aa-0000-0000-0000-000000000000",
    agent_type: "codex",
    display_name: "Cobalt Atlas",
    model_tier: "large",
    status: "busy",
    context_size: 0,
    total_cost: 0,
    total_tokens: 0,
    tasks_completed: 0,
    tasks_failed: 0,
    current_play: null,
    ...overrides,
  };
}

function activePlay(overrides: Partial<ActivePlay> = {}): ActivePlay {
  return {
    play_type: "code_review",
    play_id: 98,
    started_at: "2026-06-05T00:21:00Z",
    issue_number: null,
    pr_number: null,
    branch: null,
    ...overrides,
  };
}

function completedEvent(overrides: Partial<PlayEvent> = {}): PlayEvent {
  return {
    type: "play_event",
    status: "completed",
    play_type: "code_review",
    agent_id: "163080aa-0000-0000-0000-000000000000",
    success: true,
    duration_seconds: 12,
    dollar_cost: 0.1,
    token_cost: 0,
    artifacts: [],
    alignment_delta: 0,
    error: null,
    play_id: 98,
    skipped: false,
    skip_category: null,
    trigger_agent_id: null,
    trigger_agent_type: null,
    trigger_error_class: null,
    ...overrides,
  } as PlayEvent;
}

function onlyCard(state: DrawerState) {
  expect(state.cards).toHaveLength(1);
  return state.cards[0];
}

describe("EventDrawer reducer — terminated-agent identity preservation", () => {
  it("keeps name/type when the agent leaves the snapshot (state_update refresh path)", () => {
    let state = INITIAL_STATE;


    state = reducer(state, {
      type: "state_update",
      agents: [agent({ current_play: activePlay() })],
    });
    expect(onlyCard(state).name).toBe("Cobalt Atlas");
    expect(onlyCard(state).type).toBe("Large Codex");


    state = reducer(state, { type: "play_event", event: completedEvent() });



    state = reducer(state, { type: "state_update", agents: [] });

    const card = onlyCard(state);
    expect(card.status).toBe("completed");
    expect(card.name).toBe("Cobalt Atlas");
    expect(card.type).toBe("Large Codex");
  });

  it("keeps name/type when a completed event arrives after the agent is gone (upsert path)", () => {
    let state = INITIAL_STATE;

    state = reducer(state, {
      type: "state_update",
      agents: [agent({ current_play: activePlay() })],
    });

    state = reducer(state, { type: "state_update", agents: [] });
    state = reducer(state, { type: "play_event", event: completedEvent() });

    const card = onlyCard(state);
    expect(card.name).toBe("Cobalt Atlas");
    expect(card.type).toBe("Large Codex");
  });

  it("moves a running card out of Running when its agent leaves the snapshot", () => {
    let state = INITIAL_STATE;

    state = reducer(state, {
      type: "state_update",
      agents: [agent({ current_play: activePlay() })],
    });
    expect(onlyCard(state).status).toBe("started");

    state = reducer(state, { type: "state_update", agents: [] });

    const card = onlyCard(state);
    expect(card.status).toBe("failed");
    expect(card.result).toBe("Ended (agent removed)");
    expect(card.endedAt).not.toBeNull();
    expect(card.name).toBe("Cobalt Atlas");
    expect(card.type).toBe("Large Codex");
  });

  it("still upgrades name/type from the live snapshot while the agent exists", () => {
    let state = INITIAL_STATE;


    state = reducer(state, {
      type: "state_update",
      agents: [
        agent({ display_name: "Provisional", current_play: activePlay() }),
      ],
    });
    expect(onlyCard(state).name).toBe("Provisional");


    state = reducer(state, {
      type: "state_update",
      agents: [
        agent({ display_name: "Cobalt Atlas", current_play: activePlay() }),
      ],
    });
    expect(onlyCard(state).name).toBe("Cobalt Atlas");
    expect(onlyCard(state).type).toBe("Large Codex");
  });
});
