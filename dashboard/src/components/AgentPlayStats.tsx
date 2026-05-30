import React, { useEffect, useState } from "react";
import type { PlayEvent } from "../types";

interface TypeBucket {
  ok: number;
  failed: number;
}

interface AgentStats {
  ok: number;
  failed: number;
  byType: Map<string, TypeBucket>;
}

export interface AgentStatsView {
  total: number;
  ok: number;
  failed: number;
  byType: Array<{
    playType: string;
    ok: number;
    failed: number;
    total: number;
  }>;
}

const EMPTY_VIEW: AgentStatsView = { total: 0, ok: 0, failed: 0, byType: [] };

// React-port companion to dashboard/src/hud/agentPlayStats.ts. The two
// stores are intentionally independent: the imperative module is still
// driven by bootstrapDashboard.ts for the bridge SPA, while this React
// store is driven by the Dashboard.tsx message router as it lands.
const stats = new Map<string, AgentStats>();

// Monotonically increasing revision so subscribers (the hook) can detect
// that the underlying store changed without re-emitting per-agent diffs.
let revision = 0;
const listeners = new Set<(rev: number) => void>();

function ensureAgent(agentId: string): AgentStats {
  let entry = stats.get(agentId);
  if (!entry) {
    entry = { ok: 0, failed: 0, byType: new Map() };
    stats.set(agentId, entry);
  }
  return entry;
}

function recordEvent(event: PlayEvent): void {
  if (!event.agent_id) return;
  if (event.status !== "completed" && event.status !== "failed") return;

  const entry = ensureAgent(event.agent_id);
  const bucket = entry.byType.get(event.play_type) ?? { ok: 0, failed: 0 };

  if (event.status === "completed") {
    entry.ok += 1;
    bucket.ok += 1;
  } else {
    entry.failed += 1;
    bucket.failed += 1;
  }

  entry.byType.set(event.play_type, bucket);
}

function bump(): void {
  revision += 1;
  listeners.forEach((fn) => fn(revision));
}

export function notifyAgentPlayStatsEvent(event: PlayEvent): void {
  recordEvent(event);
  bump();
}

export function notifyAgentPlayStatsReplay(events: PlayEvent[]): void {
  stats.clear();
  for (const event of events) {
    recordEvent(event);
  }
  bump();
}

export function notifyAgentPlayStatsReset(): void {
  stats.clear();
  bump();
}

export function getAgentPlayStats(agentId: string): AgentStatsView {
  const entry = stats.get(agentId);
  if (!entry) return EMPTY_VIEW;

  const byType = Array.from(entry.byType.entries())
    .map(([playType, bucket]) => ({
      playType,
      ok: bucket.ok,
      failed: bucket.failed,
      total: bucket.ok + bucket.failed,
    }))
    .sort((a, b) => b.total - a.total);

  return {
    total: entry.ok + entry.failed,
    ok: entry.ok,
    failed: entry.failed,
    byType,
  };
}

export function useAgentPlayStats(agentId: string | null): AgentStatsView {
  const [, setRev] = useState(revision);
  useEffect(() => {
    listeners.add(setRev);
    return () => {
      listeners.delete(setRev);
    };
  }, []);
  if (!agentId) return EMPTY_VIEW;
  return getAgentPlayStats(agentId);
}

export interface AgentPlayStatsProps {
  agentId: string | null;
  children: (view: AgentStatsView) => React.ReactNode;
}

/**
 * Non-visual wrapper that subscribes to the React-side play-stats store
 * and forwards the resolved view to its render-prop child. Use this when
 * you need stats inside a tree that doesn't already call the hook
 * (the SidePanel reads from the imperative store directly).
 */
export function AgentPlayStats({
  agentId,
  children,
}: AgentPlayStatsProps): React.ReactElement {
  const view = useAgentPlayStats(agentId);
  return <>{children(view)}</>;
}

export default AgentPlayStats;
