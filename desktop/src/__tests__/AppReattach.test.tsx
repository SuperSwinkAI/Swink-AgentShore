/**
 * Tests for the #274 WebView reattach flow wired into App.tsx.
 *
 * Covers:
 *  - reattach-on-mount: active session → /session/dashboard, inactive → /
 *  - no-flash: pending current_session promise → ChooseProjectScreen hidden
 *  - reload-doesn't-stop: reattach never calls session.stop
 *  - currentSession() RPC wrapper unit test (in rpc/sessionClient)
 *  - heartbeat Phase 2: invoke("ui_heartbeat") fires on mount + repeats
 */
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { SessionProvider } from "../services/sessionContext";

// ---------------------------------------------------------------------------
// Hoisted mocks
// ---------------------------------------------------------------------------
const { invokeMock, listenMock } = vi.hoisted(() => ({
  invokeMock: vi.fn(),
  listenMock: vi.fn(),
}));

vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));
vi.mock("@tauri-apps/api/event", () => ({ listen: listenMock }));

// Dashboard package — used at module-level (AGENT_TYPE_SET) so must be mocked.
vi.mock("@agentshore/dashboard", () => ({
  AGENT_REGISTRY: {},
  AGENT_TYPES: [],
  DashboardCanvas: () => null,
  IdentitiesScreen: () => null,
  TrustedSourcesScreen: () => null,
}));

// Do NOT mock ../rpc/sessionClient — let the real currentSession() and
// stopSession() implementations run so they call the mocked invoke. This
// lets the wrapper unit test verify the real impl and lets the
// reload-doesn't-stop test check invokeMock for absence of session.stop.

vi.mock("../services/sessionClient", () => ({
  subscribeCompleted: vi.fn(() => () => undefined),
}));

vi.mock("../services/sidecarEvents", () => ({
  subscribeSidecarCrashed: vi.fn(async () => () => undefined),
  subscribeSidecarNotification: vi.fn(async () => () => undefined),
  subscribeBeadsSchemaDrift: vi.fn(async () => () => undefined),
}));

vi.mock("../rpc/identitiesClient", () => ({
  addIdentity: vi.fn(),
  addTrustedSource: vi.fn(),
  checkIdentityAccess: vi.fn(),
  checkKeychainToken: vi.fn(),
  listIdentities: vi.fn(),
  listTrustedSources: vi.fn(),
  removeIdentity: vi.fn(),
  removeTrustedSource: vi.fn(),
  updateIdentity: vi.fn(),
}));

vi.mock("../rpc/agentsClient", () => ({
  configureAgent: vi.fn(),
  getAgentsCatalog: vi.fn(),
  listAgents: vi.fn(),
}));

vi.mock("../rpc/projectClient", () => ({
  budgetHydrationToSelection: vi.fn(() => null),
  budgetSelectionToConfig: vi.fn(() => ({})),
  inspectProject: vi.fn(),
  setBudget: vi.fn(),
  setSeedPaths: vi.fn(),
  setTrustedIssueEnforcement: vi.fn(),
}));

// Screen stubs — just need to be renderable and distinguishable.
vi.mock("../screens/ChooseProjectScreen", () => ({
  ChooseProjectScreen: () => <div data-testid="choose-project-sentinel" />,
}));
vi.mock("../screens/SessionDashboardScreen", () => ({
  SessionDashboardScreen: () => <div data-testid="session-dashboard-sentinel" />,
}));
vi.mock("../screens/EndSessionReportScreen", () => ({
  EndSessionReportScreen: () => null,
}));
vi.mock("../screens/AgentsScreen", () => ({ AgentsScreen: () => null }));
vi.mock("../screens/BudgetScreen", () => ({ BudgetScreen: () => null }));
vi.mock("../screens/FatalErrorScreen", () => ({ FatalErrorScreen: () => null }));
vi.mock("../screens/ReadinessScreen", () => ({ ReadinessScreen: () => null }));
vi.mock("../screens/RecoveryScreen", () => ({ RecoveryScreen: () => null }));
vi.mock("../screens/StartScreen", () => ({ StartScreen: () => null }));
vi.mock("../screens/TargetBranchScreen", () => ({ TargetBranchScreen: () => null }));
vi.mock("../screens/DemoDashboardScreen", () => ({ DemoDashboardScreen: () => null }));
vi.mock("../StartingProgressRoute", () => ({ StartingProgressRoute: () => null }));
vi.mock("../SessionStartingOverlay", () => ({ SessionStartingOverlay: () => null }));
vi.mock("../components/AppMenu", () => ({ AppMenu: () => null }));
vi.mock("../components/WelcomeCarousel", () => ({ WelcomeCarousel: () => null }));

// ---------------------------------------------------------------------------
// Import App after all mocks are in place.
// ---------------------------------------------------------------------------
import { App } from "../App";

// ---------------------------------------------------------------------------
// jsdom shims required by App.tsx
// ---------------------------------------------------------------------------

// App's theme effect calls window.matchMedia — jsdom doesn't implement it.
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

// ---------------------------------------------------------------------------
// Default invoke responses (can be overridden per-test).
// ---------------------------------------------------------------------------
const DEFAULT_UI_STATE = {
  theme: "system",
  lastSelectedTab: "",
  window: null,
  onboardingCompleted: true,
};

function setupDefaultInvoke(
  currentSessionOverride?: () => Promise<unknown>,
) {
  const unlisten = vi.fn();
  listenMock.mockImplementation(async () => unlisten);

  invokeMock.mockImplementation(async (cmd: string) => {
    if (cmd === "load_ui_state") return DEFAULT_UI_STATE;
    if (cmd === "get_fatal_shell_state") return null;
    if (cmd === "current_session") {
      return currentSessionOverride
        ? currentSessionOverride()
        : Promise.resolve({ active: false, dashboardUrl: null, sessionId: null });
    }
    if (cmd === "ui_heartbeat") return undefined;
    return undefined;
  });
}

function renderApp() {
  // Match main.tsx: BrowserRouter → SessionProvider → App.
  // App renders its own <Routes> internally — no outer Routes wrapper.
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <SessionProvider>
        <App />
      </SessionProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  setupDefaultInvoke();
});

afterEach(() => {
  vi.clearAllMocks();
  cleanup();
});

// ---------------------------------------------------------------------------
// currentSession() wrapper unit test
// ---------------------------------------------------------------------------
describe("currentSession() RPC wrapper", () => {
  it("calls invoke('current_session') and returns the response shape", async () => {
    const { currentSession } = await import("../rpc/sessionClient");
    invokeMock.mockResolvedValueOnce({
      active: true,
      dashboardUrl: "http://127.0.0.1:9000/",
      sessionId: "sid-abc",
    });

    const result = await currentSession();

    expect(invokeMock).toHaveBeenCalledWith("current_session");
    expect(result).toEqual({
      active: true,
      dashboardUrl: "http://127.0.0.1:9000/",
      sessionId: "sid-abc",
    });
  });

  it("returns active: false shape when no session is running", async () => {
    const { currentSession } = await import("../rpc/sessionClient");
    invokeMock.mockResolvedValueOnce({
      active: false,
      dashboardUrl: null,
      sessionId: null,
    });

    const result = await currentSession();

    expect(result.active).toBe(false);
    expect(result.dashboardUrl).toBeNull();
    expect(result.sessionId).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// reattach-on-mount
// ---------------------------------------------------------------------------
describe("reattach-on-mount", () => {
  it("navigates to /session/dashboard when current_session returns active", async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "load_ui_state") return DEFAULT_UI_STATE;
      if (cmd === "get_fatal_shell_state") return null;
      if (cmd === "current_session")
        return { active: true, dashboardUrl: "http://127.0.0.1:8123/", sessionId: "sid-1" };
      if (cmd === "ui_heartbeat") return undefined;
      return undefined;
    });

    renderApp();

    await waitFor(() =>
      expect(screen.getByTestId("session-dashboard-sentinel")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("choose-project-sentinel")).not.toBeInTheDocument();
  });

  it("stays on / and shows ChooseProjectScreen when session is inactive", async () => {
    // default invoke returns active: false

    renderApp();

    await waitFor(() =>
      expect(screen.getByTestId("choose-project-sentinel")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("session-dashboard-sentinel")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// no-flash
// ---------------------------------------------------------------------------
describe("no-flash", () => {
  it("does not render ChooseProjectScreen while current_session is pending", async () => {
    let resolveProbe!: (v: unknown) => void;
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "load_ui_state") return DEFAULT_UI_STATE;
      if (cmd === "get_fatal_shell_state") return null;
      if (cmd === "current_session")
        return new Promise((res) => {
          resolveProbe = res;
        });
      if (cmd === "ui_heartbeat") return undefined;
      return undefined;
    });

    renderApp();

    // While the probe is unresolved, picker must not appear.
    expect(screen.queryByTestId("choose-project-sentinel")).not.toBeInTheDocument();

    // Resolve with an inactive session and let the picker render.
    await act(async () => {
      resolveProbe({ active: false, dashboardUrl: null, sessionId: null });
    });

    await waitFor(() =>
      expect(screen.getByTestId("choose-project-sentinel")).toBeInTheDocument(),
    );
  });

  it("renders /session/dashboard (not picker) when probe resolves active", async () => {
    let resolveProbe!: (v: unknown) => void;
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "load_ui_state") return DEFAULT_UI_STATE;
      if (cmd === "get_fatal_shell_state") return null;
      if (cmd === "current_session")
        return new Promise((res) => {
          resolveProbe = res;
        });
      if (cmd === "ui_heartbeat") return undefined;
      return undefined;
    });

    renderApp();

    expect(screen.queryByTestId("choose-project-sentinel")).not.toBeInTheDocument();

    await act(async () => {
      resolveProbe({ active: true, dashboardUrl: "http://127.0.0.1:8123/", sessionId: "sid-2" });
    });

    await waitFor(() =>
      expect(screen.getByTestId("session-dashboard-sentinel")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("choose-project-sentinel")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// reload-doesn't-stop
// ---------------------------------------------------------------------------
describe("reload-doesn't-stop", () => {
  it("never issues jsonrpc_call session.stop across a reattach", async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "load_ui_state") return DEFAULT_UI_STATE;
      if (cmd === "get_fatal_shell_state") return null;
      if (cmd === "current_session")
        return { active: true, dashboardUrl: "http://127.0.0.1:8123/", sessionId: "sid-3" };
      if (cmd === "ui_heartbeat") return undefined;
      return undefined;
    });

    renderApp();

    await waitFor(() =>
      expect(screen.getByTestId("session-dashboard-sentinel")).toBeInTheDocument(),
    );

    // stopSession calls invoke("jsonrpc_call", { method: "session.stop", ... }).
    // During a reattach, no stop RPC must be issued.
    const stopCalls = invokeMock.mock.calls.filter((args: unknown[]) => {
      const [cmd, params] = args;
      return (
        cmd === "jsonrpc_call" &&
        typeof params === "object" &&
        params !== null &&
        (params as Record<string, unknown>).method === "session.stop"
      );
    });
    expect(stopCalls).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Phase 2 — heartbeat
// ---------------------------------------------------------------------------
describe("heartbeat (Phase 2)", () => {
  it("invokes ui_heartbeat on mount and again after ~2s via rAF-gated loop", async () => {
    // Use fake timers only for this test to control the rAF/setTimeout cycle.
    // jsdom stubs requestAnimationFrame via setTimeout(cb, 0) so fake timers
    // intercept both.
    vi.useFakeTimers();
    setupDefaultInvoke();

    renderApp();

    // Advance enough to fire the initial rAF (jsdom rAF ≈ setTimeout(cb,0))
    // and process any synchronous microtasks.
    await act(async () => {
      vi.advanceTimersByTime(50);
    });

    const callsAfterFirst = invokeMock.mock.calls.filter(
      (args: unknown[]) => args[0] === "ui_heartbeat",
    );
    expect(callsAfterFirst.length).toBeGreaterThanOrEqual(1);

    // Advance by 2050ms to trigger the 2s repeat setTimeout then another rAF.
    await act(async () => {
      vi.advanceTimersByTime(2050);
    });

    const callsAfterSecond = invokeMock.mock.calls.filter(
      (args: unknown[]) => args[0] === "ui_heartbeat",
    );
    expect(callsAfterSecond.length).toBeGreaterThanOrEqual(2);

    // Unmount first so the heartbeat loop's cleanup runs (sets mounted=false,
    // cancels pending timers). Then restore real timers. Order matters:
    // cleanup() triggers the useEffect teardown; vi.useRealTimers() after
    // ensures no leaked fake-timer state into the next test.
    cleanup();
    vi.useRealTimers();
  });
});
