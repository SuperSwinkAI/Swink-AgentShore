import React, { useEffect, useState } from "react";
import type { StateUpdate } from "../types";
import { formatPolicyMode } from "../format";

interface TopBarHudState {
  sessionState: StateUpdate["session_state"];
  totalPlays: number;
  policyMode: StateUpdate["policy_mode"];
  issueSummary: string;
}

const listeners = new Set<(s: TopBarHudState) => void>();
let latestState: TopBarHudState | null = null;

export function notifyTopBarHud(state: StateUpdate): void {
  // Prefer the server's work_availability summary when it carries real
  // counts. The bridge has historically populated 0/0 even with hundreds
  // of issues in state.open_issues; fall back to the raw issue list size
  // in that case so the chip reflects what the user sees on the KANBAN
  // tab. KanbanStage's deriveColumns uses state.open_issues, so this
  // matches the same source of truth.
  const availability = state.work_availability ?? state.issue_availability;
  const openFromState = state.open_issues?.length ?? 0;
  const totalReported = availability?.github_open_issue_count ?? 0;
  const workableReported = availability?.workable_issue_count ?? 0;
  let issueSummary = "";
  if (totalReported > 0 || workableReported > 0) {
    issueSummary = ` · Issues: ${totalReported}/${workableReported}`;
  } else if (openFromState > 0) {
    issueSummary = ` · Issues: ${openFromState} open`;
  }
  const next: TopBarHudState = {
    sessionState: state.session_state,
    totalPlays: state.total_plays,
    policyMode: state.policy_mode,
    issueSummary,
  };
  latestState = next;
  listeners.forEach((fn) => fn(next));
}

function useTopBarHudState(): TopBarHudState | null {
  const [state, setState] = useState<TopBarHudState | null>(latestState);
  useEffect(() => {
    listeners.add(setState);
    if (latestState) setState(latestState);
    return () => {
      listeners.delete(setState);
    };
  }, []);
  return state;
}

export function TopBarHud(): React.ReactElement {
  const state = useTopBarHudState();
  const sessionState = state?.sessionState ?? "initializing";
  const totalPlays = state?.totalPlays ?? 0;
  const policyMode = state?.policyMode ?? "learning";
  const issueSummary = state?.issueSummary ?? "";

  const playsText = `Plays: ${totalPlays}${issueSummary} · Policy: ${formatPolicyMode(policyMode)}`;

  return (
    <>
      <div className="hud-chip session-state">
        <span className={`state-dot ${sessionState}`} id="state-dot" />
        <span id="session-label">{sessionState.toUpperCase()}</span>
      </div>
      <span id="plays-count" className="hud-chip">
        {playsText}
      </span>
    </>
  );
}
