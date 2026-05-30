import { describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { SessionContext, type EsrPayload } from "../../services/sessionContext";

const { dashboardMock, listenMock, stopSessionMock } = vi.hoisted(() => ({
  dashboardMock: vi.fn(),
  listenMock: vi.fn(),
  stopSessionMock: vi.fn(),
}));

vi.mock("@tauri-apps/api/event", () => ({
  listen: listenMock,
}));

vi.mock("../../services/sessionClient", () => ({
  stopSession: stopSessionMock,
}));

vi.mock("agentshore-dashboard", () => ({
  Dashboard: (props: unknown) => {
    dashboardMock(props);
    return <div data-testid="dashboard-sentinel" />;
  },
}));

import { SessionDashboardScreen } from "../SessionDashboardScreen";

const ESR_PAYLOAD = {
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
} satisfies EsrPayload;

describe("SessionDashboardScreen", () => {
  it("stores the ESR payload and routes to the report when File > Stop Session fires", async () => {
    let menuStopHandler: (() => void | Promise<void>) | null = null;
    const setEsr = vi.fn();
    const unlisten = vi.fn();
    stopSessionMock.mockResolvedValueOnce(ESR_PAYLOAD);
    listenMock.mockImplementationOnce(async (event, handler) => {
      expect(event).toBe("menu:stop_session");
      menuStopHandler = handler;
      return unlisten;
    });

    render(
      <SessionContext.Provider
        value={{
          dashboardUrl: "http://127.0.0.1:8123/",
          esr: null,
          lastProjectPath: "/tmp/proj",
          sessionStarting: false,
          setDashboardUrl: () => undefined,
          setEsr,
          setLastProjectPath: () => undefined,
          setSessionStarting: () => undefined,
        }}
      >
        <MemoryRouter initialEntries={["/session/dashboard"]}>
          <Routes>
            <Route path="/session/dashboard" element={<SessionDashboardScreen />} />
            <Route path="/session/esr" element={<div data-testid="esr-sentinel" />} />
          </Routes>
        </MemoryRouter>
      </SessionContext.Provider>,
    );

    expect(screen.getByTestId("dashboard-sentinel")).toBeInTheDocument();
    expect(dashboardMock.mock.calls[0]?.[0]).toEqual(
      expect.objectContaining({
        wsUrl: "ws://127.0.0.1:8123/ws",
      }),
    );

    await waitFor(() => expect(menuStopHandler).toBeTruthy());
    await act(async () => {
      await menuStopHandler?.();
    });

    expect(stopSessionMock).toHaveBeenCalledWith({ mode: "drain" });
    expect(setEsr).toHaveBeenCalledWith(ESR_PAYLOAD);
    expect(await screen.findByTestId("esr-sentinel")).toBeInTheDocument();
  });
});
