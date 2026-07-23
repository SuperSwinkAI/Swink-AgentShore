import React from "react";
import type { ProjectGraph, StateUpdate } from "../types";
import { createNotifyStore } from "../notifyStore";

const MAX_EPICS_SHOWN = 3;

function closureClass(ratio: number): string {
  if (ratio >= 0.75) return "closure-high";
  if (ratio >= 0.4) return "closure-mid";
  return "closure-low";
}

function pct(ratio: number): string {
  return `${Math.round(ratio * 100)}%`;
}

interface EpicPanelState {
  graph: ProjectGraph | null;
}

const store = createNotifyStore<EpicPanelState>({ graph: null });

export function notifyEpicPanel(state: StateUpdate): void {
  store.notify({ graph: state.graph ?? null });
}

function GlobalBar({ graph }: { graph: ProjectGraph }): React.ReactElement {
  return (
    <div className="epic-global">
      <div className="epic-global-label">{`${pct(graph.global_closure_ratio)} complete`}</div>
      <div className="epic-track">
        <div
          className={`epic-fill ${closureClass(graph.global_closure_ratio)}`}
          style={{ width: pct(graph.global_closure_ratio) }}
        />
      </div>
      <div className="epic-tasks-ready">{`${graph.tasks_ready} / ${graph.tasks_total} tasks ready`}</div>
    </div>
  );
}

function EpicRows({ graph }: { graph: ProjectGraph }): React.ReactElement {
  const top = graph.epics.slice(0, MAX_EPICS_SHOWN);
  return (
    <div className="epic-list">
      {top.length === 0 ? (
        <div className="epic-empty">No epics</div>
      ) : (
        <>
          {top.map((epic) => (
            <div key={epic.bead_id} className="epic-row">
              <span className="epic-name" title={epic.title}>
                {epic.title}
              </span>
              <div className="epic-track">
                <div
                  className={`epic-fill ${closureClass(epic.closure_ratio)}`}
                  style={{ width: pct(epic.closure_ratio) }}
                />
              </div>
              <span className="epic-score">{`${epic.closed_tasks}/${epic.total_tasks}`}</span>
            </div>
          ))}
          {graph.epics.length > MAX_EPICS_SHOWN && (
            <div className="epic-more">{`+${graph.epics.length - MAX_EPICS_SHOWN} more`}</div>
          )}
        </>
      )}
    </div>
  );
}

export function EpicPanel(): React.ReactElement | null {
  const { graph } = store.use();
  if (!graph) return null;
  return (
    <div id="epic-section">
      <GlobalBar graph={graph} />
      <EpicRows graph={graph} />
    </div>
  );
}

export default EpicPanel;
