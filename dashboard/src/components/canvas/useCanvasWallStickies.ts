import { useEffect, type RefObject } from "react";

import type { OfficeRenderer, WallSticky } from "../../engine/renderer";
import type { StateUpdate } from "../../types";
import { deriveColumns, PHASES } from "../../views/kanban/phase";

/**
 * In-office mural stickies — module-level notify so the React-port
 * Dashboard can pipe state_update payloads to the canvas's renderer
 * without exposing the OfficeRenderer through React state. Mirrors the
 * imperative bootstrapDashboard.ts:342 path that the React port replaced
 * but never re-wired.
 */
const stickyListeners = new Set<(stickies: WallSticky[]) => void>();
let cachedStickies: WallSticky[] = [];

function hashBeadId(beadId: string): number {
  let hash = 0;
  for (let i = 0; i < beadId.length; i++) {
    hash = (hash * 31 + beadId.charCodeAt(i)) >>> 0;
  }
  return (hash % 1_000_000) + 1;
}

export function notifyDashboardCanvasStickies(state: StateUpdate): void {
  const cols = deriveColumns(
    state.open_issues ?? [],
    state.agents ?? [],
    state.pull_requests ?? [],
    state.graph ?? null,
  );
  const stickies: WallSticky[] = [];
  for (const [sectionIndex, phase] of PHASES.entries()) {
    for (const card of cols[phase]) {
      const issueNumber = card.issue
        ? card.issue.issue_number
        : card.pr
          ? -card.pr.pr_number
          : card.task
            ? -hashBeadId(card.task.bead_id)
            : 0;
      stickies.push({ issueNumber, sectionIndex });
    }
  }
  cachedStickies = stickies;
  stickyListeners.forEach((fn) => fn(stickies));
}

/**
 * Wires the module-level sticky-notify channel (above) into this canvas
 * mount's OfficeRenderer, and applies any sticky payload that arrived
 * before this canvas mounted.
 */
export function useCanvasWallStickies(rendererRef: RefObject<OfficeRenderer | null>): void {
  useEffect(() => {
    const renderer = rendererRef.current;
    if (!renderer) return;

    const onStickies = (stickies: WallSticky[]): void => {
      renderer.setWallStickies(stickies);
    };
    stickyListeners.add(onStickies);
    // Apply any sticker payload that arrived before this canvas mounted.
    if (cachedStickies.length > 0) {
      renderer.setWallStickies(cachedStickies);
    }

    return () => {
      stickyListeners.delete(onStickies);
    };
    // rendererRef is a stable ref object populated before this effect runs;
    // intentionally runs once at mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
