import React, { useEffect, useReducer } from "react";
import type {
  AgentSnapshot,
  BudgetSnapshot,
  PlayEvent,
  StateUpdate,
} from "../types";
import {
  PLAY_DISPLAY_NAMES,
  PLAY_KEYS,
  PLAY_RESERVED,
  PLAY_TRAY_KEYS,
  PLAY_TO_ZONE,
  ZONE_ACCENTS,
} from "../office/zones";
import { ZoneId } from "../office/layout";

const LS_KEY = "agentshore.playsPanel.collapsed";
const DEFAULT_ZONE = ZoneId.FRONT_DESK;
const ACTION_INDEX_BY_KEY = new Map(PLAY_KEYS.map((key, idx) => [key, idx]));

interface PanelState {
  latestState: StateUpdate | null;
  collapsed: boolean;
}

export interface DrainStatus {
  visible: boolean;
  reason: string | null;
  connectionLost: boolean;
}

type PanelAction =
  | { type: "state_update"; state: StateUpdate }
  | { type: "play_event"; event: PlayEvent }
  | { type: "toggle_collapse" };

function reducer(panel: PanelState, action: PanelAction): PanelState {
  switch (action.type) {
    case "state_update":
      return { ...panel, latestState: action.state };
    case "play_event":
      return panel;
    case "toggle_collapse": {
      const next = !panel.collapsed;
      localStorage.setItem(LS_KEY, String(next));
      return { ...panel, collapsed: next };
    }
  }
}

type Dispatch = (action: PanelAction) => void;
const dispatchers = new Set<Dispatch>();

function broadcast(action: PanelAction): void {
  dispatchers.forEach((d) => d(action));
}

export function notifyPlaysPanelUpdate(state: StateUpdate): void {
  broadcast({ type: "state_update", state });
}

export function notifyPlaysPanelEvent(event: PlayEvent): void {
  broadcast({ type: "play_event", event });
}

function usePanelState(): [PanelState, Dispatch] {
  const [state, dispatch] = useReducer(reducer, {
    latestState: null,
    collapsed: localStorage.getItem(LS_KEY) === "true",
  });
  useEffect(() => {
    dispatchers.add(dispatch);
    return () => {
      dispatchers.delete(dispatch);
    };
  }, []);
  return [state, dispatch];
}

function agentsForPlay(
  playKey: string,
  agents: AgentSnapshot[],
  activePlay: StateUpdate["active_play"],
): AgentSnapshot[] {
  const result: AgentSnapshot[] = [];
  for (const agent of agents) {
    if (
      agent.current_play?.play_type === playKey &&
      agent.status !== "terminated"
    ) {
      result.push(agent);
    }
  }
  if (activePlay && !activePlay.agent_id && activePlay.play_type === playKey) {
    // Preserve ACTIVE state for session-level plays that are not bound to an agent.
    result.push({
      agent_id: "__session_active__",
      agent_type: "codex",
      display_name: "Session",
      status: "idle",
      current_play: activePlay,
      context_size: 0,
      total_cost: 0,
      total_tokens: 0,
      tasks_completed: 0,
      tasks_failed: 0,
    });
  }
  return result;
}

interface PlayCardProps {
  playKey: string;
  agents: AgentSnapshot[];
  actionMask: boolean[];
  maskReason?: string;
}

function PlayCard({
  playKey,
  agents,
  actionMask,
  maskReason,
}: PlayCardProps): React.ReactElement {
  const count = agents.length;
  const isActive = count > 0;
  const isReserved = PLAY_RESERVED.has(playKey);
  const actionIndex = ACTION_INDEX_BY_KEY.get(playKey);
  const isMasked =
    !isActive &&
    (isReserved ||
      (actionIndex !== undefined &&
        actionMask.length > actionIndex &&
        !actionMask[actionIndex]));

  const zoneId = PLAY_TO_ZONE[playKey] ?? DEFAULT_ZONE;
  const accent = ZONE_ACCENTS[zoneId];
  const displayName = PLAY_DISPLAY_NAMES[playKey] ?? playKey;

  const className = [
    "pp-card",
    isReserved ? "pp-card-reserved" : "",
    isActive
      ? "pp-card-active"
      : isMasked
        ? "pp-card-masked"
        : "pp-card-default",
  ]
    .filter(Boolean)
    .join(" ");

  const title =
    isMasked && maskReason ? `${displayName}: ${maskReason}` : displayName;

  return (
    <button
      type="button"
      className={className}
      data-play-key={playKey}
      aria-disabled={isMasked}
      style={
        {
          "--zone-accent": accent,
          cursor: isMasked ? "help" : "default",
        } as React.CSSProperties
      }
      title={title}
    >
      <span className="pp-card-name">{displayName}</span>
      {count > 1 && <span className="pp-card-badge">×{count}</span>}
      <div className="pp-card-activity" hidden={!isActive}>
        <div className="pp-card-activity-fill" />
      </div>
      <span className="pp-hatch" />
    </button>
  );
}

function formatRemainingTime(minutes: number): string {
  const safe = Math.max(0, Math.round(minutes));
  const hours = Math.floor(safe / 60);
  const mins = safe % 60;
  if (hours > 0) return `${hours}h ${mins}m`;
  return `${mins}m`;
}

/**
 * Resolve the budget label segments, meter fill %, and tooltip from a snapshot.
 *
 * Dollars and wall-clock time are independent soft caps; either may be off.
 * The single meter tracks whichever cap is *closest to triggering the drain*
 * (the binding constraint) so a time-only run shows real progress instead of
 * an empty dollar bar. The meter renders *between* the two figures — dollars
 * on the left, time-left on the right — so each segment reads cleanly and the
 * bar visually separates them. A fully-uncapped run collapses the right
 * segment to a compact "∞" rather than a verbose "(unlimited)" string.
 */
function describeBudget(b: BudgetSnapshot): {
  dollarLabel: string;
  timeLabel: string;
  title: string;
  pct: number;
} {
  const spent = b.spent;
  const total = b.total_budget;
  const dollarCapped = b.enabled && total !== null && b.remaining !== null;
  const timeCapped =
    !!b.time_enabled &&
    b.time_total_minutes !== null &&
    b.time_total_minutes !== undefined &&
    b.time_remaining_minutes !== null &&
    b.time_remaining_minutes !== undefined;

  const dollarPct = dollarCapped && total ? (spent / total) * 100 : 0;
  const timePct =
    timeCapped && b.time_total_minutes
      ? ((b.time_total_minutes - (b.time_remaining_minutes as number)) /
          b.time_total_minutes) *
        100
      : 0;

  const dollarLabel = dollarCapped
    ? `$${spent.toFixed(2)} / $${(total as number).toFixed(2)}`
    : `$${spent.toFixed(2)}`;
  // Right segment: remaining time when capped; "∞" only when nothing is
  // capped at all (a dollar-only run leaves the right side empty).
  const timeLabel = timeCapped
    ? `${formatRemainingTime(b.time_remaining_minutes as number)} left`
    : !dollarCapped
      ? "∞"
      : "";

  const sep = timeLabel ? " · " : "";
  return {
    dollarLabel,
    timeLabel,
    title: `${dollarLabel}${sep}${timeLabel}`,
    pct: Math.max(dollarPct, timePct),
  };
}

function BudgetBar({
  state,
  drainStatus,
}: {
  state: StateUpdate | null;
  drainStatus?: DrainStatus;
}): React.ReactElement {
  let dollarLabel: string;
  let timeLabel = "";
  let titleText: string;
  let fillWidth: string;
  let fillClass: string;

  if (!state) {
    dollarLabel = "$0.00";
    titleText = "$0.00";
    fillWidth = "0%";
    fillClass = "budget-fill ok";
  } else if (state.budget) {
    const desc = describeBudget(state.budget);
    dollarLabel = desc.dollarLabel;
    timeLabel = desc.timeLabel;
    titleText = desc.title;
    fillWidth = `${Math.min(desc.pct, 100)}%`;
    fillClass =
      desc.pct > 80
        ? "budget-fill critical"
        : desc.pct > 60
          ? "budget-fill warning"
          : "budget-fill ok";
  } else {
    dollarLabel = `$${state.total_cost.toFixed(2)}`;
    titleText = dollarLabel;
    fillWidth = "0%";
    fillClass = "budget-fill ok";
  }

  const budgetMeter = (
    <div className="hud-chip budget-bar" title={titleText}>
      <span id="budget-label" className="budget-label-text budget-dollar">
        {dollarLabel}
      </span>
      <div className="budget-track">
        <div
          className={fillClass}
          id="budget-fill"
          style={{ width: fillWidth }}
        />
      </div>
      {timeLabel && (
        <span className="budget-label-text budget-time">{timeLabel}</span>
      )}
    </div>
  );

  if (!drainStatus?.visible) return budgetMeter;

  const leadingText = drainStatus.connectionLost
    ? "DRAINING"
    : drainStatus.reason
      ? `DRAINING (${drainStatus.reason})`
      : "DRAINING";
  const trailingText = drainStatus.connectionLost
    ? "connection lost"
    : "waiting for agents to finish";

  return (
    <div className="budget-drain-wrapper" role="status">
      <span className="budget-drain-leading">{leadingText}</span>
      {budgetMeter}
      <span className="budget-drain-trailing">{trailingText}</span>
    </div>
  );
}

export function PlaysPanelComponent({
  drainStatus,
}: {
  drainStatus?: DrainStatus;
} = {}): React.ReactElement {
  const [panel, dispatch] = usePanelState();
  const { latestState: state, collapsed } = panel;

  useEffect(() => {
    const el = document.getElementById("plays-panel");
    if (el) el.classList.toggle("pp-collapsed", collapsed);
  }, [collapsed]);

  const agents = state?.agents ?? [];
  const activePlay = state?.active_play ?? null;
  const actionMask = state?.action_mask ?? [];

  let totActive = 0;
  let totReady = 0;
  let totMasked = 0;

  const cardAgents: Record<string, AgentSnapshot[]> = {};
  const maskedReasons = state?.mask_reasons ?? {};
  for (const key of PLAY_TRAY_KEYS) {
    const playAgents = agentsForPlay(key, agents, activePlay);
    cardAgents[key] = playAgents;

    const count = playAgents.length;
    const isActive = count > 0;
    const isReserved = PLAY_RESERVED.has(key);
    const actionIndex = ACTION_INDEX_BY_KEY.get(key);
    const isMasked =
      !isActive &&
      (isReserved ||
        (actionIndex !== undefined &&
          actionMask.length > actionIndex &&
          !actionMask[actionIndex]));

    // Count plays, not play types — a card with 2 agents running issue_pickup
    // contributes 2 to ACTIVE, matching the lower-panel "M active plays" string.
    if (isActive) {
      totActive += count;
    } else if (!isMasked) {
      totReady++;
    } else {
      totMasked++;
    }
  }

  return (
    <>
      <div className="pp-titlebar">
        <div className="pp-title-main">
          <button
            id="pp-collapse-btn"
            type="button"
            className="pp-collapse-btn"
            aria-label={
              collapsed ? "Expand plays panel" : "Collapse plays panel"
            }
            onClick={() => dispatch({ type: "toggle_collapse" })}
          >
            {collapsed ? "▴" : "▾"}
          </button>
          <span className="pp-title">PLAYS</span>
        </div>
        <BudgetBar state={state} drainStatus={drainStatus} />
        <span className="pp-totals">
          <span className="pp-totals-chip" id="pp-totals-active">
            {totActive} ACTIVE
          </span>
          <span className="pp-totals-chip" id="pp-totals-ready">
            {totReady} READY
          </span>
          <span className="pp-totals-chip" id="pp-totals-masked">
            {totMasked} MASKED
          </span>
          <span className="pp-totals-chip" id="pp-totals-total">
            {PLAY_TRAY_KEYS.length} TOTAL
          </span>
        </span>
      </div>
      <div id="plays-panel-grid" className="pp-flat" hidden={collapsed}>
        {PLAY_TRAY_KEYS.map((key) => (
          <PlayCard
            key={key}
            playKey={key}
            agents={cardAgents[key] ?? []}
            actionMask={actionMask}
            maskReason={maskedReasons[key]}
          />
        ))}
      </div>
    </>
  );
}

export { PlaysPanelComponent as PlaysPanel };
