import type { AgentSnapshot } from "../../types";
import { AGENT_REGISTRY, AGENT_TYPES, agentLabel } from "../../agentRegistry";

// Pure math/formatting for the Fleet Concurrency chart in StatsStage.tsx.
// Kept free of React/store concerns so it can be unit tested in isolation.

export const CONCURRENCY_MAX_WINDOW_MS = 3 * 60 * 60 * 1000;
const CONCURRENCY_SAMPLE_BUCKET_MS = 1000;
export const CHART_WIDTH = 960;
export const CHART_HEIGHT = 280;
export const CHART_LEFT = 46;
export const CHART_RIGHT = 14;
export const CHART_TOP = 18;
const CHART_BOTTOM = 34;
const CHART_PLOT_WIDTH = CHART_WIDTH - CHART_LEFT - CHART_RIGHT;
export const CHART_PLOT_HEIGHT = CHART_HEIGHT - CHART_TOP - CHART_BOTTOM;
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

export function pointsAttr(points: Array<{ x: number; y: number }>): string {
  return points.map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ");
}

export function bandPointsAttr(points: StackedPoint[]): string {
  const top = points.map((point) => ({ x: point.x, y: point.y1 }));
  const bottom = points
    .slice()
    .reverse()
    .map((point) => ({ x: point.x, y: point.y0 }));
  return pointsAttr([...top, ...bottom]);
}
