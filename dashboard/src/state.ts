import type {
  ActivePlay,
  AgentStatus,
  AgentShoreMessage,
  StateUpdate,
  PlayEvent,
  PlayType,
  AgentSnapshot,
} from "./types";
import { makeActivePlay } from "./types";
import { displayAgentName, formatPlayWithTarget } from "./format";
import {
  CURRENT_LOCATION_PLAY_TYPES,
  FRONT_DESK_EXIT_PLAY_TYPES,
  PLAY_TO_ZONE,
  RECOVERY_PLAY_TYPES,
} from "./office/zones";
import {
  type Character,
  CharacterState,
  NpcKind,
  normalizeAgentModelTier,
  type NpcDefinition,
} from "./characters/types";
import {
  spawnCharacter,
  spawnNpcCharacter,
  assignToZone,
  returnToIdle,
  sendToRecovery,
  sendToFrontDeskExit,
  showBubble,
} from "./characters/stateMachine";

const NPCS: NpcDefinition[] = [
  {
    id: "npc-fezzik",
    name: "Fezzik",
    kind: NpcKind.MASTIFF,
    scale: 2,
    startTile: { x: 36, y: 47 },
  },
  {
    id: "npc-missy",
    name: "Missy",
    kind: NpcKind.GERMAN_SHEPHERD,
    scale: 1.12,
    startTile: { x: 50, y: 48 },
  },
  {
    id: "npc-chloe",
    name: "Chloe",
    kind: NpcKind.RUSSIAN_BLUE_CAT,
    scale: 0.84,
    startTile: { x: 47, y: 51 },
  },
];

function displayStatusForAgent(agent: AgentSnapshot): AgentStatus {
  if (agent.current_play && agent.status === "idle") return "busy";
  return agent.status;
}

/** Minimal play-identity used by the same-play predicates below. */
interface PlayIdentity {
  play_id: number | null;
  agent_id?: string | null;
  play_type: PlayType;
}

/**
 * Null-tolerant "is this the same play instance?" predicate, used when
 * reconciling a per-agent play against the canonical ``active_play``.
 * Matches when the agent and play_type agree and the play_ids are either
 * equal or one side is unknown (null). Centralised so the three
 * reconciliation sites can't drift to subtly different rules.
 */
function samePlay(a: PlayIdentity, b: PlayIdentity): boolean {
  return (
    a.agent_id === b.agent_id &&
    a.play_type === b.play_type &&
    (a.play_id === null || b.play_id === null || a.play_id === b.play_id)
  );
}

/**
 * Permissive "does this completion event clear the active play?" predicate.
 * A completed/failed event clears the global ``active_play`` when it matches by
 * play_id, by agent_id, or — for agent-less plays — by play_type. Kept distinct
 * from {@link samePlay}: completion clearing is intentionally lenient so a
 * lost/renumbered started event never wedges a stale running card.
 */
function isCompletionForActivePlay(
  active: ActivePlay,
  msg: { play_id: number | null; agent_id: string | null; play_type: PlayType },
): boolean {
  return (
    active.play_id === msg.play_id ||
    active.agent_id === msg.agent_id ||
    (msg.agent_id === null && active.play_type === msg.play_type)
  );
}

export class AgentShoreStateManager {
  private characters = new Map<string, Character>();
  private previousAgents = new Map<string, AgentSnapshot>();
  private npcs = NPCS.map((npc) => spawnNpcCharacter(npc));
  private lastSeenSeq = 0;

  latestState: StateUpdate | null = null;
  feedbackPending: string | null = null;
  connected = true;
  sessionEnded = false;

  // The session_id this manager is bound to. With session_id stamped on every
  // frame (Tier 1), any frame naming a different id marks a new orchestrator
  // run and triggers a full reset. Null until the first id-bearing frame.
  currentSessionId: string | null = null;
  // Host hook (set by Dashboard) to run component-level resets — play bar,
  // event drawer, agent stats, bootstrap modal — when the manager crosses a
  // session boundary. Invoked from handleMessage before the new frame is
  // processed, so the UI is cleared before the new session repopulates it.
  onSessionReset: (() => void) | null = null;

  // Bootstrap progress (desktop-afp). bootstrapPhase is the currently-running
  // step name, or null once bootstrap has emitted its final ("ready", "completed").
  // bootstrapStartedAt is wall-clock ms when the first bootstrap_phase event
  // arrived, used by the modal to render an elapsed counter.
  bootstrapPhase: string | null = null;
  bootstrapStartedAt: number | null = null;

  clearFeedbackPending(): void {
    this.feedbackPending = null;
  }

  /**
   * Reconcile bootstrap progress against live traffic (#361). The backend's
   * ("ready", "completed") sentinel is published fire-and-forget under
   * suppress(Exception), so it can be lost — leaving the modal pinned forever.
   * A state_update or play_event is conclusive proof bootstrap finished (the
   * same signal the desktop's SessionStartingOverlay already uses), so treat
   * either as a second way to clear it.
   */
  clearBootstrapProgress(): void {
    this.bootstrapPhase = null;
    this.bootstrapStartedAt = null;
  }

  /**
   * Wipe per-session state: agent characters, previous-agents map, the
   * latest state snapshot, seq counter, and bootstrap progress. Called
   * when the transport sees a new session_id and the previous session
   * never sent a clean session_ended (e.g. Tauri shell was killed).
   * Only NPCs are preserved across sessions — they are properties of the
   * office, not of any one orchestrator run. Bootstrap is per-run: each
   * orchestrator re-runs it and must be able to re-show the modal, so it
   * is reset here too.
   */
  resetSession(): void {
    this.characters.clear();
    this.previousAgents.clear();
    this.latestState = null;
    this.feedbackPending = null;
    this.sessionEnded = false;
    // Reset the seq de-dup floor: the new session's bridge restarts at
    // seq=1, so a stale-high lastSeenSeq from the prior run would drop the
    // new session's low-seq frames (the missing-bootstrap-modal bug).
    this.lastSeenSeq = 0;
    this.bootstrapPhase = null;
    this.bootstrapStartedAt = null;
  }

  getCharacters(): Character[] {
    return [...this.characters.values(), ...this.npcs];
  }

  getAgents(): Character[] {
    return [...this.characters.values()];
  }

  revealCharactersForStaticRender(): void {
    for (const char of this.characters.values()) {
      if (!char.despawning) {
        char.opacity = 1;
      }
    }
  }

  handleMessage(msg: AgentShoreMessage): boolean {
    // Session boundary (Tier 1): with session_id stamped on every frame, a new
    // id means a new orchestrator run. Reset everything (incl. seq + bootstrap,
    // via resetSession) before processing so the new run's low-seq frames are
    // accepted and the modal re-shows. The first id-bearing frame of a fresh
    // mount only adopts the id — there is nothing to reset yet.
    const sid = (msg as { session_id?: unknown }).session_id;
    if (typeof sid === "string" && sid !== this.currentSessionId) {
      if (this.currentSessionId !== null) {
        this.resetSession();
        this.onSessionReset?.();
      }
      this.currentSessionId = sid;
    }

    // Drop out-of-order or replayed messages using the monotonic seq number.
    // Not all message types carry seq (ConnectionLost/Restored are client-synthetic).
    if ("seq" in msg && typeof msg.seq === "number") {
      if (msg.seq <= this.lastSeenSeq) return false;
      this.lastSeenSeq = msg.seq;
    }

    switch (msg.type) {
      case "state_update":
        this.clearBootstrapProgress();
        this.handleStateUpdate(msg);
        return true;
      case "play_event":
        this.clearBootstrapProgress();
        this.handlePlayEvent(msg);
        return true;
      case "feedback_requested":
        this.feedbackPending = msg.reason;
        this.markBusyAgentsForFeedback();
        return false;
      case "bootstrap_phase":
        if (msg.phase === "ready" && msg.status === "completed") {
          this.bootstrapPhase = null;
          this.bootstrapStartedAt = null;
        } else {
          if (this.bootstrapStartedAt === null) {
            this.bootstrapStartedAt = Date.now();
          }
          if (msg.status === "started") {
            this.bootstrapPhase = msg.phase;
          }
        }
        return false;
      case "session_ended":
        this.sessionEnded = true;
        // A surviving manager (the instance persists across reconnects)
        // must accept the next run's seq=1 frames, so drop the de-dup floor.
        this.lastSeenSeq = 0;
        return false;
      case "connection_lost":
        this.connected = false;
        return false;
      case "connection_restored":
        this.connected = true;
        this.sessionEnded = false;
        // A reconnect may attach to a new orchestrator whose seq restarts at
        // 1; the bridge only replays the current session, so clearing the
        // floor here is safe and avoids dropping the fresh stream.
        this.lastSeenSeq = 0;
        return false;
      case "active_play_replay":
        this.patchActivePlayReplay(msg.active_play);
        if (msg.active_play?.agent_id) {
          const char = this.characters.get(msg.active_play.agent_id);
          if (char) {
            this.routePlay(char, msg.active_play);
          }
        }
        return this.latestState !== null;
      case "agent_changed":
        this.patchAgentStatus(msg.agent_id, msg.status);
        if (msg.agent_id) {
          const char = this.characters.get(msg.agent_id);
          if (char) {
            char.status = msg.status;
            if (msg.status === "busy") {
              // Route from canonical state — current_play is set by patchStartedPlay
              // when the paired play_event(started) arrives.
              const agent = this.latestState?.agents.find(
                (a) => a.agent_id === msg.agent_id,
              );
              const currentPlay = agent
                ? this.currentPlayForAgent(agent)
                : null;
              if (currentPlay) {
                this.routePlay(char, currentPlay);
              }
            } else if (msg.status === "idle") {
              this.clearActivePlay(char);
              if (
                char.targetState !== CharacterState.IDLE &&
                !char.despawnOnArrival
              ) {
                returnToIdle(char);
              }
            } else if (msg.status === "error") {
              const agent = this.latestState?.agents.find(
                (a) => a.agent_id === msg.agent_id,
              );
              const currentPlay = agent
                ? this.currentPlayForAgent(agent)
                : null;
              if (currentPlay) {
                this.routePlay(char, currentPlay);
              } else {
                this.clearActivePlay(char);
                sendToRecovery(char);
              }
              showBubble(char, "error");
            }
          }
        }
        return true;
    }
    return false;
  }

  private handleStateUpdate(msg: StateUpdate): void {
    const merged = this.mergeStateUpdate(msg);
    this.latestState = merged;
    const currentAgents = new Map(merged.agents.map((a) => [a.agent_id, a]));

    // Spawn new agents
    for (const [id, agent] of currentAgents) {
      if (!this.characters.has(id)) {
        const char = spawnCharacter(
          id,
          agent.agent_type,
          normalizeAgentModelTier(agent.model_tier),
        );
        char.displayName = displayAgentName(agent);
        char.status = agent.status;
        this.characters.set(id, char);
        if (agent.status === "idle") {
          returnToIdle(char);
        } else if (agent.status === "error") {
          sendToRecovery(char);
        }
      }
    }

    // Mark agents no longer present for fade-out.
    for (const [id, char] of this.characters) {
      if (
        !currentAgents.has(id) &&
        !char.despawning &&
        !char.despawnOnArrival
      ) {
        sendToFrontDeskExit(char);
      }
    }

    // Diff statuses (workaround for issue #5)
    for (const [id, agent] of currentAgents) {
      const prev = this.previousAgents.get(id);
      const char = this.characters.get(id);
      if (!char) continue;

      const displayStatus = displayStatusForAgent(agent);
      const previousDisplayStatus = prev ? displayStatusForAgent(prev) : null;

      char.status = displayStatus;
      char.displayName = displayAgentName(agent);
      char.modelTier = normalizeAgentModelTier(agent.model_tier);
      const currentPlay = this.currentPlayForAgent(agent);
      if (agent.status === "error") {
        showBubble(char, "error");
      }

      if (displayStatus === "error") {
        if (currentPlay) {
          this.routePlay(char, currentPlay);
        } else {
          this.clearActivePlay(char);
          sendToRecovery(char);
        }
      } else if (currentPlay && agent.status !== "terminated") {
        this.routePlay(char, currentPlay);
      } else if (displayStatus === "idle" || displayStatus === "terminated") {
        this.clearActivePlay(char);
      }

      if (prev && previousDisplayStatus !== displayStatus) {
        // When status flips to idle, route to Zen Garden unless the character
        // is already heading there (targetState=IDLE) or being despawned.
        // The previous guard required char.state === WORK, which left agents
        // stranded in workshop seats when the play_event(completed) was lost
        // or when state_update outpaced it mid-walk.
        if (
          displayStatus === "idle" &&
          char.targetState !== CharacterState.IDLE &&
          !char.despawnOnArrival
        ) {
          returnToIdle(char);
        }
        if (
          displayStatus === "error" &&
          !currentPlay &&
          !char.despawnOnArrival
        ) {
          sendToRecovery(char);
        }
        if (
          displayStatus === "terminated" &&
          !char.despawning &&
          !char.despawnOnArrival
        ) {
          sendToFrontDeskExit(char);
        }
      }
    }

    this.previousAgents = currentAgents;
  }

  private mergeStateUpdate(msg: StateUpdate): StateUpdate {
    const previous = this.latestState;
    if (!previous) return msg;

    let preservedActivePlay = false;
    let changed = false;
    const agents = msg.agents.map((agent) => {
      if (agent.current_play) return agent;
      if (agent.status === "terminated" || agent.status === "error") return agent;

      if (msg.active_play?.agent_id === agent.agent_id) {
        changed = true;
        return {
          ...agent,
          status: "busy" as AgentStatus,
          current_play: msg.active_play,
        };
      }

      const previousPlay = this.previousInFlightPlayForAgent(previous, agent.agent_id);
      if (msg.active_play === null && previousPlay) {
        preservedActivePlay = true;
        changed = true;
        return {
          ...agent,
          status: "busy" as AgentStatus,
          current_play: previousPlay,
        };
      }

      return agent;
    });

    if (!preservedActivePlay) {
      return changed ? { ...msg, agents } : msg;
    }

    return {
      ...msg,
      active_play: previous.active_play,
      agents,
    };
  }

  private previousInFlightPlayForAgent(
    previous: StateUpdate,
    agentId: string,
  ): ActivePlay | null {
    const active = previous.active_play;
    if (!active || active.agent_id !== agentId) return null;

    const agent = previous.agents.find((candidate) => candidate.agent_id === agentId);
    const currentPlay = agent?.current_play;
    if (!currentPlay) return null;

    return samePlay(active, { ...currentPlay, agent_id: agentId })
      ? currentPlay
      : null;
  }

  cleanupDespawned(): void {
    for (const [id, char] of this.characters) {
      if (char.despawning && char.opacity <= 0) {
        this.characters.delete(id);
      }
    }
  }

  private handlePlayEvent(msg: PlayEvent): void {
    if (msg.status === "started") {
      this.patchStartedPlay(msg);
      if (msg.agent_id) {
        const char = this.characters.get(msg.agent_id);
        // Route from canonical state after patching — latestState now has the
        // current_play set for this agent.
        const agent = this.latestState?.agents.find(
          (a) => a.agent_id === msg.agent_id,
        );
        const currentPlay = agent ? this.currentPlayForAgent(agent) : null;
        if (char && currentPlay) {
          this.routePlay(char, currentPlay);
        }
      }
    }

    if (msg.status === "completed" || msg.status === "failed") {
      this.patchCompletedPlay(msg);
      if (msg.agent_id) {
        const char = this.characters.get(msg.agent_id);
        if (char) {
          if (msg.status === "failed") {
            this.clearActivePlay(char);
            sendToRecovery(char);
            showBubble(char, "fail", 5000);
            return;
          }
          if (FRONT_DESK_EXIT_PLAY_TYPES.has(msg.play_type)) return;
          if (CURRENT_LOCATION_PLAY_TYPES.has(msg.play_type)) {
            this.clearActivePlay(char);
            showBubble(
              char,
              msg.status === "completed" ? "success" : "fail",
              msg.status === "completed" ? 3000 : 5000,
            );
            return;
          }
          // Seq-based filtering guarantees this message is not stale, so we
          // unconditionally route from the canonical patched state.
          const agent = this.latestState?.agents.find(
            (a) => a.agent_id === msg.agent_id,
          );
          if (agent?.status === "idle") {
            this.clearActivePlay(char);
            returnToIdle(char);
          }
          showBubble(
            char,
            msg.status === "completed" ? "success" : "fail",
            msg.status === "completed" ? 3000 : 5000,
          );
        }
      }
    }
  }

  private patchStartedPlay(msg: PlayEvent): void {
    if (msg.status !== "started" || !this.latestState) return;

    // PlayEventStarted fields are all required-nullable on the wire, so no
    // ?? null defensiveness is needed; makeActivePlay fills the rest.
    const currentPlay = makeActivePlay({
      play_type: msg.play_type,
      agent_id: msg.agent_id,
      play_id: msg.play_id,
      started_at: msg.started_at,
      issue_number: msg.issue_number,
      pr_number: msg.pr_number,
      branch: msg.branch,
      trigger_agent_id: msg.trigger_agent_id,
      trigger_agent_type: msg.trigger_agent_type,
      trigger_error_class: msg.trigger_error_class,
    });

    this.latestState = {
      ...this.latestState,
      active_play: makeActivePlay({
        ...currentPlay,
        // active_play synthesises a started_at so the play-bar timer can run
        // even when the event omitted one; the per-agent current_play keeps
        // the raw (possibly null) value.
        started_at: msg.started_at ?? new Date().toISOString(),
      }),
      agents: this.latestState.agents.map((agent) =>
        msg.agent_id !== null && agent.agent_id === msg.agent_id
          ? {
              ...agent,
              status: "busy",
              current_play: currentPlay,
            }
          : agent,
      ),
    };
  }

  private patchCompletedPlay(msg: PlayEvent): void {
    if (
      (msg.status !== "completed" && msg.status !== "failed") ||
      !this.latestState
    )
      return;

    const active = this.latestState.active_play;
    this.latestState = {
      ...this.latestState,
      active_play:
        active && isCompletionForActivePlay(active, msg) ? null : active,
      agents: this.latestState.agents.map((agent) => {
        if (agent.agent_id !== msg.agent_id) return agent;
        const countPatch =
          msg.status === "completed"
            ? { tasks_completed: agent.tasks_completed + 1 }
            : { tasks_failed: agent.tasks_failed + 1 };
        return {
          ...agent,
          ...countPatch,
          status: "idle",
          current_play: null,
        };
      }),
    };
  }

  private patchActivePlayReplay(activePlay: ActivePlay | null): void {
    if (!this.latestState) return;

    this.latestState = {
      ...this.latestState,
      active_play: activePlay,
      agents: this.latestState.agents.map((agent) =>
        activePlay?.agent_id && agent.agent_id === activePlay.agent_id
          ? {
              ...agent,
              current_play: activePlay,
            }
          : agent,
      ),
    };
  }

  private patchAgentStatus(agentId: string, status: AgentStatus): void {
    if (!this.latestState) return;
    this.latestState = {
      ...this.latestState,
      agents: this.latestState.agents.map((agent) =>
        agent.agent_id === agentId
          ? {
              ...agent,
              status,
              current_play:
                status === "idle" || status === "terminated"
                  ? null
                  : agent.current_play,
            }
          : agent,
      ),
    };
  }

  private routePlay(char: Character, currentPlay: ActivePlay): void {
    const playType = currentPlay.play_type;
    const playId = currentPlay.play_id ?? null;
    this.showCurrentPlayBubble(char, currentPlay);
    if (char.activePlayType === playType && char.activePlayId === playId)
      return;
    char.activePlayId = playId;
    char.activePlayType = playType;
    this.routeStartedPlay(char, currentPlay);
  }

  private clearActivePlay(char: Character): void {
    char.activePlayId = null;
    char.activePlayType = null;
    char.bubble = null;
    char.bubbleUntil = null;
  }

  private routeStartedPlay(char: Character, currentPlay: ActivePlay): void {
    const playType = currentPlay.play_type;
    if (FRONT_DESK_EXIT_PLAY_TYPES.has(playType)) {
      sendToFrontDeskExit(char);
      return;
    }
    if (CURRENT_LOCATION_PLAY_TYPES.has(playType)) {
      return;
    }
    if (RECOVERY_PLAY_TYPES.has(playType)) {
      sendToRecovery(char);
      return;
    }

    const zoneId = PLAY_TO_ZONE[playType];
    if (zoneId === undefined) {
      // Zen Garden is reserved for idle agents — never route a working
      // play there. An unmapped play_type is a bug (likely a new play
      // missing a PLAY_TO_ZONE entry); leave the character where they
      // are rather than visually misclassify them.
      console.warn("routeStartedPlay: no PLAY_TO_ZONE entry", { playType });
      return;
    }
    assignToZone(char, zoneId);
  }

  private currentPlayForAgent(agent: AgentSnapshot): ActivePlay | null {
    if (!agent.current_play) return null;
    const active = this.latestState?.active_play;
    if (
      active &&
      samePlay(active, { ...agent.current_play, agent_id: agent.agent_id })
    ) {
      // active_play carries the canonical trigger metadata; fold it onto the
      // per-agent current_play. All fields are required-nullable now, so no
      // ?? null defensiveness is needed.
      return {
        ...agent.current_play,
        agent_id: active.agent_id,
        trigger_agent_id: active.trigger_agent_id,
        trigger_agent_type: active.trigger_agent_type,
        trigger_error_class: active.trigger_error_class,
      };
    }
    return agent.current_play;
  }

  private showCurrentPlayBubble(char: Character, currentPlay: ActivePlay): void {
    const primary = formatPlayWithTarget(currentPlay.play_type, currentPlay);
    const trigger =
      currentPlay.trigger_error_class ??
      currentPlay.trigger_agent_type ??
      null;
    showBubble(char, {
      text: trigger ? `${primary}: ${trigger}` : primary,
      tone: "work",
    });
  }

  private markBusyAgentsForFeedback(): void {
    for (const char of this.characters.values()) {
      if (char.status === "busy" || char.state === CharacterState.WORK) {
        showBubble(char, "feedback");
      }
    }
  }
}
