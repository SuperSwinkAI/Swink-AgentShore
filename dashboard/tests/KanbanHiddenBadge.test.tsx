/**
 * Piece C: the Kanban header "(N hidden)" badge.
 *
 * Open PRs whose base branch differs from the session target branch are filtered
 * out server-side; the dashboard surfaces only a count via
 * ``work_availability.pull_requests_hidden_count``. The badge must appear when
 * that count is > 0 and be absent at 0.
 *
 * Uses react-dom/client + act directly (no @testing-library/react), consistent
 * with the rest of the dashboard suite.
 */

import React from "react";
import { createRoot, type Root } from "react-dom/client";
import { act } from "react";
import { describe, it, expect, beforeEach, afterEach } from "vitest";

import KanbanStage, {
  notifyKanbanStateUpdate,
} from "../src/components/KanbanStage";
import type { StateUpdate, WorkAvailability } from "../src/types";

function makeState(hiddenCount: number): StateUpdate {
  const work_availability = {
    pull_requests_hidden_count: hiddenCount,
  } as unknown as WorkAvailability;
  return {
    type: "state_update",
    open_issues: [],
    agents: [],
    pull_requests: [],
    graph: null,
    work_availability,
  } as unknown as StateUpdate;
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("Kanban (N hidden) badge", () => {
  it("renders the badge when pull_requests_hidden_count > 0", () => {
    act(() => {
      root.render(<KanbanStage />);
    });
    act(() => {
      notifyKanbanStateUpdate(makeState(2));
    });
    const badge = container.querySelector(".km-hdr-hidden");
    expect(badge).not.toBeNull();
    expect(badge?.textContent).toContain("2 hidden");
  });

  it("omits the badge when the count is zero", () => {
    act(() => {
      root.render(<KanbanStage />);
    });
    act(() => {
      notifyKanbanStateUpdate(makeState(0));
    });
    expect(container.querySelector(".km-hdr-hidden")).toBeNull();
  });
});
