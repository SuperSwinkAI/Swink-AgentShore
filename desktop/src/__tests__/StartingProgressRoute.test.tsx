import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import type { ProgressNotificationParams } from "../services/sidecarEvents";

type ProgressHandler = (params: ProgressNotificationParams) => void;

const { subscribeProgressMock, startSessionMock, setTrustedIssueEnforcementMock } =
  vi.hoisted(() => ({
    subscribeProgressMock: vi.fn(),
    startSessionMock: vi.fn(),
    setTrustedIssueEnforcementMock: vi.fn(),
  }));

vi.mock("../services/sidecarEvents", () => ({
  subscribeProgress: subscribeProgressMock,
}));

vi.mock("../services/sessionClient", () => ({
  startSession: startSessionMock,
}));

vi.mock("../rpc/projectClient", () => ({
  setTrustedIssueEnforcement: setTrustedIssueEnforcementMock,
}));

import { StartingProgressRoute } from "../StartingProgressRoute";

afterEach(() => {
  subscribeProgressMock.mockReset();
  startSessionMock.mockReset();
  setTrustedIssueEnforcementMock.mockReset();
  cleanup();
});

interface RenderRouteOpts {
  state?: unknown;
}

function renderRoute(opts: RenderRouteOpts = {}) {
  const entry =
    opts.state === undefined
      ? "/starting"
      : { pathname: "/starting", state: opts.state };
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/starting" element={<StartingProgressRoute />} />
        <Route
          path="/session/dashboard"
          element={<div data-testid="session-dashboard">Session dashboard</div>}
        />
        <Route
          path="/setup/start"
          element={<div data-testid="setup-start">Setup start</div>}
        />
      </Routes>
    </MemoryRouter>,
  );
}

async function currentProgressToken(): Promise<string | number> {
  await waitFor(() => expect(startSessionMock).toHaveBeenCalledTimes(1));
  const params = startSessionMock.mock.calls[0]?.[0] as
    | { progressToken?: string | number }
    | undefined;
  expect(params?.progressToken).toBeDefined();
  return params!.progressToken!;
}

describe("StartingProgressRoute", () => {
  it("auto-advances to /session/dashboard once first_snapshot=ok AND session.start resolved", async () => {
    let handler: ProgressHandler | null = null;
    subscribeProgressMock.mockImplementation(async (h: ProgressHandler) => {
      handler = h;
      return () => undefined;
    });
    startSessionMock.mockResolvedValue({
      session_id: "test-session",
      ipc_endpoint: { kind: "tcp", host: "127.0.0.1", port: 9999 },
    });

    renderRoute();

    await waitFor(() => expect(handler).not.toBeNull());
    const token = await currentProgressToken();

    handler!({
      token,
      step: "first_snapshot",
      status: "ok",
      percent: 100,
      message: "Dashboard ready",
      error: null,
    });

    expect(await screen.findByTestId("session-dashboard")).toBeInTheDocument();
  });

  it("does not advance until session.start's RPC has resolved, even when first_snapshot=ok", async () => {
    let handler: ProgressHandler | null = null;
    subscribeProgressMock.mockImplementation(async (h: ProgressHandler) => {
      handler = h;
      return () => undefined;
    });
    // Promise that never resolves — session.start is still in flight.
    startSessionMock.mockReturnValue(new Promise(() => undefined));

    renderRoute();

    await waitFor(() => expect(handler).not.toBeNull());
    const token = await currentProgressToken();

    handler!({
      token,
      step: "first_snapshot",
      status: "ok",
      percent: 100,
      message: undefined,
      error: null,
    });

    // first_snapshot=ok alone shouldn't navigate; the RPC hasn't returned.
    expect(screen.queryByTestId("session-dashboard")).not.toBeInTheDocument();
  });

  it("does not advance while intermediate steps complete", async () => {
    let handler: ProgressHandler | null = null;
    subscribeProgressMock.mockImplementation(async (h: ProgressHandler) => {
      handler = h;
      return () => undefined;
    });
    startSessionMock.mockResolvedValue({
      session_id: "test-session",
      ipc_endpoint: { kind: "tcp", host: "127.0.0.1", port: 9999 },
    });

    renderRoute();

    await waitFor(() => expect(handler).not.toBeNull());
    const token = await currentProgressToken();

    handler!({
      token,
      step: "config_merge",
      status: "ok",
      percent: 20,
      message: undefined,
      error: null,
    });
    handler!({
      token,
      step: "install_skills",
      status: "ok",
      percent: 40,
      message: undefined,
      error: null,
    });

    expect(screen.queryByTestId("session-dashboard")).not.toBeInTheDocument();
  });

  it("ignores progress notifications for another start token", async () => {
    let handler: ProgressHandler | null = null;
    subscribeProgressMock.mockImplementation(async (h: ProgressHandler) => {
      handler = h;
      return () => undefined;
    });
    startSessionMock.mockResolvedValue({
      session_id: "test-session",
      ipc_endpoint: { kind: "tcp", host: "127.0.0.1", port: 9999 },
    });

    renderRoute();

    await waitFor(() => expect(handler).not.toBeNull());
    await currentProgressToken();

    handler!({
      token: "stale-start-token",
      step: "first_snapshot",
      status: "ok",
      percent: 100,
      message: "stale completion",
      error: null,
    });

    expect(screen.queryByTestId("session-dashboard")).not.toBeInTheDocument();
    expect(screen.getByTestId("step-first_snapshot")).toHaveAttribute(
      "aria-label",
      "First state snapshot: pending",
    );
  });

  it("skips session.start when navigate state hands off { sessionStarted, startResult } (issue #582)", async () => {
    let handler: ProgressHandler | null = null;
    subscribeProgressMock.mockImplementation(async (h: ProgressHandler) => {
      handler = h;
      return () => undefined;
    });

    renderRoute({
      state: {
        sessionStarted: true,
        startResult: {
          session_id: "preflight-session",
          ipc_endpoint: { kind: "tcp", host: "127.0.0.1", port: 9999 },
        },
      },
    });

    // Wait until the route registers its progress subscriber — that's
    // the synchronization point past which a buggy implementation would
    // have already fired session.start.
    await waitFor(() => expect(handler).not.toBeNull());

    expect(startSessionMock).not.toHaveBeenCalled();
    // The handoff also latches first_snapshot=ok internally, so the
    // dashboard route is reached without any $/progress events.
    expect(await screen.findByTestId("session-dashboard")).toBeInTheDocument();
    expect(startSessionMock).not.toHaveBeenCalled();
  });

  it("ignores a bare sessionStarted flag with no startResult (defensive guard)", async () => {
    let handler: ProgressHandler | null = null;
    subscribeProgressMock.mockImplementation(async (h: ProgressHandler) => {
      handler = h;
      return () => undefined;
    });
    startSessionMock.mockResolvedValue({
      session_id: "fallback",
      ipc_endpoint: { kind: "tcp", host: "127.0.0.1", port: 9999 },
    });

    renderRoute({ state: { sessionStarted: true } });

    await waitFor(() => expect(handler).not.toBeNull());

    // Without a startResult the handoff is incomplete and the route
    // falls back to firing session.start itself.
    await waitFor(() => expect(startSessionMock).toHaveBeenCalledTimes(1));
  });

  it("writes the trusted-issue gate to agentshore.yaml before firing session.start", async () => {
    let handler: ProgressHandler | null = null;
    subscribeProgressMock.mockImplementation(async (h: ProgressHandler) => {
      handler = h;
      return () => undefined;
    });
    setTrustedIssueEnforcementMock.mockResolvedValue({
      enabled: true,
      yaml_path: "/p/agentshore.yaml",
    });
    startSessionMock.mockResolvedValue({
      session_id: "test-session",
      ipc_endpoint: { kind: "tcp", host: "127.0.0.1", port: 9999 },
    });

    renderRoute({ state: { trustedIssueEnforcement: true } });

    await waitFor(() => expect(startSessionMock).toHaveBeenCalledTimes(1));
    // The reconcile RPC ran, with the user's choice, before session.start.
    expect(setTrustedIssueEnforcementMock).toHaveBeenCalledWith(true);
    expect(setTrustedIssueEnforcementMock.mock.invocationCallOrder[0]).toBeLessThan(
      startSessionMock.mock.invocationCallOrder[0],
    );
  });

  it("refuses to start when ENABLING the gate fails (no silent ungated launch)", async () => {
    let handler: ProgressHandler | null = null;
    subscribeProgressMock.mockImplementation(async (h: ProgressHandler) => {
      handler = h;
      return () => undefined;
    });
    setTrustedIssueEnforcementMock.mockRejectedValue(new Error("no active project"));

    renderRoute({ state: { trustedIssueEnforcement: true } });

    await waitFor(() => expect(handler).not.toBeNull());
    // The enable write failed, so the session must NOT start ungated and
    // the user sees the error.
    expect(startSessionMock).not.toHaveBeenCalled();
    expect(
      await screen.findByText(/only work issues from trusted identities/i),
    ).toBeInTheDocument();
  });

  it("still starts when DISABLING the gate fails (absent key already means off)", async () => {
    let handler: ProgressHandler | null = null;
    subscribeProgressMock.mockImplementation(async (h: ProgressHandler) => {
      handler = h;
      return () => undefined;
    });
    setTrustedIssueEnforcementMock.mockRejectedValue(new Error("no active project"));
    startSessionMock.mockResolvedValue({
      session_id: "test-session",
      ipc_endpoint: { kind: "tcp", host: "127.0.0.1", port: 9999 },
    });

    renderRoute({ state: { trustedIssueEnforcement: false } });

    // wantGate === false: a failed write is a no-op, so start proceeds.
    await waitFor(() => expect(startSessionMock).toHaveBeenCalledTimes(1));
  });

  it("skips the reconcile entirely when no gate choice is in the route state", async () => {
    let handler: ProgressHandler | null = null;
    subscribeProgressMock.mockImplementation(async (h: ProgressHandler) => {
      handler = h;
      return () => undefined;
    });
    startSessionMock.mockResolvedValue({
      session_id: "test-session",
      ipc_endpoint: { kind: "tcp", host: "127.0.0.1", port: 9999 },
    });

    renderRoute();

    await waitFor(() => expect(startSessionMock).toHaveBeenCalledTimes(1));
    expect(setTrustedIssueEnforcementMock).not.toHaveBeenCalled();
  });

  it("does not advance when first_snapshot fails", async () => {
    let handler: ProgressHandler | null = null;
    subscribeProgressMock.mockImplementation(async (h: ProgressHandler) => {
      handler = h;
      return () => undefined;
    });
    startSessionMock.mockResolvedValue({
      session_id: "test-session",
      ipc_endpoint: { kind: "tcp", host: "127.0.0.1", port: 9999 },
    });

    renderRoute();

    await waitFor(() => expect(handler).not.toBeNull());
    const token = await currentProgressToken();

    handler!({
      token,
      step: "first_snapshot",
      status: "failed",
      percent: 100,
      message: undefined,
      error: "no snapshot delivered",
    });

    expect(screen.queryByTestId("session-dashboard")).not.toBeInTheDocument();
  });
});
