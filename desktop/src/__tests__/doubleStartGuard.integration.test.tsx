/**
 * Integration coverage for issue #582: Repeat (#561) and Quick Start
 * (#565) both walk the user into ``/starting``. Before this fix the
 * shared helper ``startSessionFromPersistedSetup`` dispatched
 * ``session.start`` AND then handed control to ``StartingProgressRoute``
 * which dispatched ``session.start`` a SECOND time, double-billing the
 * sidecar and risking duplicate orchestrator boots.
 *
 * These tests walk each entrypoint into a mocked ``/starting`` route
 * inside a ``MemoryRouter`` and assert that ``session.start`` is
 * invoked EXACTLY ONCE end-to-end. They specifically guard against the
 * regression class — the existing unit tests mock the helper at the
 * RPC seam and don't see the second dispatch from the route.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import type { ProgressNotificationParams } from "../services/sidecarEvents";
import type { ChooseProjectAdapter } from "../screens/ChooseProjectScreen";
import type { RecentEntry } from "../rpc/recentsClient";
import { SessionContext, type EsrPayload } from "../services/sessionContext";

type ProgressHandler = (params: ProgressNotificationParams) => void;

// All sidecar RPC seams used by the helper + the /starting route are
// mocked at the module boundary so the test stays hermetic.
const {
  selectProjectMock,
  inspectProjectMock,
  startSessionMock,
  subscribeProgressMock,
  setBudgetMock,
  listRecentsMock,
  touchRecentMock,
  removeRecentMock,
} = vi.hoisted(() => ({
  selectProjectMock: vi.fn(),
  inspectProjectMock: vi.fn(),
  startSessionMock: vi.fn(),
  subscribeProgressMock: vi.fn(),
  setBudgetMock: vi.fn(),
  listRecentsMock: vi.fn(),
  touchRecentMock: vi.fn(),
  removeRecentMock: vi.fn(),
}));

vi.mock("../rpc/projectClient", () => ({
  selectProject: selectProjectMock,
  inspectProject: inspectProjectMock,
  setBudget: setBudgetMock,
}));

vi.mock("../services/sessionClient", () => ({
  startSession: startSessionMock,
}));

vi.mock("../services/sidecarEvents", () => ({
  subscribeProgress: subscribeProgressMock,
}));

vi.mock("../rpc/recentsClient", () => ({
  listRecents: listRecentsMock,
  touchRecent: touchRecentMock,
  removeRecent: removeRecentMock,
}));

import { EndSessionReportScreen } from "../screens/EndSessionReportScreen";
import { ChooseProjectScreen } from "../screens/ChooseProjectScreen";
import { StartingProgressRoute } from "../StartingProgressRoute";

const ESR_PAYLOAD: EsrPayload = {
  session_id: "session-1",
  exit_reason: "human_stop",
  exit_code: 0,
  archive_path: "/tmp/proj/.agentshore/archives/session-1",
  report_path: "/tmp/proj/.agentshore/reports/report.html",
  log_path: "/tmp/proj/.agentshore/logs/agentshore-session-1.log",
  esr_summary: {
    overview: {
      session_id: "session-1",
      duration_seconds: 0,
      total_plays: 0,
      successful_plays: 0,
      failed_plays: 0,
      total_cost: 0,
      final_alignment: null,
      started_at: "2026-05-16T00:00:00Z",
      ended_at: "2026-05-16T00:00:00Z",
    },
    repo_url: null,
    play_stats: [],
    closed_issues: [],
    control_rejections: [],
  },
};

const RECENT_ENTRY: RecentEntry = {
  path: "/tmp/proj",
  label: "proj",
  last_started: "2026-05-15T00:00:00Z",
  last_exit_reason: null,
  has_valid_config: true,
};

function makeInspectResult() {
  return {
    path: "/tmp/proj",
    repo_identity: { is_git: true },
    branch: null,
    detected_tools: [],
    agentshore_yaml: { path: "/agentshore.yaml", raw: "budget:\n  enabled: true\n  total: 25\n" },
    beads_status: { initialised: true },
    prerequisites: { git: true, bd: true, gh: true },
  };
}

function installInMemoryLocalStorage(): void {
  const store = new Map<string, string>();
  Object.defineProperty(globalThis, "localStorage", {
    value: {
      get length() {
        return store.size;
      },
      clear: () => store.clear(),
      getItem: (key: string) => store.get(key) ?? null,
      key: (idx: number) => Array.from(store.keys())[idx] ?? null,
      removeItem: (key: string) => {
        store.delete(key);
      },
      setItem: (key: string, value: string) => {
        store.set(key, String(value));
      },
    } as Storage,
    configurable: true,
    writable: true,
  });
}

function renderFromEsr() {
  const sessionContextValue = {
    dashboardUrl: null,
    esr: ESR_PAYLOAD,
    lastProjectPath: "/tmp/proj",
    sessionStarting: false,
    setDashboardUrl: () => undefined,
    setEsr: () => undefined,
    setLastProjectPath: () => undefined,
    setSessionStarting: () => undefined,
  };
  return render(
    <SessionContext.Provider value={sessionContextValue}>
      <MemoryRouter initialEntries={["/session/esr"]}>
        <Routes>
          <Route path="/session/esr" element={<EndSessionReportScreen />} />
          <Route path="/starting" element={<StartingProgressRoute />} />
          <Route
            path="/session/dashboard"
            element={<div data-testid="session-dashboard">dashboard</div>}
          />
        </Routes>
      </MemoryRouter>
    </SessionContext.Provider>,
  );
}

function renderFromChooser() {
  const adapter: ChooseProjectAdapter = {
    list: listRecentsMock as ChooseProjectAdapter["list"],
    touch: touchRecentMock as ChooseProjectAdapter["touch"],
    remove: removeRecentMock as ChooseProjectAdapter["remove"],
    select: selectProjectMock as ChooseProjectAdapter["select"],
    openDirectory: vi.fn().mockResolvedValue(null),
    // Don't override quickStart — we want the REAL helper to run so we
    // exercise the actual select → inspect → start → navigate pipeline.
  };
  const sessionContextValue = {
    dashboardUrl: null,
    esr: null,
    lastProjectPath: null,
    sessionStarting: false,
    setDashboardUrl: () => undefined,
    setEsr: () => undefined,
    setLastProjectPath: () => undefined,
    setSessionStarting: () => undefined,
  };
  return render(
    <SessionContext.Provider value={sessionContextValue}>
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route path="/" element={<ChooseProjectScreen adapter={adapter} />} />
          <Route path="/starting" element={<StartingProgressRoute />} />
          <Route
            path="/session/dashboard"
            element={<div data-testid="session-dashboard">dashboard</div>}
          />
        </Routes>
      </MemoryRouter>
    </SessionContext.Provider>,
  );
}

describe("issue #582: session.start fires exactly once through Repeat / Quick Start", () => {
  beforeEach(() => {
    installInMemoryLocalStorage();
    selectProjectMock.mockResolvedValue({ path: "/tmp/proj" });
    inspectProjectMock.mockResolvedValue(makeInspectResult());
    setBudgetMock.mockResolvedValue({
      budget: { enabled: true, total: 25, warning_threshold: 0.2 },
      yaml_path: "/agentshore.yaml",
    });
    startSessionMock.mockResolvedValue({
      session_id: "sid-repeat",
      ipc_endpoint: { kind: "tcp", host: "127.0.0.1", port: 9999 },
    });
    // subscribeProgress is awaited by /starting before firing session.start.
    // The mock captures the handler and emits the final progress event after
    // a microtask so the route's gate effect transitions to dashboard.
    subscribeProgressMock.mockImplementation(
      async (handler: ProgressHandler) => {
        queueMicrotask(() => {
          handler({ step: "first_snapshot", status: "ok", error: null });
        });
        return () => undefined;
      },
    );
    listRecentsMock.mockResolvedValue([RECENT_ENTRY]);
    touchRecentMock.mockResolvedValue(undefined);
    removeRecentMock.mockResolvedValue(undefined);
  });

  afterEach(() => {
    selectProjectMock.mockReset();
    inspectProjectMock.mockReset();
    startSessionMock.mockReset();
    subscribeProgressMock.mockReset();
    setBudgetMock.mockReset();
    listRecentsMock.mockReset();
    touchRecentMock.mockReset();
    removeRecentMock.mockReset();
    installInMemoryLocalStorage();
    cleanup();
  });

  it("Repeat path: EndSessionReportScreen → /starting dispatches session.start exactly ONCE", async () => {
    renderFromEsr();
    const user = userEvent.setup();

    await user.click(
      screen.getByRole("button", { name: /repeat with same settings/i }),
    );

    // The handoff path latches first_snapshot internally, so /starting
    // navigates straight to the dashboard sentinel.
    expect(await screen.findByTestId("session-dashboard")).toBeInTheDocument();
    expect(startSessionMock).toHaveBeenCalledTimes(1);
  });

  it("Quick Start path: ChooseProjectScreen → /starting dispatches session.start exactly ONCE", async () => {
    renderFromChooser();
    const user = userEvent.setup();

    // The Quick Start chip only renders for rows the helper marks
    // eligible; force-mark by seeding localStorage with a persisted
    // setup snapshot before the row mounts. We installed an empty
    // shim in beforeEach, so write the canonical key now.
    localStorage.setItem(
      "agentshore.desktop.setup.v1",
      JSON.stringify({
        startSelection: { seedInputPath: null },
        budget: { mode: "capped", total: 25 },
      }),
    );

    // Wait for recents to render, then click the row's Quick Start
    // chip. The row test-id pattern is ``quick-start-<path>``.
    const quickStart = await screen.findByTestId("quick-start-/tmp/proj");
    await user.click(quickStart);

    expect(await screen.findByTestId("session-dashboard")).toBeInTheDocument();
    expect(startSessionMock).toHaveBeenCalledTimes(1);
  });
});
