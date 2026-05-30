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

const stats = new Map<string, AgentStats>();

const EMPTY_VIEW: AgentStatsView = { total: 0, ok: 0, failed: 0, byType: [] };

function ensureAgent(agentId: string): AgentStats {
  let entry = stats.get(agentId);
  if (!entry) {
    entry = { ok: 0, failed: 0, byType: new Map() };
    stats.set(agentId, entry);
  }
  return entry;
}

export function recordPlayEvent(event: PlayEvent): void {
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

export function replayPlayStats(events: PlayEvent[]): void {
  stats.clear();
  for (const event of events) {
    recordPlayEvent(event);
  }
}

export function resetPlayStats(): void {
  stats.clear();
}

export function getPlayStats(agentId: string): AgentStatsView {
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
