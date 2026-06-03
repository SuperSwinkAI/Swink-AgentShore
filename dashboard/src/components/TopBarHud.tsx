import React, { useEffect, useState } from "react";
import type { StateUpdate } from "../types";

interface TopBarHudState {
  sessionState: StateUpdate["session_state"];
  totalPlays: number;
  openIssueCount: number;
}

const listeners = new Set<(s: TopBarHudState) => void>();
let latestState: TopBarHudState | null = null;

export function notifyTopBarHud(state: StateUpdate): void {
  // Prefer the server's open-issue count when available. The bridge has
  // historically populated 0/0 availability summaries even with hundreds
  // of issues in state.open_issues; fall back to the raw issue list size
  // when the server did not report any open issues.
  const availability = state.work_availability ?? state.issue_availability;
  const openFromState = state.open_issues?.length ?? 0;
  const totalReported = availability?.github_open_issue_count ?? 0;
  const openIssueCount = totalReported > 0 ? totalReported : openFromState;
  const next: TopBarHudState = {
    sessionState: state.session_state,
    totalPlays: state.total_plays,
    openIssueCount,
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
  const openIssueCount = state?.openIssueCount ?? 0;

  const playsText = `Plays: ${totalPlays} · Open Issues : ${openIssueCount}`;

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
