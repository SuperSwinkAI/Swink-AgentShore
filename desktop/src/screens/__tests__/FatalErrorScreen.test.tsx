import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  FatalErrorScreen,
  type FatalErrorAdapter,
  type FatalShellInfo,
} from "../FatalErrorScreen";

function makeAdapter(logFilePath: string | null = null): FatalErrorAdapter & {
  openLog: ReturnType<typeof vi.fn>;
  quitApp: ReturnType<typeof vi.fn>;
} {
  return {
    logFilePath,
    openLog: vi.fn().mockResolvedValue(undefined),
    quitApp: vi.fn().mockResolvedValue(undefined),
  };
}

describe("FatalErrorScreen — build_id mismatch", () => {
  const MISMATCH: FatalShellInfo = {
    kind: "build_id_mismatch",
    expected: "shell-abc123",
    received: "sidecar-def456",
  };

  it("renders the mismatch headline + expected/received build_ids", () => {
    render(<FatalErrorScreen info={MISMATCH} adapter={makeAdapter()} />);
    expect(screen.getByText(/AgentShore build mismatch/i)).toBeInTheDocument();
    expect(screen.getByTestId("expected-build-id").textContent).toBe("shell-abc123");
    expect(screen.getByTestId("received-build-id").textContent).toBe("sidecar-def456");
  });

  it("renders only Quit and Open log file buttons (no other navigation)", () => {
    render(<FatalErrorScreen info={MISMATCH} adapter={makeAdapter()} />);
    expect(screen.getByTestId("quit-app")).toBeInTheDocument();
    expect(screen.getByTestId("open-log")).toBeInTheDocument();
    // No "Restart sidecar", "Home", or any other nav target.
    expect(screen.queryByText(/Restart sidecar/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Home/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Setup/i)).not.toBeInTheDocument();
  });

  it("Open log file is disabled when logFilePath is null", () => {
    render(<FatalErrorScreen info={MISMATCH} adapter={makeAdapter(null)} />);
    expect(screen.getByTestId("open-log")).toBeDisabled();
  });

  it("Open log file calls adapter.openLog with the path when set", async () => {
    const adapter = makeAdapter("/tmp/agentshore.log");
    render(<FatalErrorScreen info={MISMATCH} adapter={adapter} />);
    await userEvent.click(screen.getByTestId("open-log"));
    expect(adapter.openLog).toHaveBeenCalledWith("/tmp/agentshore.log");
  });

  it("Quit calls adapter.quitApp", async () => {
    const adapter = makeAdapter("/tmp/agentshore.log");
    render(<FatalErrorScreen info={MISMATCH} adapter={adapter} />);
    await userEvent.click(screen.getByTestId("quit-app"));
    expect(adapter.quitApp).toHaveBeenCalledTimes(1);
  });
});

describe("FatalErrorScreen — other failures", () => {
  const OTHER: FatalShellInfo = {
    kind: "other",
    reason: "spawn sidecar: ENOENT",
  };

  it("renders the generic headline + reason for non-mismatch failures", () => {
    render(<FatalErrorScreen info={OTHER} adapter={makeAdapter()} />);
    expect(screen.getByText(/AgentShore sidecar failed to start/i)).toBeInTheDocument();
    expect(screen.getByTestId("fatal-reason").textContent).toBe(
      "spawn sidecar: ENOENT",
    );
  });
});

describe("FatalErrorScreen — null payload", () => {
  it("renders a minimal Quit-only fallback when info is null", () => {
    const adapter = makeAdapter("/tmp/agentshore.log");
    render(<FatalErrorScreen info={null} adapter={adapter} />);
    expect(screen.getByText(/unknown fatal state/i)).toBeInTheDocument();
    expect(screen.getByTestId("quit-app")).toBeInTheDocument();
    expect(screen.queryByTestId("open-log")).not.toBeInTheDocument();
    expect(screen.queryByTestId("fatal-detail")).not.toBeInTheDocument();
  });
});
