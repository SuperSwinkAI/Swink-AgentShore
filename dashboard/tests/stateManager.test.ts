import { beforeEach, describe, expect, it } from "vitest";

import { __testHooks } from "../src/characters/stateMachine";
import { CharacterState } from "../src/characters/types";
import { ZoneId, getZone } from "../src/office/layout";
import { AgentShoreStateManager } from "../src/state";
import type {
  ActivePlayReplay,
  AgentSnapshot,
  AgentShoreMessage,
  PlayEventStarted,
  StateUpdate,
} from "../src/types";

function agent(overrides: Partial<AgentSnapshot> = {}): AgentSnapshot {
  return {
    agent_id: "agent-1",
    agent_type: "claude_code",
    status: "idle",
    context_size: 0,
    total_cost: 0,
    total_tokens: 0,
    tasks_completed: 0,
    tasks_failed: 0,
    current_play: null,
    ...overrides,
  };
}

function stateUpdate(
  seq: number,
  overrides: Partial<StateUpdate> = {},
): StateUpdate {
  return {
    type: "state_update",
    seq,
    session_id: "s1",
    session_state: "running",
    policy_mode: "learning",
    total_plays: 0,
    total_cost: 0,
    agents: [agent()],
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

function playStarted(
  seq: number,
  overrides: Partial<PlayEventStarted> = {},
): PlayEventStarted {
  return {
    type: "play_event",
    status: "started",
    seq,
    play_type: "issue_pickup",
    agent_id: "agent-1",
    play_id: 1,
    started_at: "2026-01-01T00:00:00.000Z",
    issue_number: 100,
    pr_number: null,
    branch: null,
    trigger_agent_id: null,
    trigger_agent_type: null,
    trigger_error_class: null,
    ...overrides,
  };
}

function activePlayReplay(
  overrides: Partial<ActivePlayReplay> = {},
): ActivePlayReplay {
  return {
    type: "active_play_replay",
    active_play: {
      play_type: "issue_pickup",
      agent_id: "agent-1",
      play_id: 10,
      started_at: "2026-01-01T00:00:00.000Z",
      issue_number: 100,
      pr_number: null,
      branch: null,
    },
    ...overrides,
  };
}

describe("AgentShoreStateManager — seq-based stale-drop", () => {
  it("accepts the first state_update and records latestState", () => {
    const mgr = new AgentShoreStateManager();
    const accepted = mgr.handleMessage(stateUpdate(1));
    expect(accepted).toBe(true);
    expect(mgr.latestState?.session_id).toBe("s1");
  });

  it("drops a duplicate seq with no state mutation", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage(stateUpdate(5, { total_plays: 3 }));
    const before = mgr.latestState;

    const accepted = mgr.handleMessage(stateUpdate(5, { total_plays: 9 }));
    expect(accepted).toBe(false);
    expect(mgr.latestState).toBe(before);
    expect(mgr.latestState?.total_plays).toBe(3);
  });

  it("drops a strictly-older seq even if the payload would otherwise mutate state", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage(stateUpdate(10, { total_plays: 7 }));

    const accepted = mgr.handleMessage(stateUpdate(2, { total_plays: 99 }));
    expect(accepted).toBe(false);
    expect(mgr.latestState?.total_plays).toBe(7);
  });

  it("drops out-of-order play_event arriving after a higher-seq state_update", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage(stateUpdate(10));

    const accepted = mgr.handleMessage(playStarted(8));
    expect(accepted).toBe(false);
    // The agent should still be idle — patchStartedPlay was never run.
    expect(mgr.latestState?.agents[0].status).toBe("idle");
    expect(mgr.latestState?.agents[0].current_play).toBeNull();
  });

  it("accepts a play_event with strictly-higher seq and patches latestState", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage(stateUpdate(10));

    const accepted = mgr.handleMessage(playStarted(11));
    expect(accepted).toBe(true);
    const updatedAgent = mgr.latestState?.agents[0];
    expect(updatedAgent?.status).toBe("busy");
    expect(updatedAgent?.current_play?.play_type).toBe("issue_pickup");
    expect(updatedAgent?.current_play?.issue_number).toBe(100);
  });

  it("shows play and target context in the active agent bubble", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage(stateUpdate(10));

    mgr.handleMessage(playStarted(11));

    expect(mgr.getAgents()[0].bubble).toEqual({
      text: "Issue Pickup 100",
      tone: "work",
    });
  });

  it("does not advance lastSeenSeq when a message has no seq (synthetic events)", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage(stateUpdate(5));

    // Synthetic, no seq.
    const syntheticConnLost: AgentShoreMessage = {
      type: "connection_lost",
    } as AgentShoreMessage;
    mgr.handleMessage(syntheticConnLost);
    expect(mgr.connected).toBe(false);

    // A subsequent real message at seq=6 should still be accepted.
    const accepted = mgr.handleMessage(stateUpdate(6, { total_plays: 11 }));
    expect(accepted).toBe(true);
    expect(mgr.latestState?.total_plays).toBe(11);
  });
});

describe("AgentShoreStateManager — state_update is authoritative", () => {
  it("a play_event(started) followed by state_update with no current_play leaves the agent idle", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage(stateUpdate(1));
    mgr.handleMessage(playStarted(2));
    expect(mgr.latestState?.agents[0].status).toBe("busy");

    // Authoritative snapshot says the agent is idle — even though play_event
    // had said it was busy. The patched optimistic state must be overwritten.
    mgr.handleMessage(
      stateUpdate(3, {
        agents: [agent({ status: "idle", current_play: null })],
      }),
    );
    expect(mgr.latestState?.agents[0].status).toBe("idle");
    expect(mgr.latestState?.agents[0].current_play).toBeNull();
    expect(mgr.getAgents()[0].bubble).toBeNull();
  });

  it("active_play_replay patches current play into the latest snapshot", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage(stateUpdate(1));

    const accepted = mgr.handleMessage(activePlayReplay());

    expect(accepted).toBe(true);
    expect(mgr.latestState?.active_play?.play_type).toBe("issue_pickup");
    expect(mgr.latestState?.agents[0].current_play?.play_id).toBe(10);
    expect(mgr.latestState?.agents[0].current_play?.issue_number).toBe(100);
  });
});

describe("AgentShoreStateManager — bootstrap_phase events (desktop-afp)", () => {
  it("sets bootstrapPhase on the first started event and stamps started time", () => {
    const mgr = new AgentShoreStateManager();
    expect(mgr.bootstrapPhase).toBeNull();
    expect(mgr.bootstrapStartedAt).toBeNull();

    mgr.handleMessage({
      type: "bootstrap_phase",
      phase: "init_ppo_selector",
      status: "started",
      elapsed_ms: 0,
    } satisfies AgentShoreMessage);

    expect(mgr.bootstrapPhase).toBe("init_ppo_selector");
    expect(typeof mgr.bootstrapStartedAt).toBe("number");
  });

  it("keeps started time stable across multiple phase transitions", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage({
      type: "bootstrap_phase",
      phase: "init_datastore",
      status: "started",
      elapsed_ms: 0,
    });
    const initialStart = mgr.bootstrapStartedAt;
    mgr.handleMessage({
      type: "bootstrap_phase",
      phase: "fetch_issues",
      status: "started",
      elapsed_ms: 0,
    });
    expect(mgr.bootstrapStartedAt).toBe(initialStart);
    expect(mgr.bootstrapPhase).toBe("fetch_issues");
  });

  it("clears bootstrap state when ready/completed arrives", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage({
      type: "bootstrap_phase",
      phase: "init_datastore",
      status: "started",
      elapsed_ms: 0,
    });
    mgr.handleMessage({
      type: "bootstrap_phase",
      phase: "ready",
      status: "completed",
      elapsed_ms: 0,
    });

    expect(mgr.bootstrapPhase).toBeNull();
    expect(mgr.bootstrapStartedAt).toBeNull();
  });
});

describe("AgentShoreStateManager — take_break routing (desktop-o9z1)", () => {
  beforeEach(() => {
    // Seat reservations live in module-scope state; clear before each test so
    // tests are independent and the spawn at Front Desk doesn't run out of
    // seats across many tests.
    __testHooks.clearOccupiedSeats();
  });

  function tileInZone(
    tile: { x: number; y: number } | undefined,
    zoneId: ZoneId,
  ): boolean {
    if (!tile) return false;
    const { bounds } = getZone(zoneId);
    return (
      tile.x >= bounds.x &&
      tile.x < bounds.x + bounds.w &&
      tile.y >= bounds.y &&
      tile.y < bounds.y + bounds.h
    );
  }

  it("routes take_break with no trigger to the Recovery Bay", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage(stateUpdate(1));

    // The character spawned at the Front Desk on the initial state_update.
    // A take_break play_event is a cooldown play, even without trigger fields,
    // so it routes to Recovery Bay instead of the idle Zen Garden.
    const accepted = mgr.handleMessage(
      playStarted(2, {
        play_type: "take_break",
        trigger_error_class: null,
        trigger_agent_id: null,
      }),
    );
    expect(accepted).toBe(true);

    const char = mgr
      .getCharacters()
      .find((c) => c.agentId === "agent-1");
    expect(char).toBeDefined();
    // sendToRecovery sets targetState=IDLE.
    expect(char!.targetState).toBe(CharacterState.IDLE);
    // Last tile on the path must be inside Recovery Bay bounds.
    const last = char!.path[char!.path.length - 1];
    expect(tileInZone(last, ZoneId.RECOVERY_BAY)).toBe(true);
    expect(tileInZone(last, ZoneId.ZEN_GARDEN)).toBe(false);
  });

  it("routes take_break with trigger_error_class to the Recovery Bay", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage(stateUpdate(1));

    const accepted = mgr.handleMessage(
      playStarted(2, {
        play_type: "take_break",
        trigger_error_class: "unknown",
        trigger_agent_id: null,
      }),
    );
    expect(accepted).toBe(true);

    const char = mgr
      .getCharacters()
      .find((c) => c.agentId === "agent-1");
    expect(char).toBeDefined();
    // sendToRecovery sets targetState=IDLE.
    expect(char!.targetState).toBe(CharacterState.IDLE);
    const last = char!.path[char!.path.length - 1];
    expect(tileInZone(last, ZoneId.RECOVERY_BAY)).toBe(true);
    expect(tileInZone(last, ZoneId.ZEN_GARDEN)).toBe(false);
  });

  it("routes take_break with trigger_agent_id (but no error class) to the Recovery Bay", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage(stateUpdate(1));

    const accepted = mgr.handleMessage(
      playStarted(2, {
        play_type: "take_break",
        trigger_error_class: null,
        trigger_agent_id: "agent-7",
      }),
    );
    expect(accepted).toBe(true);

    const char = mgr
      .getCharacters()
      .find((c) => c.agentId === "agent-1");
    expect(char).toBeDefined();
    expect(char!.targetState).toBe(CharacterState.IDLE);
    const last = char!.path[char!.path.length - 1];
    expect(tileInZone(last, ZoneId.RECOVERY_BAY)).toBe(true);
  });
});
