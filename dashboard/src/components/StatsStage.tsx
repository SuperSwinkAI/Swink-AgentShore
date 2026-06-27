import React, { useEffect, useState } from "react";
import type {
  AgentSnapshot,
  EpicStatus,
  PlayTypeStatsSnapshot,
  ProjectGraph,
  StateUpdate,
} from "../types";
import {
  AGENT_REGISTRY,
  AGENT_TYPES,
  agentLabel,
} from "../agentRegistry";
import {
  formatAgentClass,
  formatPlayType,
  shortAgentName,
} from "../format";

// React port of `dashboard/src/views/stats/index.ts`; the notify* functions
// mirror the imperative module's inputs. Rendering reproduces its DOM exactly
// so the existing `.stats-*` rules in dashboard.css style this tree identically.

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
  concurrencySamples: ConcurrencySample[];
  concurrencySessionStartedAtMs: number | null;
  nowMs: number;
}

export const CONCURRENCY_MAX_WINDOW_MS = 3 * 60 * 60 * 1000;
export const CONCURRENCY_RECALC_INTERVAL_MS = 60 * 1000;
const CONCURRENCY_SAMPLE_BUCKET_MS = 1000;
const CHART_WIDTH = 960;
const CHART_HEIGHT = 280;
const CHART_LEFT = 46;
const CHART_RIGHT = 14;
const CHART_TOP = 18;
const CHART_BOTTOM = 34;
const CHART_PLOT_WIDTH = CHART_WIDTH - CHART_LEFT - CHART_RIGHT;
const CHART_PLOT_HEIGHT = CHART_HEIGHT - CHART_TOP - CHART_BOTTOM;
const FALLBACK_SERIES_COLORS = [
  "#7dd3fc",
  "#c084fc",
  "#fb7185",
  "#a3e635",
  "#facc15",
  "#38bdf8",
  "#f97316",
  "#34d399",
];

export interface ConcurrencySample {
  timestampMs: number;
  counts: Record<string, number>;
}

export interface StackedPoint {
  timestampMs: number;
  x: number;
  y0: number;
  y1: number;
  value: number;
}

export interface HarnessSeries {
  agentType: string;
  label: string;
  color: string;
  total: number;
  points: StackedPoint[];
}

export interface ConcurrencyChartModel {
  series: HarnessSeries[];
  totalLinePoints: Array<{ x: number; y: number }>;
  currentTotal: number;
  peakTotal: number;
  yMax: number;
  windowDurationMs: number;
  windowStartMs: number;
  windowEndMs: number;
}

const listeners = new Set<(s: StatsStageState) => void>();
let concurrencySessionId: string | null = null;
let concurrencySessionStartedAtMs: number | null = null;
let concurrencySamples: ConcurrencySample[] = [];
let latestState: StatsStageState = {
  state: null,
  visible: false,
  insets: null,
  concurrencySamples: [],
  concurrencySessionStartedAtMs: null,
  nowMs: Date.now(),
};

function broadcast(next: StatsStageState): void {
  latestState = next;
  listeners.forEach((fn) => fn(next));
}

export function notifyStatsStageUpdate(state: StateUpdate): void {
  const nowMs = Date.now();
  const nextSamples = updateConcurrencyHistory(state, nowMs);
  broadcast({
    ...latestState,
    state,
    concurrencySamples: nextSamples,
    concurrencySessionStartedAtMs,
    nowMs,
  });
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
  useEffect(() => {
    const handle = window.setInterval(() => {
      const nowMs = Date.now();
      concurrencySamples = pruneConcurrencySamples(
        concurrencySamples,
        nowMs,
        CONCURRENCY_MAX_WINDOW_MS,
      );
      broadcast({ ...latestState, concurrencySamples, nowMs });
    }, CONCURRENCY_RECALC_INTERVAL_MS);
    return () => window.clearInterval(handle);
  }, []);
  return state;
}

export function deriveBusyAgentCounts(
  agents: AgentSnapshot[],
): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const agent of agents) {
    if (agent.status !== "busy") continue;
    const agentType = String(agent.agent_type);
    counts[agentType] = (counts[agentType] ?? 0) + 1;
  }
  return counts;
}

export function pruneConcurrencySamples(
  samples: ConcurrencySample[],
  nowMs: number,
  windowMs = CONCURRENCY_MAX_WINDOW_MS,
): ConcurrencySample[] {
  const startMs = nowMs - windowMs;
  return samples.filter((sample) => sample.timestampMs >= startMs);
}

export function orderConcurrencyAgentTypes(samples: ConcurrencySample[]): string[] {
  const seen = new Set<string>();
  for (const sample of samples) {
    for (const agentType of Object.keys(sample.counts)) {
      seen.add(agentType);
    }
  }
  const known = AGENT_TYPES.filter((agentType) => seen.has(agentType));
  const unknown = [...seen]
    .filter((agentType) => !(agentType in AGENT_REGISTRY))
    .sort((a, b) => a.localeCompare(b));
  return [...known, ...unknown];
}

export function colorForConcurrencyAgentType(agentType: string): string {
  const known = (AGENT_REGISTRY as Record<string, { colorFill: string } | undefined>)[
    agentType
  ];
  if (known) return known.colorFill;
  let hash = 0;
  for (let i = 0; i < agentType.length; i += 1) {
    hash = (hash * 31 + agentType.charCodeAt(i)) >>> 0;
  }
  return FALLBACK_SERIES_COLORS[hash % FALLBACK_SERIES_COLORS.length];
}

function sampleCountsEqual(
  left: Record<string, number>,
  right: Record<string, number>,
): boolean {
  const keys = new Set([...Object.keys(left), ...Object.keys(right)]);
  for (const key of keys) {
    if ((left[key] ?? 0) !== (right[key] ?? 0)) return false;
  }
  return true;
}

export function appendConcurrencySample(
  samples: ConcurrencySample[],
  sample: ConcurrencySample,
  nowMs: number,
  windowMs = CONCURRENCY_MAX_WINDOW_MS,
  bucketMs = CONCURRENCY_SAMPLE_BUCKET_MS,
): ConcurrencySample[] {
  const pruned = pruneConcurrencySamples(samples, nowMs, windowMs);
  const last = pruned.at(-1);
  if (!last) return [sample];
  const lastBucket = Math.floor(last.timestampMs / bucketMs);
  const sampleBucket = Math.floor(sample.timestampMs / bucketMs);
  if (lastBucket === sampleBucket) {
    if (sampleCountsEqual(last.counts, sample.counts)) return pruned;
    return [...pruned.slice(0, -1), sample];
  }
  return [...pruned, sample];
}

export function resolveConcurrencyWindowMs(
  samples: ConcurrencySample[],
  nowMs: number,
  maxWindowMs = CONCURRENCY_MAX_WINDOW_MS,
): number {
  const oldestTimestampMs = Math.min(nowMs, ...samples.map((sample) => sample.timestampMs));
  return Math.min(maxWindowMs, Math.max(0, nowMs - oldestTimestampMs));
}

export function formatConcurrencyWindowDuration(windowMs: number): string {
  if (windowMs < 60 * 1000) return "<1m";
  const totalMinutes = Math.max(1, Math.round(windowMs / (60 * 1000)));
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours === 0) return `${minutes}m`;
  if (minutes === 0) return `${hours}h`;
  return `${hours}h ${minutes}m`;
}

export function buildConcurrencyChartModel(
  samples: ConcurrencySample[],
  nowMs: number,
  maxWindowMs = CONCURRENCY_MAX_WINDOW_MS,
  sessionStartedAtMs?: number | null,
): ConcurrencyChartModel {
  const windowEndMs = nowMs;
  const sortedSamples = samples.slice().sort((a, b) => a.timestampMs - b.timestampMs);
  const sessionSamples =
    sessionStartedAtMs === undefined || sessionStartedAtMs === null
      ? sortedSamples
      : [{ timestampMs: sessionStartedAtMs, counts: {} }, ...sortedSamples];
  const windowDurationMs = resolveConcurrencyWindowMs(
    sessionSamples,
    nowMs,
    maxWindowMs,
  );
  const windowStartMs = windowEndMs - windowDurationMs;
  const orderedSamples = sortedSamples
    .filter((sample) => sample.timestampMs >= windowStartMs)
    .slice()
    .sort((a, b) => a.timestampMs - b.timestampMs);
  const agentTypes = orderConcurrencyAgentTypes(orderedSamples);
  const totals = orderedSamples.map((sample) =>
    agentTypes.reduce((sum, agentType) => sum + (sample.counts[agentType] ?? 0), 0),
  );
  const peakTotal = Math.max(0, ...totals);
  const yMax = Math.max(1, Math.ceil(peakTotal));

  const xForTimestamp = (timestampMs: number): number => {
    if (windowEndMs <= windowStartMs) return CHART_LEFT;
    const bounded = Math.min(Math.max(timestampMs, windowStartMs), windowEndMs);
    const pctOfWindow = (bounded - windowStartMs) / (windowEndMs - windowStartMs);
    return CHART_LEFT + pctOfWindow * CHART_PLOT_WIDTH;
  };
  const yForValue = (value: number): number =>
    CHART_TOP + CHART_PLOT_HEIGHT - (value / yMax) * CHART_PLOT_HEIGHT;

  const runningStacks = orderedSamples.map(() => 0);
  const series = agentTypes
    .map((agentType) => {
      const points = orderedSamples.map((sample, index) => {
        const value = sample.counts[agentType] ?? 0;
        const y0Value = runningStacks[index];
        runningStacks[index] += value;
        return {
          timestampMs: sample.timestampMs,
          x: xForTimestamp(sample.timestampMs),
          y0: yForValue(y0Value),
          y1: yForValue(y0Value + value),
          value,
        };
      });
      return {
        agentType,
        label: agentLabel(agentType),
        color: colorForConcurrencyAgentType(agentType),
        total: points.reduce((sum, point) => sum + point.value, 0),
        points,
      };
    })
    .filter((entry) => entry.total > 0);

  const totalLinePoints = orderedSamples.map((sample) => ({
    x: xForTimestamp(sample.timestampMs),
    y: yForValue(
      agentTypes.reduce((sum, agentType) => sum + (sample.counts[agentType] ?? 0), 0),
    ),
  }));

  return {
    series,
    totalLinePoints,
    currentTotal: totals.at(-1) ?? 0,
    peakTotal,
    yMax,
    windowDurationMs,
    windowStartMs,
    windowEndMs,
  };
}

function timestampMsForState(state: StateUpdate, fallbackMs: number): number {
  const parsed = state.timestamp ? Date.parse(state.timestamp) : NaN;
  return Number.isFinite(parsed) ? parsed : fallbackMs;
}

function updateConcurrencyHistory(
  state: StateUpdate,
  nowMs: number,
): ConcurrencySample[] {
  if (concurrencySessionId !== state.session_id) {
    concurrencySessionId = state.session_id;
    concurrencySessionStartedAtMs = null;
    concurrencySamples = [];
  }
  const sample = {
    timestampMs: timestampMsForState(state, nowMs),
    counts: deriveBusyAgentCounts(state.agents),
  };
  concurrencySessionStartedAtMs =
    concurrencySessionStartedAtMs === null
      ? sample.timestampMs
      : Math.min(concurrencySessionStartedAtMs, sample.timestampMs);
  concurrencySamples = appendConcurrencySample(concurrencySamples, sample, nowMs);
  return concurrencySamples;
}

export function resetStatsStageForTests(): void {
  concurrencySessionId = null;
  concurrencySessionStartedAtMs = null;
  concurrencySamples = [];
  latestState = {
    state: null,
    visible: false,
    insets: null,
    concurrencySamples: [],
    concurrencySessionStartedAtMs: null,
    nowMs: Date.now(),
  };
  listeners.clear();
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

function pointsAttr(points: Array<{ x: number; y: number }>): string {
  return points.map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ");
}

function bandPointsAttr(points: StackedPoint[]): string {
  const top = points.map((point) => ({ x: point.x, y: point.y1 }));
  const bottom = points
    .slice()
    .reverse()
    .map((point) => ({ x: point.x, y: point.y0 }));
  return pointsAttr([...top, ...bottom]);
}

function colorStyle(color: string): React.CSSProperties & Record<`--${string}`, string> {
  return { "--series-color": color };
}

function FleetConcurrencySection({
  samples,
  sessionStartedAtMs,
  nowMs,
}: {
  samples: ConcurrencySample[];
  sessionStartedAtMs: number | null;
  nowMs: number;
}): React.ReactElement {
  const model = buildConcurrencyChartModel(samples, nowMs, undefined, sessionStartedAtMs);
  const windowLabel = formatConcurrencyWindowDuration(model.windowDurationMs);
  const gridValues = Array.from({ length: model.yMax + 1 }, (_, index) => index);
  const yForGridValue = (value: number): number =>
    CHART_TOP + CHART_PLOT_HEIGHT - (value / model.yMax) * CHART_PLOT_HEIGHT;

  return (
    <section className="stats-section stats-concurrency-section">
      <div className="stats-concurrency-head">
        <h2>Fleet Concurrency</h2>
        <div className="stats-concurrency-meta" aria-label="Fleet concurrency summary">
          <span>{`current ${model.currentTotal} busy`}</span>
          <span>{`peak ${model.peakTotal}`}</span>
          <span>{`rolling ${windowLabel} window`}</span>
        </div>
      </div>

      {samples.length < 2 || model.series.length === 0 ? (
        <div className="stats-empty">Waiting for fleet history</div>
      ) : (
        <div className="stats-concurrency-chart">
          <svg
            className="stats-concurrency-svg"
            viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
            role="img"
            aria-label="Stacked busy agent count by harness over time"
          >
            {gridValues.map((value) => {
              const y = yForGridValue(value);
              return (
                <g key={value}>
                  <line
                    className="stats-concurrency-grid-line"
                    x1={CHART_LEFT}
                    y1={y}
                    x2={CHART_WIDTH - CHART_RIGHT}
                    y2={y}
                  />
                  <text
                    className="stats-concurrency-axis-label"
                    x={CHART_LEFT - 14}
                    y={y + 3}
                    textAnchor="end"
                  >
                    {value}
                  </text>
                </g>
              );
            })}
            <line
              className="stats-concurrency-axis"
              x1={CHART_LEFT}
              y1={CHART_TOP}
              x2={CHART_LEFT}
              y2={CHART_TOP + CHART_PLOT_HEIGHT}
            />
            <line
              className="stats-concurrency-axis"
              x1={CHART_LEFT}
              y1={CHART_TOP + CHART_PLOT_HEIGHT}
              x2={CHART_WIDTH - CHART_RIGHT}
              y2={CHART_TOP + CHART_PLOT_HEIGHT}
            />
            <text
              className="stats-concurrency-axis-label"
              x={CHART_LEFT}
              y={CHART_HEIGHT - 8}
            >
              {`-${windowLabel}`}
            </text>
            <text
              className="stats-concurrency-axis-label"
              x={CHART_WIDTH - CHART_RIGHT}
              y={CHART_HEIGHT - 8}
              textAnchor="end"
            >
              now
            </text>
            {model.series.map((series) => (
              <polygon
                key={series.agentType}
                className="stats-concurrency-band"
                points={bandPointsAttr(series.points)}
                style={colorStyle(series.color)}
              />
            ))}
            <polyline
              className="stats-concurrency-total-line"
              points={pointsAttr(model.totalLinePoints)}
            />
          </svg>
          <div className="stats-concurrency-legend" aria-label="Fleet concurrency legend">
            {model.series.map((series) => (
              <span key={series.agentType} className="stats-concurrency-legend-item">
                <span
                  className="stats-concurrency-swatch"
                  style={{ backgroundColor: series.color }}
                />
                {`${series.label} busy`}
              </span>
            ))}
            <span className="stats-concurrency-legend-item">
              <span className="stats-concurrency-swatch stats-concurrency-swatch-total" />
              total concurrency
            </span>
          </div>
        </div>
      )}
    </section>
  );
}

export default function StatsStage(): React.ReactElement {
  const {
    state,
    visible,
    insets,
    concurrencySamples,
    concurrencySessionStartedAtMs,
    nowMs,
  } = useStatsStageState();

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
          <FleetConcurrencySection
            samples={concurrencySamples}
            sessionStartedAtMs={concurrencySessionStartedAtMs}
            nowMs={nowMs}
          />
        </div>
      )}
    </div>
  );
}
