import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { RecoveryScreen, type RecoveryAdapter, type TrackedAgent } from "../RecoveryScreen";
import type { SidecarCrashedPayload } from "../../services/sidecarEvents";

function makeAdapter(agents: TrackedAgent[] = []): RecoveryAdapter & {
  openLog: ReturnType<typeof vi.fn>;
  restartSidecar: ReturnType<typeof vi.fn>;
  quitApp: ReturnType<typeof vi.fn>;
  trackedAgents: ReturnType<typeof vi.fn>;
  killAllAgents: ReturnType<typeof vi.fn>;
} {
  return {
    openLog: vi.fn().mockResolvedValue(undefined),
    restartSidecar: vi.fn().mockResolvedValue(undefined),
    quitApp: vi.fn().mockResolvedValue(undefined),
    trackedAgents: vi.fn().mockResolvedValue(agents),
    killAllAgents: vi.fn().mockResolvedValue(agents),
  };
}

const PAYLOAD: SidecarCrashedPayload = {
  exit_code: 137,
  last_stderr_lines: ["fatal: out of memory", "killed by OOM"],
  log_file_path: "/Users/example/.agentshore/sidecar.log",
};

describe("RecoveryScreen", () => {
  it("renders exit code, log path, and stderr lines from payload", () => {
    const adapter = makeAdapter();
    render(<RecoveryScreen payload={PAYLOAD} adapter={adapter} />);

    expect(screen.getByTestId("exit-code").textContent).toContain("137");
    expect(screen.getByTestId("log-path").textContent).toContain(
      "/Users/example/.agentshore/sidecar.log",
    );
    const stderr = screen.getByTestId("stderr-pane");
    expect(stderr.textContent).toContain("fatal: out of memory");
    expect(stderr.textContent).toContain("killed by OOM");
  });

  it("renders an empty-state message when no stderr is captured", () => {
    const adapter = makeAdapter();
    render(
      <RecoveryScreen
        payload={{ exit_code: null, last_stderr_lines: [], log_file_path: null }}
        adapter={adapter}
      />,
    );
    expect(screen.queryByTestId("stderr-pane")).not.toBeInTheDocument();
    expect(screen.getByTestId("stderr-empty")).toBeInTheDocument();
  });

  it("clicking Open log file calls adapter.openLog with the payload path", async () => {
    const adapter = makeAdapter();
    render(<RecoveryScreen payload={PAYLOAD} adapter={adapter} />);
    await userEvent.click(screen.getByTestId("open-log"));
    expect(adapter.openLog).toHaveBeenCalledWith("/Users/example/.agentshore/sidecar.log");
  });

  it("disables Open log file when log_file_path is null", () => {
    const adapter = makeAdapter();
    render(
      <RecoveryScreen
        payload={{ exit_code: 1, last_stderr_lines: [], log_file_path: null }}
        adapter={adapter}
      />,
    );
    expect(screen.getByTestId("open-log")).toBeDisabled();
  });

  it("clicking Restart sidecar calls adapter.restartSidecar", async () => {
    const adapter = makeAdapter();
    render(<RecoveryScreen payload={PAYLOAD} adapter={adapter} />);
    await userEvent.click(screen.getByTestId("restart-sidecar"));
    expect(adapter.restartSidecar).toHaveBeenCalledTimes(1);
  });

  it("clicking Quit app calls adapter.quitApp", async () => {
    const adapter = makeAdapter();
    render(<RecoveryScreen payload={PAYLOAD} adapter={adapter} />);
    await userEvent.click(screen.getByTestId("quit-app"));
    expect(adapter.quitApp).toHaveBeenCalledTimes(1);
  });

  it("renders gracefully when payload is null", () => {
    const adapter = makeAdapter();
    render(<RecoveryScreen payload={null} adapter={adapter} />);
    expect(screen.getByTestId("exit-code").textContent).toContain("unknown");
    expect(screen.getByTestId("log-path").textContent).toContain("no log file recorded");
    expect(screen.getByTestId("stderr-empty")).toBeInTheDocument();
    expect(screen.getByTestId("open-log")).toBeDisabled();
  });

  describe("tracked agent subprocesses", () => {
    const AGENTS: TrackedAgent[] = [
      { agent_id: "agent-claude", agent_type: "claude_code", pid: 42 },
      { agent_id: "agent-codex", agent_type: "codex", pid: 101 },
    ];

    it("shows empty state when no tracked agents are reported", async () => {
      const adapter = makeAdapter([]);
      render(<RecoveryScreen payload={PAYLOAD} adapter={adapter} />);
      await waitFor(() => expect(adapter.trackedAgents).toHaveBeenCalled());
      expect(screen.getByTestId("agents-empty")).toBeInTheDocument();
      expect(screen.queryByTestId("kill-all-agents")).not.toBeInTheDocument();
    });

    it("renders one row per tracked agent with type + pid", async () => {
      const adapter = makeAdapter(AGENTS);
      render(<RecoveryScreen payload={PAYLOAD} adapter={adapter} />);
      const claudeRow = await screen.findByTestId("agent-row-agent-claude");
      expect(claudeRow.textContent).toContain("claude_code");
      expect(claudeRow.textContent).toContain("pid 42");
      expect(screen.getByTestId("agent-row-agent-codex")).toBeInTheDocument();
    });

    it("Kill all button invokes adapter.killAllAgents and clears the list", async () => {
      const adapter = makeAdapter(AGENTS);
      render(<RecoveryScreen payload={PAYLOAD} adapter={adapter} />);
      const killBtn = await screen.findByTestId("kill-all-agents");
      expect(killBtn.textContent).toContain("Kill all (2)");
      await userEvent.click(killBtn);
      await waitFor(() => expect(adapter.killAllAgents).toHaveBeenCalledTimes(1));
      await waitFor(() => expect(screen.queryByTestId("agents-list")).not.toBeInTheDocument());
      expect(screen.getByTestId("agents-empty")).toBeInTheDocument();
    });
  });
});
