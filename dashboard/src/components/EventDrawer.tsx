import React, { useEffect, useReducer } from "react";
import type {
  ActivePlay,
  AgentSnapshot,
  PlayEvent,
  StateUpdate,
} from "../types";
import {
  formatAgentClass,
  formatAgentType,
  formatPlayWithTarget,
  shortAgentName,
} from "../format";

const MAX_FINISHED_EVENTS = 8;

type EventFilter = "all" | "started" | "completed" | "failed";

export interface EventCard {
  key: string;
  name: string;
  type: string;
  play: string;
  status: "started" | "completed" | "failed";
  result: string;
  errorMessage: string | null;
  startedAt: string | null;
  endedAt: string | null;
  updatedAt: number;
  playId: number | null;
  agentId: string | null;
  triggerAgentId: string | null;
  triggerAgentType: string | null;
  triggerErrorClass: string | null;
  playType: string;
  issueNumber: number | null;
  prNumber: number | null;
  branch: string | null;
}

export interface DrawerState {
  cards: EventCard[];
  activeFilter: EventFilter;
  fallbackId: number;
  agentsById: Map<string, AgentSnapshot>;
}

export type DrawerAction =
  | { type: "hydrate" }
  | { type: "state_update"; agents: AgentSnapshot[] }
  | { type: "play_event"; event: PlayEvent }
  | { type: "replay"; history: PlayEvent[] }
  | { type: "reset" }
  | { type: "set_filter"; filter: EventFilter };

function lookupAgent(
  agentId: string | null,
  agentsById: Map<string, AgentSnapshot>,
): AgentSnapshot | undefined {
  return agentId ? agentsById.get(agentId) : undefined;
}

function isAgentAssignedEvent(event: PlayEvent): boolean {
  return typeof event.agent_id === "string" && event.agent_id.length > 0;
}

function displayAgentType(
  agentId: string | null,
  agentsById: Map<string, AgentSnapshot>,
  triggerAgentType: string | null = null,
): string {
  if (!agentId)
    return triggerAgentType ? formatAgentType(triggerAgentType) : "System";
  const agent = agentsById.get(agentId);
  if (agent) return formatAgentClass(agent);
  return triggerAgentType ? formatAgentType(triggerAgentType) : "Agent";
}

function resultText(event: PlayEvent): string {
  if (event.status === "started") {
    return event.trigger_error_class
      ? `Running (${formatAgentType(event.trigger_error_class)})`
      : "Running";
  }
  if (event.status === "completed") return "Completed";
  return "Failed";
}

function errorMessageText(event: PlayEvent): string | null {
  if (event.status !== "failed" || !event.error) return null;
  const normalized = event.error.trim().replace(/\s+/g, " ");
  if (!normalized) return null;
  return normalized;
}

function knownTimestamp(value: string | null | undefined): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function inferStartedAtFromCompletion(
  event: Extract<PlayEvent, { status: "completed" | "failed" }>,
  endedAt: string | null,
): string | null {
  if (!endedAt || !Number.isFinite(event.duration_seconds)) return null;
  const endedMs = Date.parse(endedAt);
  if (Number.isNaN(endedMs)) return null;
  return new Date(endedMs - event.duration_seconds * 1000).toISOString();
}

function startedAtForEvent(
  event: PlayEvent,
  existing: EventCard | undefined,
  endedAt: string | null,
): string | null {
  if (event.status === "started") {
    return (
      knownTimestamp(event.started_at) ??
      knownTimestamp(event.timestamp) ??
      existing?.startedAt ??
      null
    );
  }
  return existing?.startedAt ?? inferStartedAtFromCompletion(event, endedAt);
}

function endedAtForEvent(
  event: PlayEvent,
  existing: EventCard | undefined,
): string | null {
  if (event.status === "started") return null;
  return (
    knownTimestamp(event.timestamp) ??
    existing?.endedAt ??
    new Date().toISOString()
  );
}

interface KeyResult {
  key: string;
  fallbackId: number;
}

function eventKey(
  event: PlayEvent,
  cards: EventCard[],
  fallbackId: number,
): KeyResult {
  if (event.play_id !== undefined && event.play_id !== null) {
    return { key: `play:${event.play_id}`, fallbackId };
  }
  if (event.status === "started" && event.agent_id) {
    const issueNumber = event.issue_number ?? "";
    const prNumber = event.pr_number ?? "";
    const branch = event.branch ?? "";
    return {
      key: `agent:${event.agent_id}:${event.play_type}:${issueNumber}:${prNumber}:${branch}`,
      fallbackId,
    };
  }
  if (event.status !== "started") {
    const running = cards.find(
      (card) =>
        card.status === "started" &&
        card.agentId === event.agent_id &&
        card.playType === event.play_type,
    );
    if (running) return { key: running.key, fallbackId };
  }
  return { key: `fallback:${fallbackId}`, fallbackId: fallbackId + 1 };
}

function pruneCards(cards: EventCard[]): EventCard[] {
  const running = cards.filter((card) => card.status === "started");
  const finished = cards
    .filter((card) => card.status !== "started")
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .slice(0, MAX_FINISHED_EVENTS);
  return [...running, ...finished].sort((a, b) => b.updatedAt - a.updatedAt);
}

function upsertCardInList(
  event: PlayEvent,
  cards: EventCard[],
  agentsById: Map<string, AgentSnapshot>,
  fallbackId: number,
): { cards: EventCard[]; fallbackId: number } {
  const { key, fallbackId: nextFallback } = eventKey(event, cards, fallbackId);
  const existing = cards.find((card) => card.key === key);
  const issueNumber =
    "issue_number" in event
      ? (event.issue_number ?? null)
      : (existing?.issueNumber ?? null);
  const prNumber =
    "pr_number" in event
      ? (event.pr_number ?? null)
      : (existing?.prNumber ?? null);
  const branch =
    "branch" in event ? (event.branch ?? null) : (existing?.branch ?? null);
  const triggerAgentId =
    "trigger_agent_id" in event
      ? (event.trigger_agent_id ?? null)
      : (existing?.triggerAgentId ?? null);
  const triggerAgentType =
    "trigger_agent_type" in event
      ? (event.trigger_agent_type ?? null)
      : (existing?.triggerAgentType ?? null);
  const triggerErrorClass =
    "trigger_error_class" in event
      ? (event.trigger_error_class ?? null)
      : (existing?.triggerErrorClass ?? null);
  const displayAgentId = event.agent_id ?? triggerAgentId;
  const endedAt = endedAtForEvent(event, existing);
  const startedAt = startedAtForEvent(event, existing, endedAt);

  // Resolve the agent's name/type from the live snapshot when possible, but
  // fall back to the values already captured on the card (the same existing?.*
  // preservation used for the trigger/target fields above). A terminated agent
  // drops out of agentsById, so recomputing would clobber a real name with the
  // id-slice/"Agent" fallback — completed-play tiles are historical records and
  // must keep the identity captured while the agent was live.
  const resolvedAgent = lookupAgent(displayAgentId, agentsById);
  const resolvedName = resolvedAgent
    ? shortAgentName(resolvedAgent, displayAgentId?.slice(0, 8) ?? "Session")
    : (existing?.name ?? (displayAgentId?.slice(0, 8) ?? "Session"));
  const resolvedType = resolvedAgent
    ? formatAgentClass(resolvedAgent)
    : (existing?.type ??
      displayAgentType(displayAgentId, agentsById, triggerAgentType));

  const next: EventCard = {
    key,
    name: resolvedName,
    type: resolvedType,
    play: formatPlayWithTarget(event.play_type, {
      issue_number: issueNumber,
      pr_number: prNumber,
    }),
    status: event.status,
    result: resultText(event),
    errorMessage: errorMessageText(event),
    startedAt,
    endedAt,
    updatedAt: Date.now(),
    playId: event.play_id ?? existing?.playId ?? null,
    agentId: event.agent_id,
    triggerAgentId,
    triggerAgentType,
    triggerErrorClass,
    playType: event.play_type,
    issueNumber,
    prNumber,
    branch,
  };

  const withoutExisting = existing
    ? cards.filter((card) => card.key !== key)
    : cards;
  return {
    cards: pruneCards([...withoutExisting, next]),
    fallbackId: nextFallback,
  };
}

function upsertCurrentAgentPlayInList(
  agent: AgentSnapshot,
  current: ActivePlay,
  cards: EventCard[],
  agentsById: Map<string, AgentSnapshot>,
  fallbackId: number,
): { cards: EventCard[]; fallbackId: number } {
  // ActivePlay now matches the wire contract (all fields required-nullable),
  // so no ?? null defensiveness is needed mapping it onto PlayEventStarted.
  return upsertCardInList(
    {
      type: "play_event",
      status: "started",
      play_type: current.play_type,
      agent_id: agent.agent_id,
      play_id: current.play_id,
      started_at: current.started_at,
      issue_number: current.issue_number,
      pr_number: current.pr_number,
      branch: current.branch,
      trigger_agent_id: current.trigger_agent_id,
      trigger_agent_type: current.trigger_agent_type,
      trigger_error_class: current.trigger_error_class,
    },
    cards,
    agentsById,
    fallbackId,
  );
}

function currentPlayMatchesCard(current: ActivePlay, card: EventCard): boolean {
  if (current.play_id !== null || card.playId !== null) {
    return current.play_id === card.playId;
  }
  return (
    current.play_type === card.playType &&
    current.issue_number === card.issueNumber &&
    current.pr_number === card.prNumber &&
    current.branch === card.branch
  );
}

function endCardFromSnapshot(card: EventCard, result: string): EventCard {
  return {
    ...card,
    status: "failed",
    result,
    endedAt: card.endedAt ?? new Date().toISOString(),
    updatedAt: Date.now(),
  };
}

export function reducer(state: DrawerState, action: DrawerAction): DrawerState {
  switch (action.type) {
    case "hydrate":
      return state;
    case "state_update": {
      const agentsById = new Map(
        action.agents.map((agent) => [agent.agent_id, agent]),
      );
      let cards = state.cards;
      let fallbackId = state.fallbackId;

      for (const agent of action.agents) {
        if (agent.current_play) {
          const result = upsertCurrentAgentPlayInList(
            agent,
            agent.current_play,
            cards,
            agentsById,
            fallbackId,
          );
          cards = result.cards;
          fallbackId = result.fallbackId;
        }
      }

      // Reconcile cards stuck on "Running" against the orchestrator's
      // authoritative agent state. Two flip cases only:
      //
      // - Agent disappeared from a later snapshot → the handle was cleared;
      //   mark the card ended.
      // - Agent has moved to a DIFFERENT play → the prior card is stale;
      //   mark it ended.
      //
      // The previous "agent idle + null current_play → mark Completed"
      // path was repeatedly mis-firing during active plays — agent.status
      // transitions through brief idle snapshots while a play is genuinely
      // in progress (seen 2026-05-22 desktop-y3kq: design_audit running
      // 0:19 in the active panel but the card flipped to "Completed
      // (status reconciled)"). The earlier ``agent.status === "idle"``
      // discriminator didn't fix it because that intermediate state
      // really does show idle.
      //
      // Inferring completion from agent state is a backstop for missed
      // play_event "completed" events. False-positive completions are
      // worse than false-negative ones (a stuck-on-running card is
      // unambiguous; a wrong "Completed" looks correct and hides bugs).
      // Trust play_event "completed" to drive completion; if those are
      // being lost, fix the WS delivery, not the reducer.
      cards = cards.map((card) => {
        if (card.status !== "started" || !card.agentId) return card;
        const agent = agentsById.get(card.agentId);
        if (!agent) {
          const wasKnownAgent = state.agentsById.has(card.agentId);
          if (!wasKnownAgent && action.agents.length === 0) return card;
          return endCardFromSnapshot(card, "Ended (agent removed)");
        }
        const current = agent.current_play;
        if (!current) return card;
        if (!currentPlayMatchesCard(current, card)) {
          return endCardFromSnapshot(card, "Ended (status reconciled)");
        }
        return card;
      });

      // Refresh name/type from the new agent map, but ONLY for cards whose
      // agent is still present. A terminated agent drops out of the snapshot,
      // and unconditionally recomputing would reset its completed-play cards to
      // the id-slice/"Agent" fallback — clobbering the identity captured while
      // it was live. Completed-play tiles are historical records: keep what was
      // resolved at execution time, only upgrade when the agent is resolvable.
      cards = cards.map((card) => {
        const displayAgentId = card.agentId ?? card.triggerAgentId;
        const resolvedAgent = lookupAgent(displayAgentId, agentsById);
        if (!resolvedAgent) return card;
        return {
          ...card,
          name: shortAgentName(
            resolvedAgent,
            displayAgentId?.slice(0, 8) ?? "Session",
          ),
          type: formatAgentClass(resolvedAgent),
        };
      });

      return { ...state, cards, fallbackId, agentsById };
    }
    case "play_event": {
      if (!isAgentAssignedEvent(action.event)) return state;
      const result = upsertCardInList(
        action.event,
        state.cards,
        state.agentsById,
        state.fallbackId,
      );
      return {
        ...state,
        cards: result.cards,
        fallbackId: result.fallbackId,
      };
    }
    case "replay": {
      let cards: EventCard[] = [];
      let fallbackId = 0;
      for (const event of action.history) {
        if (!isAgentAssignedEvent(event)) continue;
        const result = upsertCardInList(
          event,
          cards,
          state.agentsById,
          fallbackId,
        );
        cards = result.cards;
        fallbackId = result.fallbackId;
      }
      return { ...state, cards, fallbackId };
    }
    case "reset":
      return { ...INITIAL_STATE, activeFilter: state.activeFilter };
    case "set_filter":
      return { ...state, activeFilter: action.filter };
  }
}

export const INITIAL_STATE: DrawerState = {
  cards: [],
  activeFilter: "all",
  fallbackId: 0,
  agentsById: new Map(),
};

type Dispatch = (action: DrawerAction) => void;
const dispatchers = new Set<Dispatch>();
let latestState = INITIAL_STATE;

function broadcast(action: DrawerAction): void {
  latestState = reducer(latestState, action);
  dispatchers.forEach((d) => d(action));
}

export function notifyEventDrawerStateUpdate(state: StateUpdate): void {
  broadcast({ type: "state_update", agents: state.agents });
}

export function notifyEventDrawerEvent(event: PlayEvent): void {
  broadcast({ type: "play_event", event });
}

export function notifyEventDrawerReplay(history: PlayEvent[]): void {
  broadcast({ type: "replay", history });
}

/**
 * Wipe all accumulated cards. Called from Dashboard.tsx on session
 * boundary (session_id change) so a fresh session doesn't display
 * cards from the prior session — symptom was phantom agents with
 * hash-id "names" because the new session's agent registry didn't
 * contain the old agent_ids.
 */
export function notifyEventDrawerReset(): void {
  broadcast({ type: "reset" });
}

function useDrawer(): [DrawerState, Dispatch] {
  const [state, dispatch] = useReducer(reducer, latestState);
  useEffect(() => {
    dispatchers.add(dispatch);
    dispatch({ type: "hydrate" });
    return () => {
      dispatchers.delete(dispatch);
    };
  }, []);
  return [state, dispatch];
}

const FILTERS: { id: EventFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "started", label: "Running" },
  { id: "completed", label: "Done" },
  { id: "failed", label: "Failed" },
];

function resultClass(status: EventCard["status"]): string {
  if (status === "started") return "running";
  return status;
}

function formatClockTime(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  return `${hours}:${minutes}`;
}

function formatTimeRange(startedAt: string | null, endedAt: string | null): string {
  const start = formatClockTime(startedAt) || "--:--";
  const end = formatClockTime(endedAt);
  return end ? `${start} - ${end}` : `${start} -`;
}

export default function EventDrawer(): React.ReactElement {
  const [state, dispatch] = useDrawer();
  const visible = state.cards.filter(
    (card) => state.activeFilter === "all" || card.status === state.activeFilter,
  );

  return (
    <div id="event-drawer" style={{ padding: "8px" }}>
      <div className="drawer-filters">
        {FILTERS.map((filter) => (
          <button
            key={filter.id}
            type="button"
            data-event-filter={filter.id}
            className={state.activeFilter === filter.id ? "selected" : ""}
            onClick={() =>
              dispatch({ type: "set_filter", filter: filter.id })
            }
          >
            {filter.label}
          </button>
        ))}
      </div>
      <div id="event-list">
        {visible.length === 0 ? (
          <div className="event-empty">No events</div>
        ) : (
          visible.map((card) => {
            const timeRange = formatTimeRange(card.startedAt, card.endedAt);
            const errorDetail = card.errorMessage ? `, ${card.errorMessage}` : "";
            return (
              <article
                key={card.key}
                className={`event-card ${card.status}`}
                aria-label={`${card.name}, ${card.type}, ${card.play}, ${card.result}, ${timeRange}${errorDetail}`}
              >
                <div className="event-card-row event-card-primary">
                  <span className="event-agent-name" title={card.name}>
                    {card.name}
                  </span>
                  <span className="event-agent-type" title={card.type}>
                    {card.type}
                  </span>
                </div>
                <div className="event-card-row event-card-secondary">
                  <span className="event-play-name" title={card.play}>
                    {card.play}
                  </span>
                  <span
                    className={`event-result ${resultClass(card.status)}`}
                    title={card.result}
                  >
                    {card.result}
                  </span>
                </div>
                <div className="event-time-range" title={timeRange}>
                  {timeRange}
                </div>
                {card.errorMessage && (
                  <div className="event-error-message" title={card.errorMessage}>
                    <span className="event-error-label">FAILED:</span>{" "}
                    {card.errorMessage}
                  </div>
                )}
              </article>
            );
          })
        )}
      </div>
    </div>
  );
}
