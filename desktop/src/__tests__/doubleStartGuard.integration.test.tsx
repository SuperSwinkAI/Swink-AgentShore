/**
 * Quick Start navigates to a mocked /starting route inside a MemoryRouter.
 * Assert that session.start is invoked exactly once end-to-end.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import type { ProgressNotificationParams } from "../services/sidecarEvents";
import type { ChooseProjectAdapter } from "../screens/ChooseProjectScreen";
import type { RecentEntry } from "../rpc/recentsClient";
import { SessionContext } from "../services/sessionContext";

type ProgressHandler = (params: ProgressNotificationParams) => void;



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
  budgetSelectionToConfig: (s: { mode: string; total: number; timeMode?: string; timeMinutes?: number }) => ({
    enabled: s.mode === "capped",
    total: s.mode === "capped" ? s.total : 0,
    time_enabled: s.timeMode === "capped",
    time_total_minutes: s.timeMode === "capped" ? (s.timeMinutes ?? 0) : 0,
  }),
  budgetHydrationToSelection: () => null,
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

import { ChooseProjectScreen } from "../screens/ChooseProjectScreen";
import { StartingProgressRoute } from "../StartingProgressRoute";

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
    agentshore_yaml: {
      path: "/agentshore.yaml",
      raw: "budget:\n  enabled: true\n  total: 25\n",
    },
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

function renderFromChooser() {
  const adapter: ChooseProjectAdapter = {
    list: listRecentsMock as ChooseProjectAdapter["list"],
    touch: touchRecentMock as ChooseProjectAdapter["touch"],
    remove: removeRecentMock as ChooseProjectAdapter["remove"],
    select: selectProjectMock as ChooseProjectAdapter["select"],
    openDirectory: vi.fn().mockResolvedValue(null),


  };
  const sessionContextValue = {
    dashboardUrl: null,
    esr: null,
    lastProjectPath: null,
    sessionStarting: false,
    sessionReattaching: false,
    setDashboardUrl: () => undefined,
    setEsr: () => undefined,
    setLastProjectPath: () => undefined,
    setSessionStarting: () => undefined,
    setSessionReattaching: () => undefined,
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

describe("issue #582: session.start fires exactly once through Quick Start", () => {
  beforeEach(() => {
    installInMemoryLocalStorage();
    selectProjectMock.mockResolvedValue({ path: "/tmp/proj" });
    inspectProjectMock.mockResolvedValue(makeInspectResult());
    setBudgetMock.mockResolvedValue({
      budget: { enabled: true, total: 25, warning_threshold: 0.2 },
      yaml_path: "/agentshore.yaml",
    });
    let progressHandler: ProgressHandler | null = null;
    subscribeProgressMock.mockImplementation(
      async (handler: ProgressHandler) => {
        progressHandler = handler;
        return () => undefined;
      },
    );
    startSessionMock.mockImplementation(
      async (params?: { progressToken?: string | number }) => {
        progressHandler?.({
          token: params?.progressToken,
          step: "first_snapshot",
          status: "ok",
          error: null,
        });
        return {
          session_id: "sid-repeat",
          ipc_endpoint: { kind: "tcp", host: "127.0.0.1", port: 9999 },
        };
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

  it("Quick Start path: ChooseProjectScreen → /starting dispatches session.start exactly ONCE", async () => {
    renderFromChooser();
    const user = userEvent.setup();





    localStorage.setItem(
      "agentshore.desktop.setup.v1",
      JSON.stringify({
        startSelection: { seedInputPath: null },
        budget: { mode: "capped", total: 25 },
      }),
    );



    const quickStart = await screen.findByTestId("quick-start-/tmp/proj");
    await user.click(quickStart);

    expect(await screen.findByTestId("session-dashboard")).toBeInTheDocument();
    expect(startSessionMock).toHaveBeenCalledTimes(1);
  });
});
