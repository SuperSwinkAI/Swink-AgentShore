import React, { useState } from "react";
import { createNotifyStore } from "../notifyStore";

export type ViewMode = "office" | "kanban" | "stats";

interface TabDef {
  mode: ViewMode;
  label: string;
  sub: string;
}

const TAB_DEFS: TabDef[] = [
  { mode: "office", label: "Office", sub: "floorplan" },
  { mode: "kanban", label: "Kanban", sub: "issues" },
  { mode: "stats", label: "Stats", sub: "metrics" },
];

const store = createNotifyStore<ViewMode | null>(null);

export function notifyStageTabsMode(mode: ViewMode): void {
  store.notify(mode);
}

export interface StageTabsProps {
  initial?: ViewMode;
  onChange?: (mode: ViewMode) => void;
}

export default function StageTabs({
  initial = "office",
  onChange,
}: StageTabsProps): React.ReactElement {
  const storeMode = store.use();
  const [localInitial] = useState(initial);
  const current = storeMode ?? localInitial;

  function handleClick(mode: ViewMode): void {
    if (mode === current) return;
    store.notify(mode);
    onChange?.(mode);
  }

  return (
    <>
      {TAB_DEFS.map((def) => {
        const isActive = def.mode === current;
        return (
          <button
            key={def.mode}
            type="button"
            className={`stage-tab${isActive ? " active" : ""}`}
            data-mode={def.mode}
            role="tab"
            aria-selected={isActive}
            onClick={() => handleClick(def.mode)}
          >
            <span className="stage-tab-label">{def.label}</span>
            <span className="stage-tab-sub">{def.sub}</span>
          </button>
        );
      })}
    </>
  );
}
