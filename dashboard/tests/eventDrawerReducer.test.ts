import { describe, expect, it } from "vitest";

import {
  INITIAL_STATE,
  reducer,
  type DrawerState,
} from "../src/components/EventDrawer";
import type { ActivePlay, AgentSnapshot, PlayEvent } from "../src/types";

// Regression: the left-panel activity tiles must keep the agent's display name
// and type after the agent terminates. A terminated agent drops out of the
// state snapshot's agent list; the reducer used to recompute name/type from the
// live agent map on every update, so a play's started->completed transition (or
// any subsequent state_update) clobbered "Cobalt Atlas / Large Codex" with the
// "163080aa / Agent" id-slice fallback. Completed-play tiles are historical
// records and must freeze the identity captured while the agent was live.

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

    // Agent live, running its play.
    state = reducer(state, {
      type: "state_update",
      agents: [agent({ current_play: activePlay() })],
    });
    expect(onlyCard(state).name).toBe("Cobalt Atlas");
    expect(onlyCard(state).type).toBe("Large Codex");

    // Play completes (agent still in the last snapshot's map).
    state = reducer(state, { type: "play_event", event: completedEvent() });

    // Agent is torn down -> absent from the next snapshot. The refresh block
    // must NOT reset the completed card to the id-slice/"Agent" fallback.
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
    // Agent terminates and leaves the snapshot before the completed event.
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

    // First snapshot has a provisional display name.
    state = reducer(state, {
      type: "state_update",
      agents: [
        agent({ display_name: "Provisional", current_play: activePlay() }),
      ],
    });
    expect(onlyCard(state).name).toBe("Provisional");

    // Updated snapshot (agent still present) must refresh to the new name.
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
