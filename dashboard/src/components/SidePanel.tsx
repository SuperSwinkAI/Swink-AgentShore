import React, { useEffect, useReducer } from "react";
import type {
  ActivePlay,
  AgentSnapshot,
  PlayEvent,
  StateUpdate,
} from "../types";
import { makeActivePlay } from "../types";
import {
  formatAgentClass,
  formatAgentType,
  formatPlayType,
  formatPlayWithTarget,
  shortAgentName,
} from "../format";
import { getPlayStats } from "../hud/agentPlayStats";
import { AGENT_COLORS } from "../characters/types";

export type AgentClickHandler = (agentId: string | null) => void;

interface SidePanelState {
  agents: AgentSnapshot[];
  activePlay: ActivePlay | null;
  selectedAgentId: string | null;
  providerFilter: string | null;
  eventCurrentPlay: Map<string, ActivePlay>;
  onAgentClick: AgentClickHandler | null;
}

type SidePanelAction =
  | { type: "hydrate"; state: SidePanelState }
  | {
      type: "state_update";
      agents: AgentSnapshot[];
      activePlay: ActivePlay | null;
    }
  | { type: "play_event"; event: PlayEvent }
  | { type: "select_agent"; agentId: string | null }
  | { type: "set_provider_filter"; provider: string | null }
  | { type: "set_click_handler"; handler: AgentClickHandler | null }
  | { type: "set_active_play"; activePlay: ActivePlay | null };

function reducer(
  state: SidePanelState,
  action: SidePanelAction,
): SidePanelState {
  switch (action.type) {
    case "hydrate":
      return action.state;
    case "state_update": {
      const nextEventCurrentPlay = new Map(state.eventCurrentPlay);
      for (const agent of action.agents) {
        if (agent.current_play) nextEventCurrentPlay.delete(agent.agent_id);
      }
      const availableProviders = new Set<string>(
        action.agents.map((agent) => agent.agent_type),
      );
      const providerFilter = state.providerFilter
        ? availableProviders.has(state.providerFilter)
          ? state.providerFilter
          : null
        : null;
      const selectedAgentId = action.agents.some(
        (agent) => agent.agent_id === state.selectedAgentId,
      )
        ? state.selectedAgentId
        : null;
      return {
        ...state,
        agents: action.agents,
        activePlay: action.activePlay,
        selectedAgentId,
        providerFilter,
        eventCurrentPlay: nextEventCurrentPlay,
      };
    }
    case "play_event": {
      if (!action.event.agent_id) return state;
      const nextMap = new Map(state.eventCurrentPlay);
      if (action.event.status === "started") {
        nextMap.set(
          action.event.agent_id,
          makeActivePlay({
            play_type: action.event.play_type,
            agent_id: action.event.agent_id,
            play_id: action.event.play_id,
            started_at: action.event.started_at,
            issue_number: action.event.issue_number,
            pr_number: action.event.pr_number,
            branch: action.event.branch,
            trigger_agent_id: action.event.trigger_agent_id,
            trigger_agent_type: action.event.trigger_agent_type,
            trigger_error_class: action.event.trigger_error_class,
          }),
        );
      } else {
        nextMap.delete(action.event.agent_id);
      }
      return { ...state, eventCurrentPlay: nextMap };
    }
    case "select_agent":
      return { ...state, selectedAgentId: action.agentId };
    case "set_provider_filter":
      return { ...state, providerFilter: action.provider };
    case "set_click_handler":
      return { ...state, onAgentClick: action.handler };
    case "set_active_play":
      return { ...state, activePlay: action.activePlay };
  }
}

const INITIAL_STATE: SidePanelState = {
  agents: [],
  activePlay: null,
  selectedAgentId: null,
  providerFilter: null,
  eventCurrentPlay: new Map(),
  onAgentClick: null,
};

type Dispatch = (action: SidePanelAction) => void;
const dispatchers = new Set<Dispatch>();
let latestState = INITIAL_STATE;

function usePanel(): [SidePanelState, Dispatch] {
  const [state, dispatch] = useReducer(reducer, latestState);
  useEffect(() => {
    dispatchers.add(dispatch);
    dispatch({ type: "hydrate", state: latestState });
    return () => {
      dispatchers.delete(dispatch);
    };
  }, []);
  return [state, dispatch];
}

function broadcast(action: SidePanelAction): void {
  latestState = reducer(latestState, action);
  dispatchers.forEach((d) => d(action));
}

export function notifySidePanelUpdate(state: StateUpdate): void {
  broadcast({
    type: "state_update",
    agents: state.agents,
    activePlay: state.active_play,
  });
}

export function notifySidePanelPlayEvent(event: PlayEvent): void {
  broadcast({ type: "play_event", event });
}

export function notifySidePanelSelectAgent(agentId: string | null): void {
  broadcast({ type: "select_agent", agentId });
}

export function notifySidePanelClickHandler(
  handler: AgentClickHandler | null,
): void {
  broadcast({ type: "set_click_handler", handler });
}

export function notifySidePanelActivePlay(activePlay: ActivePlay | null): void {
  broadcast({ type: "set_active_play", activePlay });
}

function currentPlayForAgent(
  agent: AgentSnapshot,
  state: SidePanelState,
): ActivePlay | null {
  if (agent.current_play) return agent.current_play;
  const eventPlay = state.eventCurrentPlay.get(agent.agent_id);
  if (eventPlay) return eventPlay;
  if (state.activePlay?.agent_id === agent.agent_id) {
    return makeActivePlay({
      play_type: state.activePlay.play_type,
      agent_id: state.activePlay.agent_id,
      play_id: state.activePlay.play_id,
      started_at: state.activePlay.started_at,
      issue_number: state.activePlay.issue_number,
      pr_number: state.activePlay.pr_number,
      branch: state.activePlay.branch,
    });
  }
  return null;
}

function displayStatusForAgent(
  agent: AgentSnapshot,
  current: ActivePlay | null,
): AgentSnapshot["status"] {
  if (current && agent.status === "idle") return "busy";
  return agent.status;
}

function currentPlayTargetLabel(current: ActivePlay | null): string | null {
  if (!current) return null;
  if (current.issue_number !== null) return `Issue #${current.issue_number}`;
  if (current.pr_number !== null) return `PR #${current.pr_number}`;
  if (current.branch) return current.branch;
  return null;
}

function providerLabel(agentType: string): string {
  switch (agentType) {
    case "claude_code":
      return "Claude";
    case "codex":
      return "Codex";
    case "grok":
      return "Grok";
    case "api_gpt":
      return "OpenAI";
    case "api_other":
      return "API";
    default:
      return formatAgentType(agentType);
  }
}

function sessionProviders(agents: AgentSnapshot[]): string[] {
  const seen = new Set<string>();
  const providers: string[] = [];
  for (const agent of agents) {
    if (seen.has(agent.agent_type)) continue;
    seen.add(agent.agent_type);
    providers.push(agent.agent_type);
  }
  return providers;
}

function DetailRow({
  label,
  value,
}: {
  label: string;
  value: string;
}): React.ReactElement {
  return (
    <div className="agent-detail-row">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function PlaysByType({ agentId }: { agentId: string }): React.ReactElement {
  const stats = getPlayStats(agentId);
  const maxTotal = stats.byType[0]?.total ?? 1;
  return (
    <div className="plays-section">
      <div className="plays-section-title">Plays by type</div>
      {stats.byType.length === 0 ? (
        <div className="plays-empty">No plays yet.</div>
      ) : (
        stats.byType.map((entry) => (
          <div key={entry.playType} className="plays-row">
            <span className="plays-row-label">
              {formatPlayType(entry.playType)}
            </span>
            <span className="plays-row-bar">
              <span
                className="plays-row-bar-fill"
                style={{ width: `${(entry.total / maxTotal) * 100}%` }}
              />
            </span>
            <span className="plays-row-count">
              {entry.failed === 0
                ? String(entry.total)
                : `${entry.ok}✓ ${entry.failed}✗`}
            </span>
          </div>
        ))
      )}
    </div>
  );
}

function AgentDetail({
  agent,
  current,
}: {
  agent: AgentSnapshot;
  current: ActivePlay | null;
}): React.ReactElement {
  const displayStatus = displayStatusForAgent(agent, current);
  const target = currentPlayTargetLabel(current);
  return (
    <>
      <DetailRow label="Status" value={displayStatus} />
      <DetailRow
        label="Current play"
        value={current ? formatPlayType(current.play_type) : "Idle"}
      />
      {target && <DetailRow label="Target" value={target} />}
      <div className="agent-detail-row">
        <span>Tokens</span>
        <strong>{agent.total_tokens.toLocaleString()}</strong>
      </div>
      <PlaysByType agentId={agent.agent_id} />
    </>
  );
}

export function SidePanelComponent(): React.ReactElement {
  const [state, dispatch] = usePanel();

  function handleAgentClick(agentId: string): void {
    const nextId = agentId === state.selectedAgentId ? null : agentId;
    dispatch({ type: "select_agent", agentId: nextId });
    state.onAgentClick?.(nextId);
  }

  const providers = sessionProviders(state.agents);
  const visibleAgents = state.providerFilter
    ? state.agents.filter((agent) => agent.agent_type === state.providerFilter)
    : state.agents;

  return (
    <div className="side-panel-content">
      <div className="side-section-title">Agents</div>
      {providers.length > 0 && (
        <div
          className="agent-provider-filters"
          role="group"
          aria-label="Filter agents by provider"
        >
          <button
            type="button"
            className="agent-provider-filter"
            data-agent-provider-filter="all"
            aria-pressed={state.providerFilter === null}
            onClick={() =>
              dispatch({ type: "set_provider_filter", provider: null })
            }
          >
            All
          </button>
          {providers.map((provider) => (
            <button
              key={provider}
              type="button"
              className="agent-provider-filter"
              data-agent-provider-filter={provider}
              aria-pressed={state.providerFilter === provider}
              onClick={() =>
                dispatch({ type: "set_provider_filter", provider })
              }
            >
              {providerLabel(provider)}
            </button>
          ))}
        </div>
      )}
      <div id="agent-list">
        {visibleAgents.map((agent) => {
          const current = currentPlayForAgent(agent, state);
          const displayStatus = displayStatusForAgent(agent, current);
          const playLabel = current
            ? `${formatPlayWithTarget(current.play_type, current)} · `
            : "";
          // Use persisted serializer fields (agents.tasks_completed/tasks_failed)
          // so a dashboard restart or browser refresh doesn't reset the tallies.
          const ok = agent.tasks_completed;
          const failed = agent.tasks_failed;
          const total = ok + failed;
          const costText = `${playLabel}${total} plays · ${ok}✓ ${failed}✗ | $${agent.total_cost.toFixed(2)}`;

          // desktop-31h2: older sessions (pre agents.dispatch_count) send no
          // dispatch_share; default to 0 so the badge renders 0% not NaN.
          const dispatchShare =
            typeof agent.dispatch_share === "number" ? agent.dispatch_share : 0;
          const dispatchPct = Math.round(dispatchShare * 100);
          const selected = agent.agent_id === state.selectedAgentId;
          return (
            <div
              key={agent.agent_id}
              className={`agent-entry${selected ? " selected" : ""}`}
              data-agent-entry-id={agent.agent_id}
            >
              <button
                type="button"
                className={`agent-item${selected ? " selected" : ""}`}
                data-agent-id={agent.agent_id}
                aria-expanded={selected}
                onClick={() => handleAgentClick(agent.agent_id)}
              >
                <div className="agent-heading">
                  <span
                    className={`agent-status ${displayStatus}`}
                    style={{
                      background:
                        AGENT_COLORS[agent.agent_type]?.fill ??
                        "var(--color-fm-neutral)",
                    }}
                    data-agent-type={agent.agent_type}
                  />
                  <span className="agent-name" title={shortAgentName(agent)}>
                    {shortAgentName(agent)}
                  </span>
                  <div className="agent-type">{formatAgentClass(agent)}</div>
                  <span
                    className="agent-dispatch-share"
                    data-agent-dispatch-share={dispatchPct}
                    title={`Dispatch share: ${dispatchPct}% (${agent.dispatch_count ?? 0} dispatches)`}
                  >
                    {dispatchPct}%
                  </span>
                </div>
                <div className="agent-cost">{costText}</div>
              </button>
              {selected && (
                <div id="agent-detail" className="agent-detail-drawer">
                  <AgentDetail agent={agent} current={current} />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
