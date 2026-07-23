import React from "react";
import type { StateUpdate } from "../types";
import { createNotifyStore } from "../notifyStore";

interface TopBarHudState {
  sessionState: StateUpdate["session_state"];
  totalPlays: number;
  openIssueCount: number;
}

const store = createNotifyStore<TopBarHudState | null>(null);

export function notifyTopBarHud(state: StateUpdate): void {
  // Bridge can report 0/0 availability despite a populated open_issues; fall back to list size.
  const availability = state.work_availability;
  const openFromState = state.open_issues?.length ?? 0;
  const totalReported = availability?.github_open_issue_count ?? 0;
  const openIssueCount = totalReported > 0 ? totalReported : openFromState;
  store.notify({
    sessionState: state.session_state,
    totalPlays: state.total_plays,
    openIssueCount,
  });
}

export function TopBarHud(): React.ReactElement {
  const state = store.use();
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
