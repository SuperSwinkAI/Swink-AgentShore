import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { EndSessionReportScreen } from "../EndSessionReportScreen";
import { SessionContext, type EsrPayload } from "../../services/sessionContext";
import type { startSessionFromPersistedSetup } from "../../setup/startFromPersistedSetup";

type RepeatImpl = typeof startSessionFromPersistedSetup;

interface RenderOpts {
  lastProjectPath?: string | null;
  repeatImpl?: RepeatImpl;
  openPathImpl?: (path: string) => Promise<void>;
}

function renderScreen(esr: EsrPayload | null, opts: RenderOpts = {}) {
  const adapter = {
    fetchReportByPath: vi.fn(async () => ({
      html: "<!doctype html><html><body>AgentShore End Session Report</body></html>",
      sections: [],
    })),
    fetchLogsByPath: vi.fn(async () => ({ lines: ["log-1", "log-2"] })),
  };

  const result = render(
    <SessionContext.Provider
      value={{
        dashboardUrl: null,
        esr,
        lastProjectPath: opts.lastProjectPath ?? null,
        sessionStarting: false,
        sessionReattaching: false,
        setDashboardUrl: () => undefined,
        setEsr: () => undefined,
        setLastProjectPath: () => undefined,
        setSessionStarting: () => undefined,
        setSessionReattaching: () => undefined,
      }}
    >
      <MemoryRouter initialEntries={["/session/esr"]}>
        <Routes>
          <Route
            path="/session/esr"
            element={
              <EndSessionReportScreen
                adapter={adapter}
                repeatImpl={opts.repeatImpl}
                openPathImpl={opts.openPathImpl}
              />
            }
          />
          <Route path="/" element={<div data-testid="choose-project-sentinel">cp</div>} />
          <Route
            path="/starting"
            element={<div data-testid="starting-sentinel">starting</div>}
          />
        </Routes>
      </MemoryRouter>
    </SessionContext.Provider>,
  );
  return { ...result, adapter };
}

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

describe("EndSessionReportScreen", () => {
  it("loads the generated report_path as the default ESR surface", async () => {
    const { adapter } = renderScreen(ESR_PAYLOAD);

    const frame = await screen.findByTitle("Full session report");

    expect(adapter.fetchReportByPath).toHaveBeenCalledWith(ESR_PAYLOAD.report_path);
    expect(frame).toHaveAttribute(
      "srcdoc",
      expect.stringContaining("AgentShore End Session Report"),
    );
  });

  it("loads raw logs from the session log_path provided by core", async () => {
    const { adapter } = renderScreen(ESR_PAYLOAD);
    const user = userEvent.setup();

    await user.click(screen.getByRole("tab", { name: /raw logs/i }));

    expect(await screen.findByText(/log-1/)).toBeInTheDocument();
    expect(adapter.fetchLogsByPath).toHaveBeenCalledWith(
      ESR_PAYLOAD.log_path,
      undefined,
    );
  });

  it("does not infer a raw log path when the ESR payload omits one", async () => {
    const { adapter } = renderScreen({ ...ESR_PAYLOAD, log_path: null });
    const user = userEvent.setup();

    await user.click(screen.getByRole("tab", { name: /raw logs/i }));

    expect(screen.getByText(/log path unavailable/i)).toBeInTheDocument();
    expect(adapter.fetchLogsByPath).not.toHaveBeenCalled();
  });

  it("navigates back to Screen 1 when clicking Start a new session", async () => {
    renderScreen(ESR_PAYLOAD);
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /start a new session/i }));

    expect(screen.getByTestId("choose-project-sentinel")).toBeInTheDocument();
  });

  it("renders fallback and routes Return to start to Screen 1 when no ESR exists", async () => {
    renderScreen(null);
    const user = userEvent.setup();

    expect(
      screen.getByRole("heading", { name: /no end-of-session report available/i }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: /return to start/i }));

    expect(screen.getByTestId("choose-project-sentinel")).toBeInTheDocument();
  });

  it("renders the issue #561 chrome bar with Back-to-Home and Repeat buttons", () => {
    renderScreen(ESR_PAYLOAD);

    expect(screen.getByTestId("esr-chrome-bar")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /back to home/i })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /repeat with same settings/i }),
    ).toBeInTheDocument();
  });

  it("Back-to-Home navigates the router to the chooser", async () => {
    renderScreen(ESR_PAYLOAD);
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /back to home/i }));

    expect(screen.getByTestId("choose-project-sentinel")).toBeInTheDocument();
  });

  it("Repeat invokes startSessionFromPersistedSetup with the SessionContext project path", async () => {
    const repeatImpl: RepeatImpl = vi.fn(async () => undefined);
    renderScreen(ESR_PAYLOAD, {
      lastProjectPath: "/Users/example/projects/foo",
      repeatImpl,
    });
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /repeat with same settings/i }));

    expect(repeatImpl).toHaveBeenCalledTimes(1);
    expect(repeatImpl).toHaveBeenCalledWith(
      "/Users/example/projects/foo",
      expect.objectContaining({
        onError: expect.any(Function),
        navigate: expect.any(Function),
      }),
    );
  });

  it("Repeat falls back to inferring project path from the archive_path", async () => {
    const repeatImpl: RepeatImpl = vi.fn(async () => undefined);
    renderScreen(ESR_PAYLOAD, {
      // lastProjectPath null — must derive from "/tmp/proj/.agentshore/archives/..."
      lastProjectPath: null,
      repeatImpl,
    });
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /repeat with same settings/i }));

    expect(repeatImpl).toHaveBeenCalledWith("/tmp/proj", expect.any(Object));
  });

  it("Repeat surfaces a fallback message when no project path is on record", async () => {
    const repeatImpl: RepeatImpl = vi.fn(async () => undefined);
    // Fallback ESR (no esr) renders the chrome bar too, but archive_path is
    // unavailable so the helper must NOT be invoked.
    renderScreen(null, { lastProjectPath: null, repeatImpl });
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /repeat with same settings/i }));

    expect(repeatImpl).not.toHaveBeenCalled();
    expect(
      screen.getByRole("alert", { name: undefined }),
    ).toHaveTextContent(/no prior project path on record/i);
  });

  it("Repeat surfaces a step-scoped error from the helper", async () => {
    const repeatImpl: RepeatImpl = vi.fn(async (_path, opts) => {
      opts?.onError?.(new Error("rpc down"), "select");
    });
    renderScreen(ESR_PAYLOAD, {
      lastProjectPath: "/Users/example/projects/foo",
      repeatImpl,
    });
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /repeat with same settings/i }));

    expect(repeatImpl).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /couldn't select — try again from home/i,
    );
  });

  it("auto-opens the timelapse MP4 when the payload carries one", async () => {
    const openPathImpl = vi.fn(async () => undefined);
    renderScreen(
      { ...ESR_PAYLOAD, timelapse_output_path: "/tmp/proj/.agentshore/timelapse-runs/x/output.mp4" },
      { openPathImpl },
    );
    await vi.waitFor(() =>
      expect(openPathImpl).toHaveBeenCalledWith(
        "/tmp/proj/.agentshore/timelapse-runs/x/output.mp4",
      ),
    );
  });

  it("does not open anything when no timelapse path is present", async () => {
    const openPathImpl = vi.fn(async () => undefined);
    renderScreen(ESR_PAYLOAD, { openPathImpl });
    // Give effects a tick to run.
    await new Promise((r) => setTimeout(r, 0));
    expect(openPathImpl).not.toHaveBeenCalled();
    expect(screen.queryByTestId("esr-open-timelapse")).not.toBeInTheDocument();
  });

  it("shows an Open timelapse button that re-opens the MP4", async () => {
    const openPathImpl = vi.fn(async () => undefined);
    renderScreen(
      { ...ESR_PAYLOAD, timelapse_output_path: "/tmp/x/output.mp4" },
      { openPathImpl },
    );
    const user = userEvent.setup();
    await user.click(await screen.findByTestId("esr-open-timelapse"));
    // Auto-open (1) + button click (1).
    expect(openPathImpl).toHaveBeenCalledTimes(2);
  });
});
