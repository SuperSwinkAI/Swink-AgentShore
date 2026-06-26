import React, { useEffect, useState } from "react";

// React port of `dashboard/src/hud/bootstrapModal.ts`. Host wires up
// `notifyBootstrapModal`; the component re-renders + ticks elapsed-time via state.

const PHASE_LABELS: Record<string, string> = {
  init_datastore: "Opening session database",
  reset_session_scoped_tables: "Resetting session tables",
  init_manager: "Wiring the agent manager",
  init_github: "Probing GitHub",
  init_executor: "Building the play executor",
  init_metrics: "Bringing metrics online",
  init_ppo_selector: "Warming up the PPO policy",
  create_session: "Recording the session",
  clear_beads_in_progress: "Clearing stale beads work",
  install_skills: "Installing skill templates",
  fetch_issues: "Snapshotting open issues + PRs",
  ensure_labels: "Ensuring GitHub labels exist",
  load_learnings: "Loading session learnings",
  queue_agent_instantiation: "Queuing bootstrap agents",
};

export interface BootstrapModalState {
  phase: string | null;
  startedAt: number | null;
}

const listeners = new Set<(s: BootstrapModalState) => void>();
let latestState: BootstrapModalState = { phase: null, startedAt: null };

export function notifyBootstrapModal(state: BootstrapModalState): void {
  latestState = { phase: state.phase, startedAt: state.startedAt };
  listeners.forEach((fn) => fn(latestState));
}

function useBootstrapModalState(): BootstrapModalState {
  const [state, setState] = useState<BootstrapModalState>(latestState);
  useEffect(() => {
    listeners.add(setState);
    setState(latestState);
    return () => {
      listeners.delete(setState);
    };
  }, []);
  return state;
}

export function BootstrapModal(): React.ReactElement {
  const { phase, startedAt } = useBootstrapModalState();
  const visible = phase !== null;

  // Tick every 250ms only while visible so the "Ns elapsed" line advances live.
  const [, forceTick] = useState(0);
  useEffect(() => {
    if (!visible) return undefined;
    const handle = window.setInterval(() => {
      forceTick((n) => n + 1);
    }, 250);
    return () => {
      window.clearInterval(handle);
    };
  }, [visible]);

  const phaseLabel = phase ? (PHASE_LABELS[phase] ?? phase) : "Initialising";
  const elapsedText =
    startedAt !== null
      ? `${Math.floor((Date.now() - startedAt) / 1000)}s elapsed`
      : "";

  return (
    <div id="bootstrap-modal" className={visible ? "visible" : undefined}>
      <div className="modal-box">
        <div className="modal-title">Starting session…</div>
        <div className="modal-reason" id="bootstrap-phase">
          {phaseLabel}
        </div>
        <div className="modal-subreason" id="bootstrap-elapsed">
          {elapsedText}
        </div>
      </div>
    </div>
  );
}

export default BootstrapModal;
