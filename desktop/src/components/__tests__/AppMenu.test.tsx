import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";

// Capture the event handlers AppMenu registers so tests can fire menu events.
const listeners = new Map<string, (event: { payload: unknown }) => void>();
vi.mock("@tauri-apps/api/event", () => ({
  listen: vi.fn((name: string, handler: (event: { payload: unknown }) => void) => {
    listeners.set(name, handler);
    return Promise.resolve(() => listeners.delete(name));
  }),
}));

import { AppMenu, type AppMenuAdapter, type AvailableUpdate } from "../AppMenu";

const DISABLEABLE = ["cleanup", "groom_backlog", "prune", "run_qa"];

function makeAdapter(overrides: Partial<AppMenuAdapter> = {}): AppMenuAdapter {
  return {
    checkForUpdate: vi.fn(async () => null),
    relaunch: vi.fn(async () => undefined),
    openLogFolder: vi.fn(async () => undefined),
    copyText: vi.fn(async () => true),
    getPreferences: vi.fn(async () => ({
      disabled_plays: [],
      disableable_plays: DISABLEABLE,
    })),
    setPreferences: vi.fn(async (disabled: string[]) => ({
      disabled_plays: disabled,
      disableable_plays: DISABLEABLE,
    })),
    ...overrides,
  };
}

async function fire(event: string, payload: unknown = undefined) {
  await act(async () => {
    listeners.get(event)?.({ payload });
  });
}

const DIAGNOSTICS = { app: "AgentShore", version: "9.9.9", os: "macos", arch: "aarch64" };

describe("AppMenu", () => {
  beforeEach(() => {
    listeners.clear();
  });

  it("loads the disableable-play list as enabled toggles on menu:preferences", async () => {
    const adapter = makeAdapter();
    render(<AppMenu adapter={adapter} />);
    await fire("menu:preferences");
    expect(screen.getByTestId("preferences-dialog")).toBeInTheDocument();
    expect(adapter.getPreferences).toHaveBeenCalledTimes(1);
    // Polarity: a play not in disabled_plays renders checked (enabled).
    const runQa = await screen.findByTestId("preferences-play-run_qa");
    expect(runQa).toBeChecked();
    expect(screen.getByTestId("preferences-play-run_qa-state")).toHaveTextContent("Enabled");
    expect(screen.getByText("Run QA")).toBeInTheDocument();
  });

  it("reflects a disabled play as an off toggle", async () => {
    const adapter = makeAdapter({
      getPreferences: vi.fn(async () => ({
        disabled_plays: ["cleanup"],
        disableable_plays: DISABLEABLE,
      })),
    });
    render(<AppMenu adapter={adapter} />);
    await fire("menu:preferences");
    const cleanup = await screen.findByTestId("preferences-play-cleanup");
    expect(cleanup).not.toBeChecked();
    expect(screen.getByTestId("preferences-play-cleanup-state")).toHaveTextContent("Disabled");
  });

  it("disables a play when its toggle is turned off and saved", async () => {
    const adapter = makeAdapter();
    render(<AppMenu adapter={adapter} />);
    await fire("menu:preferences");
    const checkbox = await screen.findByTestId("preferences-play-cleanup");
    expect(checkbox).toBeChecked(); // starts enabled
    await act(async () => {
      checkbox.click(); // toggle off → disable
    });
    expect(checkbox).not.toBeChecked();
    await act(async () => {
      screen.getByTestId("preferences-dialog-primary").click();
    });
    expect(adapter.setPreferences).toHaveBeenCalledWith(["cleanup"]);
    // Dialog closes on a successful save.
    await waitFor(() =>
      expect(screen.queryByTestId("preferences-dialog")).not.toBeInTheDocument(),
    );
  });

  it("surfaces a rejected play on Save without closing", async () => {
    const adapter = makeAdapter({
      setPreferences: vi.fn(async () => {
        throw new Error("not user-disableable: issue_pickup");
      }),
    });
    render(<AppMenu adapter={adapter} />);
    await fire("menu:preferences");
    await screen.findByTestId("preferences-play-run_qa");
    await act(async () => {
      screen.getByTestId("preferences-dialog-primary").click();
    });
    expect(await screen.findByTestId("preferences-error")).toHaveTextContent(
      "not user-disableable",
    );
    expect(screen.getByTestId("preferences-dialog")).toBeInTheDocument();
  });

  it("shows the keyboard-shortcut cheat-sheet on menu:keyboard_shortcuts", async () => {
    render(<AppMenu adapter={makeAdapter()} />);
    await fire("menu:keyboard_shortcuts");
    expect(screen.getByTestId("keyboard-shortcuts-dialog")).toBeInTheDocument();
    expect(screen.getByText("Adjust Budget")).toBeInTheDocument();
    expect(screen.getByText("Stop Session")).toBeInTheDocument();
  });

  it("renders and copies diagnostics on menu:copy_diagnostics", async () => {
    const adapter = makeAdapter();
    render(<AppMenu adapter={adapter} />);
    await fire("menu:copy_diagnostics", DIAGNOSTICS);

    const text = screen.getByTestId("diagnostics-text");
    expect(text).toHaveTextContent("AgentShore 9.9.9");
    expect(text).toHaveTextContent("OS: macos (aarch64)");

    await act(async () => {
      screen.getByTestId("diagnostics-dialog-primary").click();
    });
    expect(adapter.copyText).toHaveBeenCalledWith(
      "AgentShore 9.9.9\nOS: macos (aarch64)",
    );
    await waitFor(() =>
      expect(screen.getByTestId("diagnostics-dialog-primary")).toHaveTextContent(
        "Copied",
      ),
    );
  });

  it("invokes openLogFolder on menu:open_logs", async () => {
    const adapter = makeAdapter();
    render(<AppMenu adapter={adapter} />);
    await fire("menu:open_logs");
    expect(adapter.openLogFolder).toHaveBeenCalledTimes(1);
  });

  it("prompts on a silent check when an update is available", async () => {
    const update: AvailableUpdate = {
      version: "2.0.0",
      currentVersion: "1.0.0",
      notes: "Shiny new things",
      install: vi.fn(async () => undefined),
    };
    const adapter = makeAdapter({ checkForUpdate: vi.fn(async () => update) });
    render(<AppMenu adapter={adapter} />);
    expect(await screen.findByTestId("update-dialog")).toBeInTheDocument();
    expect(screen.getByTestId("update-notes")).toHaveTextContent(
      "Shiny new things",
    );
  });

  it("installs and relaunches when the update is accepted", async () => {
    const install = vi.fn(async () => undefined);
    const update: AvailableUpdate = {
      version: "2.0.0",
      currentVersion: "1.0.0",
      notes: null,
      install,
    };
    const adapter = makeAdapter({ checkForUpdate: vi.fn(async () => update) });
    render(<AppMenu adapter={adapter} />);
    await screen.findByTestId("update-dialog");
    await act(async () => {
      screen.getByTestId("update-dialog-primary").click();
    });
    await waitFor(() => expect(install).toHaveBeenCalledTimes(1));
    expect(adapter.relaunch).toHaveBeenCalledTimes(1);
  });

  it("reports 'up to date' on a manual check with no update", async () => {
    render(<AppMenu adapter={makeAdapter()} />);
    await fire("menu:check_updates");
    expect(await screen.findByTestId("no-update-dialog")).toBeInTheDocument();
  });

  it("surfaces an error on a failed manual check", async () => {
    const adapter = makeAdapter({
      checkForUpdate: vi
        .fn()
        .mockResolvedValueOnce(null) // silent mount check
        .mockRejectedValueOnce(new Error("offline")), // manual check
    });
    render(<AppMenu adapter={adapter} />);
    await fire("menu:check_updates");
    expect(await screen.findByTestId("update-error-dialog")).toBeInTheDocument();
  });
});
