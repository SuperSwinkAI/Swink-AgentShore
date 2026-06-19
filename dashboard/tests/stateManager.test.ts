import { beforeEach, describe, expect, it } from "vitest";

import { __testHooks } from "../src/characters/stateMachine";
import { CharacterState } from "../src/characters/types";
import { ZoneId, getZone } from "../src/office/layout";
import { AgentShoreStateManager } from "../src/state";
import type {
  ActivePlayReplay,
  AgentSnapshot,
  AgentShoreMessage,
  PlayEventCompleted,
  PlayEventStarted,
  StateUpdate,
} from "../src/types";
import { makeActivePlay } from "../src/types";

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

function playCompleted(
  seq: number,
  overrides: Partial<PlayEventCompleted> = {},
): PlayEventCompleted {
  return {
    type: "play_event",
    status: "completed",
    seq,
    play_type: "issue_pickup",
    agent_id: "agent-1",
    success: true,
    duration_seconds: 60,
    dollar_cost: 0.1,
    token_cost: 100,
    artifacts: [],
    alignment_delta: 0,
    error: null,
    play_id: 1,
    skipped: false,
    skip_category: null,
    trigger_agent_id: null,
    trigger_agent_type: null,
    trigger_error_class: null,
    ...overrides,
  };
}

function playFailed(
  seq: number,
  overrides: Partial<PlayEventCompleted> = {},
): PlayEventCompleted {
  return playCompleted(seq, {
    status: "failed",
    success: false,
    error: "boom",
    ...overrides,
  });
}

function budgetUpdate(seq: number): AgentShoreMessage {
  return {
    type: "budget_update",
    seq,
    session_id: "s1",
    budget: {
      enabled: true,
      total_budget: 200,
      spent: 5,
      remaining: 195,
      estimated_cost_per_play: 0.5,
      time_enabled: true,
      time_total_minutes: 120,
      time_elapsed_minutes: 45,
      time_remaining_minutes: 75,
    },
  } as AgentShoreMessage;
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

  it("ignores a budget_update: no character churn (no sprite jitter)", () => {
    const mgr = new AgentShoreStateManager();
    // Busy agent mid-play so a character exists and has a routed target.
    mgr.handleMessage(stateUpdate(10, { agents: [agent({ status: "busy" })] }));
    mgr.handleMessage(playStarted(11));
    const char = mgr.characters.get("agent-1");
    expect(char).toBeDefined();
    const targetBefore = char?.targetState;
    const xBefore = char?.x;
    const yBefore = char?.y;
    const stateBefore = mgr.latestState;

    // The budget heartbeat must not touch the office StateManager at all: it
    // carries no agent data and returns false, leaving every character (and the
    // last full snapshot) exactly as they were.
    const accepted = mgr.handleMessage(budgetUpdate(12));
    expect(accepted).toBe(false);
    expect(mgr.characters.size).toBe(1);
    expect(mgr.characters.get("agent-1")).toBe(char);
    expect(char?.targetState).toBe(targetBefore);
    expect(char?.x).toBe(xBefore);
    expect(char?.y).toBe(yBefore);
    // latestState (the full snapshot) is untouched — budget lives on the
    // separate PlaysPanel consumer, not here.
    expect(mgr.latestState).toBe(stateBefore);
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
  it("preserves a newer play_event(started) across a stale idle/null state_update", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage(stateUpdate(1));
    mgr.handleMessage(playStarted(2));
    expect(mgr.latestState?.agents[0].status).toBe("busy");
    const char = mgr.getAgents()[0];
    const targetBefore = char.targetState;
    const activePlayBefore = mgr.latestState?.active_play;

    // The full snapshot can be stale relative to the append-only play event
    // stream; it must not visually clear the in-flight play.
    mgr.handleMessage(
      stateUpdate(3, {
        agents: [agent({ status: "idle", current_play: null })],
      }),
    );
    expect(mgr.latestState?.agents[0].status).toBe("busy");
    expect(mgr.latestState?.agents[0].current_play?.play_type).toBe(
      "issue_pickup",
    );
    expect(mgr.latestState?.active_play).toEqual(activePlayBefore);
    expect(mgr.getAgents()[0].bubble).toEqual({
      text: "Issue Pickup 100",
      tone: "work",
    });
    expect(mgr.getAgents()[0].targetState).toBe(targetBefore);
    expect(mgr.getAgents()[0].targetState).not.toBe(CharacterState.IDLE);
  });

  it("clears a completed play before a following idle/null state_update", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage(stateUpdate(1));
    mgr.handleMessage(playStarted(2));

    mgr.handleMessage(playCompleted(3));
    mgr.handleMessage(
      stateUpdate(4, {
        agents: [agent({ status: "idle", current_play: null })],
      }),
    );

    expect(mgr.latestState?.active_play).toBeNull();
    expect(mgr.latestState?.agents[0].status).toBe("idle");
    expect(mgr.latestState?.agents[0].current_play).toBeNull();
    expect(mgr.getAgents()[0].bubble).toBeNull();
    expect(mgr.getAgents()[0].targetState).toBe(CharacterState.IDLE);
  });

  it("clears a failed play before a following idle/null state_update", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage(stateUpdate(1));
    mgr.handleMessage(playStarted(2));

    mgr.handleMessage(playFailed(3));
    mgr.handleMessage(
      stateUpdate(4, {
        agents: [agent({ status: "idle", current_play: null })],
      }),
    );

    expect(mgr.latestState?.active_play).toBeNull();
    expect(mgr.latestState?.agents[0].status).toBe("idle");
    expect(mgr.latestState?.agents[0].current_play).toBeNull();
    expect(mgr.getAgents()[0].bubble).toBeNull();
    expect(mgr.getAgents()[0].targetState).toBe(CharacterState.IDLE);
  });

  it("uses a different non-null current_play from a state_update", () => {
    const mgr = new AgentShoreStateManager();
    const replacement = makeActivePlay({
      play_type: "code_review",
      agent_id: "agent-1",
      play_id: 2,
      pr_number: 55,
      started_at: "2026-01-01T00:01:00.000Z",
    });
    mgr.handleMessage(stateUpdate(1));
    mgr.handleMessage(playStarted(2));

    mgr.handleMessage(
      stateUpdate(3, {
        active_play: replacement,
        agents: [agent({ status: "busy", current_play: replacement })],
      }),
    );

    expect(mgr.latestState?.active_play).toEqual(replacement);
    expect(mgr.latestState?.agents[0].status).toBe("busy");
    expect(mgr.latestState?.agents[0].current_play).toEqual(replacement);
    expect(mgr.getAgents()[0].bubble).toEqual({
      text: "Code Review 55",
      tone: "work",
    });
  });

  it("does not preserve a prior play when the state_update terminates the agent", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage(stateUpdate(1));
    mgr.handleMessage(playStarted(2));

    mgr.handleMessage(
      stateUpdate(3, {
        agents: [agent({ status: "terminated", current_play: null })],
      }),
    );

    expect(mgr.latestState?.agents[0].status).toBe("terminated");
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

describe("AgentShoreStateManager — session reset (Tier 0)", () => {
  it("resetSession clears bootstrap progress and the seq floor", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage(stateUpdate(40));
    mgr.handleMessage({
      type: "bootstrap_phase",
      phase: "init_datastore",
      status: "started",
      elapsed_ms: 0,
    });
    expect(mgr.bootstrapPhase).toBe("init_datastore");

    mgr.resetSession();

    expect(mgr.bootstrapPhase).toBeNull();
    expect(mgr.bootstrapStartedAt).toBeNull();
    expect(mgr.latestState).toBeNull();
    // The de-dup floor is cleared, so a fresh low-seq frame is accepted.
    expect(mgr.handleMessage(stateUpdate(1))).toBe(true);
  });

  it("connection_restored drops the seq floor so a reconnect's fresh stream is accepted", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage(stateUpdate(20));
    // Reconnect may attach to a new orchestrator whose seq restarts at 1.
    mgr.handleMessage({ type: "connection_restored" } as AgentShoreMessage);
    expect(mgr.handleMessage(stateUpdate(1, { total_plays: 3 }))).toBe(true);
    expect(mgr.latestState?.total_plays).toBe(3);
  });

  it("session_ended drops the seq floor so the next run's seq=1 is accepted", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage(stateUpdate(20));
    mgr.handleMessage({ type: "session_ended" } as AgentShoreMessage);
    expect(mgr.handleMessage(stateUpdate(1))).toBe(true);
  });

  it("a stale high-seq frame can no longer suppress the next run's bootstrap modal", () => {
    const mgr = new AgentShoreStateManager();
    // The prior run left the de-dup floor high.
    mgr.handleMessage(stateUpdate(50));
    // Reconnect to the new orchestrator.
    mgr.handleMessage({ type: "connection_restored" } as AgentShoreMessage);
    // The new run's first bootstrap frame carries a low seq; it must be accepted
    // (before the fix, seq<=50 was silently dropped → modal never appeared).
    mgr.handleMessage({
      type: "bootstrap_phase",
      seq: 1,
      phase: "init_datastore",
      status: "started",
      elapsed_ms: 0,
    } as unknown as AgentShoreMessage);
    expect(mgr.bootstrapPhase).toBe("init_datastore");
  });
});

describe("AgentShoreStateManager — session boundary (Tier 1)", () => {
  it("a state_update with a new session_id resets seq + bootstrap and adopts it", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage(stateUpdate(10, { session_id: "A", total_plays: 5 }));
    mgr.handleMessage({
      type: "bootstrap_phase",
      phase: "init_datastore",
      status: "started",
      elapsed_ms: 0,
    });
    expect(mgr.currentSessionId).toBe("A");

    // Lower seq than the prior session, but a new id must still be accepted.
    const accepted = mgr.handleMessage(stateUpdate(1, { session_id: "B", total_plays: 2 }));
    expect(accepted).toBe(true);
    expect(mgr.currentSessionId).toBe("B");
    expect(mgr.bootstrapPhase).toBeNull();
    expect(mgr.latestState?.session_id).toBe("B");
    expect(mgr.latestState?.total_plays).toBe(2);
  });

  it("a session_id change on a non-state frame also triggers the boundary", () => {
    const mgr = new AgentShoreStateManager();
    mgr.handleMessage(stateUpdate(10, { session_id: "A" }));
    expect(mgr.latestState).not.toBeNull();

    // A play_event stamped with a new session_id flips the boundary (Tier 1
    // detects on any frame, not just state_update).
    mgr.handleMessage(playStarted(1, { session_id: "B" }));
    expect(mgr.currentSessionId).toBe("B");
    // resetSession wiped latestState; the play_event then no-ops (no snapshot).
    expect(mgr.latestState).toBeNull();
  });

  it("fires onSessionReset on a boundary but not on first adoption or same session", () => {
    const mgr = new AgentShoreStateManager();
    let resets = 0;
    mgr.onSessionReset = () => {
      resets += 1;
    };
    mgr.handleMessage(stateUpdate(1, { session_id: "A" }));
    expect(resets).toBe(0); // first adoption — nothing to reset
    mgr.handleMessage(stateUpdate(2, { session_id: "A" }));
    expect(resets).toBe(0); // same session
    mgr.handleMessage(stateUpdate(1, { session_id: "B" }));
    expect(resets).toBe(1); // boundary crossed
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
