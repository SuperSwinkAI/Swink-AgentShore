import React, { useEffect, useState } from "react";
import type {
  AgentSnapshot,
  EpicStatus,
  PlayTypeStatsSnapshot,
  ProjectGraph,
  StateUpdate,
} from "../types";
import {
  formatAgentClass,
  formatPlayType,
  shortAgentName,
} from "../format";

// React port of `dashboard/src/views/stats/index.ts`.
//
// The imperative module owns four things, mirrored here as module-level
// notify* functions (same pattern as TopBarHud, EpicPanel, FeedbackModal):
//   1. the latest StateUpdate snapshot       -> notifyStatsStageUpdate
//   2. visible vs. hidden                     -> notifyStatsStageVisible
//   3. CSS-variable inset overrides           -> notifyStatsStageInsets
//
// Rendering exactly reproduces the DOM the imperative module builds so the
// `.stats-*` rules already living in `dashboard/src/dashboard.css` style the
// React tree identically.

export interface StatsStageInsets {
  top: number;
  left: number;
  right: number;
  bottom: number;
}

interface StatsStageState {
  state: StateUpdate | null;
  visible: boolean;
  insets: StatsStageInsets | null;
}

const listeners = new Set<(s: StatsStageState) => void>();
let latestState: StatsStageState = {
  state: null,
  visible: false,
  insets: null,
};

function broadcast(next: StatsStageState): void {
  latestState = next;
  listeners.forEach((fn) => fn(next));
}

export function notifyStatsStageUpdate(state: StateUpdate): void {
  broadcast({ ...latestState, state });
}

export function notifyStatsStageVisible(visible: boolean): void {
  broadcast({ ...latestState, visible });
}

export function notifyStatsStageInsets(
  top: number,
  left: number,
  right: number,
  bottom: number,
): void {
  broadcast({ ...latestState, insets: { top, left, right, bottom } });
}

function useStatsStageState(): StatsStageState {
  const [state, setState] = useState<StatsStageState>(latestState);
  useEffect(() => {
    listeners.add(setState);
    setState(latestState);
    return () => {
      listeners.delete(setState);
    };
  }, []);
  return state;
}

function pct(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function decimalPct(value: number): string {
  return `${Math.round(value * 1000) / 10}%`;
}

function money(value: number): string {
  return `$${value.toFixed(2)}`;
}

function duration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const remaining = Math.round(seconds % 60);
  return `${minutes}m ${remaining.toString().padStart(2, "0")}s`;
}

function count(value: number): string {
  return value.toLocaleString();
}

function alignmentClass(score: number): string {
  if (score < 0.3) return "alignment-hot";
  if (score < 0.6) return "alignment-busy";
  return "alignment-ok";
}

function closureClass(ratio: number): string {
  if (ratio >= 0.75) return "closure-high";
  if (ratio >= 0.4) return "closure-mid";
  return "closure-low";
}

function epicLabel(epic: Pick<EpicStatus, "bead_id" | "title">): string {
  return epic.title.trim() || epic.bead_id || "Unnamed epic";
}

function agentSuccessRate(agent: AgentSnapshot): number {
  const total = agent.tasks_completed + agent.tasks_failed;
  return total > 0 ? agent.tasks_completed / total : 0;
}

function StatTile({
  label,
  value,
  sub,
}: {
  label: string;
  value: string;
  sub?: string;
}): React.ReactElement {
  return (
    <div className="stats-tile">
      <div className="stats-tile-label">{label}</div>
      <div className="stats-tile-value">{value}</div>
      {sub !== undefined && <div className="stats-tile-sub">{sub}</div>}
    </div>
  );
}

function SummarySection({ state }: { state: StateUpdate }): React.ReactElement {
  const stats = state.stats;
  const totalPlays = stats?.total_plays ?? state.total_plays;
  const successful =
    stats?.successful_plays ??
    state.agents.reduce((sum, a) => sum + a.tasks_completed, 0);
  const failed =
    stats?.failed_plays ??
    state.agents.reduce((sum, a) => sum + a.tasks_failed, 0);
  const successRate =
    stats?.success_rate ?? (totalPlays > 0 ? successful / totalPlays : 0);
  const avgCost =
    stats?.avg_cost_per_play ??
    (totalPlays > 0 ? state.total_cost / totalPlays : 0);
  const totalTokens =
    stats?.total_tokens ??
    state.agents.reduce((sum, a) => sum + a.total_tokens, 0);

  return (
    <section className="stats-section stats-summary">
      <div className="stats-tile-grid">
        <StatTile
          label="Plays"
          value={count(totalPlays)}
          sub={`${successful} ok / ${failed} failed`}
        />
        <StatTile label="Success" value={decimalPct(successRate)} sub="full session" />
        <StatTile
          label="Cost"
          value={money(stats?.total_cost ?? state.total_cost)}
          sub={`${money(avgCost)} / play`}
        />
        <StatTile label="Tokens" value={count(totalTokens)} sub="agent total" />
        <StatTile
          label="Avg Duration"
          value={duration(stats?.avg_duration_seconds ?? 0)}
          sub="completed plays"
        />
        <StatTile
          label="Failure Streak"
          value={count(state.same_type_failure_streak)}
          sub={state.last_play_type ? formatPlayType(state.last_play_type) : "none"}
        />
      </div>
    </section>
  );
}

function AlignmentSection({
  graph,
}: {
  graph: ProjectGraph | null | undefined;
}): React.ReactElement {
  const epics = graph?.epics ?? [];
  return (
    <section className="stats-section">
      <h2>Alignment</h2>
      <div className="stats-bars">
        {(!graph || graph.epics.length === 0) && (
          <div className="stats-empty">Graph not initialised</div>
        )}
        {epics.map((epic) => {
          const label = epicLabel(epic);
          return (
            <div
              key={epic.bead_id || label}
              className="cluster-bar stats-bar-row"
            >
              <span className="cluster-name" title={label}>
                {label}
              </span>
              <div className="cluster-track">
                <div
                  className={`cluster-fill ${alignmentClass(epic.closure_ratio)}`}
                  style={{ width: pct(epic.closure_ratio) }}
                />
              </div>
              <span className="cluster-score">{epic.closure_ratio.toFixed(2)}</span>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function EpicsSection({
  graph,
}: {
  graph: ProjectGraph | null | undefined;
}): React.ReactElement {
  if (!graph) {
    return (
      <section className="stats-section">
        <h2>Epics</h2>
        <div className="stats-empty">Graph not initialised</div>
      </section>
    );
  }
  return (
    <section className="stats-section">
      <h2>Epics</h2>
      <div className="stats-epic-global">
        {`${pct(graph.global_closure_ratio)} complete · ${graph.tasks_ready} / ${graph.tasks_total} tasks ready`}
      </div>
      <div className="stats-bars">
        {graph.epics.map((epic) => {
          const label = epicLabel(epic);
          return (
            <div key={epic.bead_id || label} className="epic-row stats-bar-row">
              <span className="epic-name" title={label}>
                {label}
              </span>
              <div className="epic-track">
                <div
                  className={`epic-fill ${closureClass(epic.closure_ratio)}`}
                  style={{ width: pct(epic.closure_ratio) }}
                />
              </div>
              <span className="epic-score">{`${epic.closed_tasks}/${epic.total_tasks}`}</span>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function PlayTypesSection({
  rows,
}: {
  rows: PlayTypeStatsSnapshot[];
}): React.ReactElement {
  return (
    <section className="stats-section stats-table-section">
      <h2>Plays by Type</h2>
      {rows.length === 0 ? (
        <div className="stats-empty">No completed plays</div>
      ) : (
        <table className="stats-table">
          <thead>
            <tr>
              <th>Play</th>
              <th>Total</th>
              <th>OK</th>
              <th>Fail</th>
              <th>Rate</th>
              <th>Cost</th>
              <th>Avg</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.play_type}>
                <td>{formatPlayType(row.play_type)}</td>
                <td>{count(row.total)}</td>
                <td>{count(row.successful)}</td>
                <td>{count(row.failed)}</td>
                <td>{decimalPct(row.success_rate)}</td>
                <td>{money(row.total_cost)}</td>
                <td>{duration(row.avg_duration_seconds)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function AgentsSection({
  agents,
}: {
  agents: AgentSnapshot[];
}): React.ReactElement {
  return (
    <section className="stats-section stats-table-section">
      <h2>Agents</h2>
      {agents.length === 0 ? (
        <div className="stats-empty">No agents</div>
      ) : (
        <table className="stats-table">
          <thead>
            <tr>
              <th>Agent</th>
              <th>Class</th>
              <th>Status</th>
              <th>OK</th>
              <th>Fail</th>
              <th>Rate</th>
              <th>Cost</th>
            </tr>
          </thead>
          <tbody>
            {agents.map((agent) => (
              <tr key={agent.agent_id}>
                <td>{shortAgentName(agent)}</td>
                <td>{formatAgentClass(agent)}</td>
                <td>{agent.status}</td>
                <td>{count(agent.tasks_completed)}</td>
                <td>{count(agent.tasks_failed)}</td>
                <td>{decimalPct(agentSuccessRate(agent))}</td>
                <td>{money(agent.total_cost)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

export default function StatsStage(): React.ReactElement {
  const { state, visible, insets } = useStatsStageState();

  const style: React.CSSProperties = {};
  if (insets) {
    const styleWithVars = style as React.CSSProperties &
      Record<`--${string}`, string>;
    styleWithVars["--stats-inset-top"] = `${insets.top}px`;
    styleWithVars["--stats-inset-left"] = `${insets.left}px`;
    styleWithVars["--stats-inset-right"] = `${insets.right}px`;
    styleWithVars["--stats-inset-bottom"] = `${insets.bottom}px`;
  }

  return (
    <div id="stats-stage" hidden={!visible} style={style}>
      {state && (
        <div className="stats-panel">
          <SummarySection state={state} />
          <div className="stats-grid">
            <AlignmentSection graph={state.graph} />
            <EpicsSection graph={state.graph} />
          </div>
          <PlayTypesSection rows={state.stats?.by_play_type ?? []} />
          <AgentsSection agents={state.agents} />
        </div>
      )}
    </div>
  );
}
