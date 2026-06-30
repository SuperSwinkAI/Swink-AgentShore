import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from "vitest";
import { render, screen, within, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));

import { StartingProgress } from "../StartingProgress";
import {
  buildInitialSteps,
  applyProgressEvent,
  STEP_INIT_BEADS,
  STEP_METADATA,
  type StartupStepState,
} from "../startupSteps";

afterEach(() => cleanup());

function renderScreen(
  steps: StartupStepState[],
  {
    onRetry = vi.fn<(stepId: string) => void>(),
    onCancel = vi.fn<() => void>(),
    projectName,
  }: {
    onRetry?: Mock<(stepId: string) => void>;
    onCancel?: Mock<() => void>;
    projectName?: string;
  } = {},
) {
  return {
    onRetry,
    onCancel,
    ...render(
      <MemoryRouter>
        <StartingProgress
          steps={steps}
          projectName={projectName}
          onRetry={onRetry}
          onCancel={onCancel}
        />
      </MemoryRouter>,
    ),
  };
}

describe("StartingProgress", () => {
  it("routes init_beads repair to the readiness setup screen", () => {
    expect(STEP_METADATA[STEP_INIT_BEADS].repairScreen).toBe("/setup/readiness");
  });

  let steps: StartupStepState[];

  beforeEach(() => {
    steps = buildInitialSteps();
  });

  it("renders all 7 startup steps", () => {
    renderScreen(steps);
    const list = screen.getByRole("list");
    expect(within(list).getAllByRole("listitem")).toHaveLength(7);
  });

  it("shows project name when provided", () => {
    renderScreen(steps, { projectName: "example-repo" });
    // getByText throws if not found — that's the assertion
    expect(screen.getByText("example-repo")).toBeTruthy();
  });

  it("shows progress count header", () => {
    renderScreen(steps);
    const counter = screen.getByTestId("progress-count");
    expect(counter.textContent).toBe("0/7");
  });

  it("updates progress count as steps complete", () => {
    const updated = applyProgressEvent(
      applyProgressEvent(steps, "config_merge", "ok"),
      "install_skills",
      "ok",
    );
    renderScreen(updated);
    expect(screen.getByTestId("progress-count").textContent).toBe("2/7");
  });

  it("renders cancel startup button", () => {
    renderScreen(steps);
    expect(screen.getByTestId("cancel-startup")).toBeTruthy();
  });

  it("calls onCancel when Cancel startup is clicked", async () => {
    const user = userEvent.setup();
    const { onCancel } = renderScreen(steps);
    await user.click(screen.getByTestId("cancel-startup"));
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it("shows step label and description for each step", () => {
    renderScreen(steps);
    expect(screen.getByText("Config merged")).toBeTruthy();
    expect(screen.getByText("Skills installed")).toBeTruthy();
    expect(screen.getByText("Beads ready")).toBeTruthy();
    expect(screen.getByText("IPC endpoint bound")).toBeTruthy();
    expect(screen.getByText("Dashboard bridge starting")).toBeTruthy();
    expect(screen.getByText("First state snapshot")).toBeTruthy();
  });

  describe("pending step", () => {
    it("shows aria-label as pending", () => {
      renderScreen(steps);
      const stepEl = screen.getByTestId("step-config_merge");
      expect(stepEl.getAttribute("aria-label")).toBe("Config merged: pending");
    });

    it("does not show retry button for pending step", () => {
      renderScreen(steps);
      expect(screen.queryByTestId("retry-config_merge")).toBeNull();
    });
  });

  describe("running step", () => {
    it("shows a spinner indicator for the running step", () => {
      const updated = applyProgressEvent(steps, "config_merge", "running");
      renderScreen(updated);
      const stepEl = screen.getByTestId("step-config_merge");
      expect(within(stepEl).getByRole("progressbar")).toBeTruthy();
    });

    it("marks the step aria-label as running", () => {
      const updated = applyProgressEvent(steps, "install_skills", "running");
      renderScreen(updated);
      expect(screen.getByTestId("step-install_skills").getAttribute("aria-label")).toBe(
        "Skills installed: running",
      );
    });
  });

  describe("ok step", () => {
    it("marks step aria-label as ok", () => {
      const updated = applyProgressEvent(steps, "init_beads", "ok");
      renderScreen(updated);
      expect(screen.getByTestId("step-init_beads").getAttribute("aria-label")).toBe(
        "Beads ready: ok",
      );
    });

    it("does not show retry button for ok step", () => {
      const updated = applyProgressEvent(steps, "config_merge", "ok");
      renderScreen(updated);
      expect(screen.queryByTestId("retry-config_merge")).toBeNull();
    });
  });

  describe("failed step — no repair screen (Retry only)", () => {
    let failedSteps: StartupStepState[];

    beforeEach(() => {
      failedSteps = applyProgressEvent(
        steps,
        "bind_ipc",
        "failed",
        "Port 9400 is already in use",
      );
    });

    it("shows step as failed", () => {
      renderScreen(failedSteps);
      expect(screen.getByTestId("step-bind_ipc").getAttribute("aria-label")).toBe(
        "IPC endpoint bound: failed",
      );
    });

    it("shows the error message inline", () => {
      renderScreen(failedSteps);
      const errEl = screen.getByTestId("error-bind_ipc");
      expect(errEl.textContent).toBe("Port 9400 is already in use");
    });

    it("shows a Retry button", () => {
      renderScreen(failedSteps);
      expect(screen.getByTestId("retry-bind_ipc")).toBeTruthy();
    });

    it("does not show a Go to setup link when repairScreen is null", () => {
      renderScreen(failedSteps);
      expect(screen.queryByTestId("repair-link-bind_ipc")).toBeNull();
    });

    it("calls onRetry with the step id when Retry is clicked", async () => {
      const user = userEvent.setup();
      const { onRetry } = renderScreen(failedSteps);
      await user.click(screen.getByTestId("retry-bind_ipc"));
      expect(onRetry).toHaveBeenCalledWith("bind_ipc");
    });

    it("renders error in an alert role for accessibility", () => {
      renderScreen(failedSteps);
      const stepEl = screen.getByTestId("step-bind_ipc");
      expect(within(stepEl).getByRole("alert")).toBeTruthy();
    });
  });

  describe("failed step — with repair screen", () => {
    let failedSteps: StartupStepState[];

    beforeEach(() => {
      failedSteps = applyProgressEvent(
        steps,
        "config_merge",
        "failed",
        "agentshore.yaml parse error: unknown key 'agnt'",
      );
    });

    it("shows a Go to setup link pointing to the repair screen", () => {
      renderScreen(failedSteps);
      const link = screen.getByTestId("repair-link-config_merge");
      expect(link).toBeTruthy();
      expect(link.getAttribute("href")).toBe("/setup/agents");
    });

    it("also shows a Retry button alongside the setup link", () => {
      renderScreen(failedSteps);
      expect(screen.getByTestId("retry-config_merge")).toBeTruthy();
    });

    it("shows the error message", () => {
      renderScreen(failedSteps);
      expect(screen.getByTestId("error-config_merge").textContent).toBe(
        "agentshore.yaml parse error: unknown key 'agnt'",
      );
    });
  });

  describe("failed step — generic error fallback", () => {
    it("shows fallback message when error is null", () => {
      const failedSteps = applyProgressEvent(steps, "start_bridge", "failed", null);
      renderScreen(failedSteps);
      expect(screen.getByTestId("error-start_bridge").textContent).toBe(
        "An unexpected error occurred.",
      );
    });
  });
});

describe("buildInitialSteps", () => {
  it("produces 7 steps all in pending state", () => {
    const steps = buildInitialSteps();
    expect(steps).toHaveLength(7);
    expect(steps.every((s) => s.status === "pending")).toBe(true);
    expect(steps.every((s) => s.error === null)).toBe(true);
  });

  it("assigns correct labels to each step", () => {
    const steps = buildInitialSteps();
    const labels = steps.map((s) => s.label);
    expect(labels).toEqual([
      "Config merged",
      "Agent auth & identities checked",
      "Skills installed",
      "Beads ready",
      "IPC endpoint bound",
      "Dashboard bridge starting",
      "First state snapshot",
    ]);
  });
});

describe("applyProgressEvent", () => {
  it("transitions the target step to running without touching others", () => {
    const initial = buildInitialSteps();
    const updated = applyProgressEvent(initial, "install_skills", "running");
    expect(updated.find((s) => s.id === "install_skills")!.status).toBe("running");
    // Every other step stays pending.
    for (const s of updated) {
      if (s.id !== "install_skills") expect(s.status).toBe("pending");
    }
  });

  it("clears error when step transitions to ok after a failure", () => {
    const withError = applyProgressEvent(
      buildInitialSteps(),
      "bind_ipc",
      "failed",
      "Port in use",
    );
    const recovered = applyProgressEvent(withError, "bind_ipc", "ok");
    const step = recovered.find((s) => s.id === "bind_ipc")!;
    expect(step.status).toBe("ok");
    expect(step.error).toBeNull();
  });

  it("does not mutate the input array", () => {
    const initial = buildInitialSteps();
    const copy = initial.map((s) => ({ ...s }));
    applyProgressEvent(initial, "config_merge", "running");
    expect(initial).toEqual(copy);
  });
});
