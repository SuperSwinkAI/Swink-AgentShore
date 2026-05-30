import React, { useEffect, useState } from "react";
import type {
  ActivePlay,
  AgentSnapshot,
  PlayEvent,
  StateUpdate,
} from "../types";
import { formatPlayType } from "../format";

type DisplayMode =
  | { kind: "inactive" }
  | { kind: "single"; play: ActivePlay; startMs: number }
  | {
      kind: "summary";
      count: number;
      summary: string;
      startMs: number;
    };

const listeners = new Set<(mode: DisplayMode) => void>();
let latestMode: DisplayMode = { kind: "inactive" };

function broadcast(mode: DisplayMode): void {
  latestMode = mode;
  listeners.forEach((fn) => fn(mode));
}

function parseStart(startedAt: string | null | undefined): number {
  const parsed = Date.parse(startedAt ?? new Date().toISOString());
  return Number.isNaN(parsed) ? Date.now() : parsed;
}

function summarizeRunningPlayTypes(running: ActivePlay[]): string {
  const byType = new Map<string, number>();
  for (const current of running) {
    byType.set(current.play_type, (byType.get(current.play_type) ?? 0) + 1);
  }
  return [...byType.entries()]
    .sort(
      (a, b) =>
        b[1] - a[1] || formatPlayType(a[0]).localeCompare(formatPlayType(b[0])),
    )
    .map(([playType, count]) => `${formatPlayType(playType)} x${count}`)
    .join(" · ");
}

function modeForActivePlay(activePlay: ActivePlay | null): DisplayMode {
  if (!activePlay) return { kind: "inactive" };
  return {
    kind: "single",
    play: activePlay,
    startMs: parseStart(activePlay.started_at),
  };
}

export function notifyPlayBarUpdate(state: StateUpdate): void {
  if (state.active_play) {
    broadcast(modeForActivePlay(state.active_play));
    return;
  }

  const running: Array<{ agent: AgentSnapshot; current: ActivePlay }> = [];
  for (const agent of state.agents) {
    if (!agent.current_play || agent.status === "terminated") continue;
    running.push({ agent, current: agent.current_play });
  }

  if (running.length === 0) {
    broadcast({ kind: "inactive" });
    return;
  }

  if (running.length === 1) {
    const { agent, current } = running[0];
    broadcast(
      modeForActivePlay({
        play_type: current.play_type,
        agent_id: agent.agent_id,
        play_id: current.play_id,
        issue_number: current.issue_number,
        pr_number: current.pr_number,
        branch: current.branch,
        started_at: current.started_at ?? new Date().toISOString(),
      }),
    );
    return;
  }

  const startedTimes = running
    .map((entry) =>
      entry.current.started_at ? Date.parse(entry.current.started_at) : NaN,
    )
    .filter((time) => !Number.isNaN(time));
  const startMs = startedTimes.length > 0 ? Math.min(...startedTimes) : Date.now();

  broadcast({
    kind: "summary",
    count: running.length,
    summary: summarizeRunningPlayTypes(running.map((entry) => entry.current)),
    startMs,
  });
}

export function notifyPlayBarEvent(event: PlayEvent): void {
  if (event.status === "started") {
    broadcast(
      modeForActivePlay({
        play_type: event.play_type,
        agent_id: event.agent_id,
        play_id: event.play_id ?? null,
        issue_number: event.issue_number ?? null,
        pr_number: event.pr_number ?? null,
        branch: event.branch ?? null,
        started_at: event.started_at ?? new Date().toISOString(),
        trigger_agent_id: event.trigger_agent_id ?? null,
        trigger_agent_type: event.trigger_agent_type ?? null,
        trigger_error_class: event.trigger_error_class ?? null,
      }),
    );
  } else {
    broadcast({ kind: "inactive" });
  }
}

export function notifyPlayBarActivePlay(activePlay: ActivePlay | null): void {
  broadcast(modeForActivePlay(activePlay));
}

export function notifyPlayBarClear(): void {
  broadcast({ kind: "inactive" });
}

function formatElapsed(ms: number): string {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${m}:${sec.toString().padStart(2, "0")}`;
}

function useMode(): DisplayMode {
  const [mode, setMode] = useState<DisplayMode>(latestMode);
  useEffect(() => {
    listeners.add(setMode);
    setMode(latestMode);
    return () => {
      listeners.delete(setMode);
    };
  }, []);
  return mode;
}

function useElapsed(startMs: number | null): string {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (startMs === null) return;
    setNow(Date.now());
    const handle = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(handle);
  }, [startMs]);
  if (startMs === null) return "";
  return formatElapsed(now - startMs);
}

function agentLabelForSingle(play: ActivePlay): string {
  if (play.agent_id) return `Agent: ${play.agent_id.slice(0, 8)}`;
  if (play.trigger_agent_id) {
    const reason = play.trigger_error_class
      ? ` (${formatPlayType(play.trigger_error_class)})`
      : "";
    return `Cooldown source: ${play.trigger_agent_id.slice(0, 8)}${reason}`;
  }
  return "";
}

export default function PlayBar(): React.ReactElement {
  const mode = useMode();
  const startMs = mode.kind === "inactive" ? null : mode.startMs;
  const elapsed = useElapsed(startMs);

  let typeLabel: string;
  let agentLabel: string;
  let inactive: boolean;

  switch (mode.kind) {
    case "inactive":
      typeLabel = "No active play";
      agentLabel = "";
      inactive = true;
      break;
    case "single":
      typeLabel = formatPlayType(mode.play.play_type);
      agentLabel = agentLabelForSingle(mode.play);
      inactive = false;
      break;
    case "summary":
      typeLabel = `${mode.count} active plays`;
      agentLabel = mode.summary;
      inactive = false;
      break;
  }

  return (
    <div id="play-bar" className={inactive ? "inactive" : ""}>
      <span className="play-label" id="play-type-label">
        {typeLabel}
      </span>
      <span className="play-agent" id="play-agent-label">
        {agentLabel}
      </span>
      <span className="play-elapsed" id="play-elapsed">
        {inactive ? "" : elapsed}
      </span>
    </div>
  );
}
