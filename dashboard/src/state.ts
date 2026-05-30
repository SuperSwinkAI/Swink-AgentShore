import type {
  ActivePlay,
  AgentStatus,
  AgentShoreMessage,
  StateUpdate,
  PlayEvent,
  AgentSnapshot,
} from "./types";
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

export class AgentShoreStateManager {
  private characters = new Map<string, Character>();
  private previousAgents = new Map<string, AgentSnapshot>();
  private npcs = NPCS.map((npc) => spawnNpcCharacter(npc));
  private lastSeenSeq = 0;

  // latest state for HUD (Phase 4)
  latestState: StateUpdate | null = null;
  feedbackPending: string | null = null;
  connected = true;
  sessionEnded = false;

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
   * Wipe per-session state: agent characters, previous-agents map, the
   * latest state snapshot, and seq counter. Called when the transport
   * sees a state_update with a new session_id and the previous session
   * never sent a clean session_ended (e.g. Tauri shell was killed).
   * NPCs and bootstrap progress are preserved across sessions — they
   * are properties of the office, not of any one orchestrator run.
   */
  resetSession(): void {
    this.characters.clear();
    this.previousAgents.clear();
    this.latestState = null;
    this.feedbackPending = null;
    this.sessionEnded = false;
    // Don't reset lastSeenSeq; the new session's bridge starts at seq=0
    // anyway and we only need to drop strictly older messages.
    this.lastSeenSeq = 0;
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
    // Drop out-of-order or replayed messages using the monotonic seq number.
    // Not all message types carry seq (ConnectionLost/Restored are client-synthetic).
    if ("seq" in msg && typeof msg.seq === "number") {
      if (msg.seq <= this.lastSeenSeq) return false;
      this.lastSeenSeq = msg.seq;
    }

    switch (msg.type) {
      case "state_update":
        this.handleStateUpdate(msg);
        return true;
      case "play_event":
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
        return false;
      case "connection_lost":
        this.connected = false;
        return false;
      case "connection_restored":
        this.connected = true;
        this.sessionEnded = false;
        return false;
      case "active_play_replay":
        this.patchActivePlayReplay(msg.active_play);
        if (msg.active_play?.agent_id) {
          const char = this.characters.get(msg.active_play.agent_id);
          if (char) {
            this.routeCurrentPlay(char, msg.active_play);
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
                this.routeCurrentPlay(char, currentPlay);
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
                this.routeCurrentPlay(char, currentPlay);
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
    this.latestState = msg;
    const currentAgents = new Map(msg.agents.map((a) => [a.agent_id, a]));

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
          this.routeCurrentPlay(char, currentPlay);
        } else {
          this.clearActivePlay(char);
          sendToRecovery(char);
        }
      } else if (currentPlay && agent.status !== "terminated") {
        this.routeCurrentPlay(char, currentPlay);
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
          this.routeCurrentPlay(char, currentPlay);
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

    const currentPlay: ActivePlay = {
      play_type: msg.play_type,
      play_id: msg.play_id ?? null,
      started_at: msg.started_at ?? null,
      issue_number: msg.issue_number ?? null,
      pr_number: msg.pr_number ?? null,
      branch: msg.branch ?? null,
      trigger_agent_id: msg.trigger_agent_id ?? null,
      trigger_agent_type: msg.trigger_agent_type ?? null,
      trigger_error_class: msg.trigger_error_class ?? null,
    };

    this.latestState = {
      ...this.latestState,
      active_play: {
        play_type: msg.play_type,
        agent_id: msg.agent_id,
        started_at: msg.started_at ?? new Date().toISOString(),
        play_id: msg.play_id ?? null,
        issue_number: msg.issue_number ?? null,
        pr_number: msg.pr_number ?? null,
        branch: msg.branch ?? null,
        trigger_agent_id: msg.trigger_agent_id ?? null,
        trigger_agent_type: msg.trigger_agent_type ?? null,
        trigger_error_class: msg.trigger_error_class ?? null,
      },
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

    this.latestState = {
      ...this.latestState,
      active_play:
        this.latestState.active_play?.play_id === msg.play_id ||
        this.latestState.active_play?.agent_id === msg.agent_id ||
        (msg.agent_id === null &&
          this.latestState.active_play?.play_type === msg.play_type)
          ? null
          : this.latestState.active_play,
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

  private routeCurrentPlay(char: Character, currentPlay: ActivePlay): void {
    this.routePlay(char, currentPlay);
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
      active.agent_id === agent.agent_id &&
      active.play_type === agent.current_play.play_type &&
      (active.play_id === null ||
        agent.current_play.play_id === null ||
        active.play_id === agent.current_play.play_id)
    ) {
      return {
        ...agent.current_play,
        agent_id: active.agent_id,
        trigger_agent_id: active.trigger_agent_id ?? null,
        trigger_agent_type: active.trigger_agent_type ?? null,
        trigger_error_class: active.trigger_error_class ?? null,
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
