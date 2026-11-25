import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { ChooseProjectScreen } from "../ChooseProjectScreen";
import type {
  ChooseProjectAdapter,
  QuickStartSetupStep,
} from "../ChooseProjectScreen";
import type { RecentEntry } from "../../rpc/recentsClient";
import type { StartFromPersistedSetupOptions } from "../../setup/startFromPersistedSetup";

function makeAdapter(entries: RecentEntry[]): ChooseProjectAdapter & {
  list: ReturnType<typeof vi.fn>;
  touch: ReturnType<typeof vi.fn>;
  remove: ReturnType<typeof vi.fn>;
  select: ReturnType<typeof vi.fn>;
  openDirectory: ReturnType<typeof vi.fn>;
  quickStart: ReturnType<typeof vi.fn>;
  hasPersistedSetup: ReturnType<typeof vi.fn>;
  setBudgetImpl: ReturnType<typeof vi.fn>;
  readPersistedBudgetImpl: ReturnType<typeof vi.fn>;
} {
  return {
    list: vi.fn().mockResolvedValue(entries),
    touch: vi.fn().mockResolvedValue(undefined),
    remove: vi.fn().mockResolvedValue(undefined),
    select: vi.fn().mockResolvedValue({ path: "" }),
    openDirectory: vi.fn().mockResolvedValue(null),


    quickStart: vi.fn().mockResolvedValue(undefined),


    hasPersistedSetup: vi.fn().mockReturnValue(true),



    setBudgetImpl: vi
      .fn()
      .mockResolvedValue({
        budget: { enabled: true, total: 25, warning_threshold: 0.2 },
        yaml_path: "/tmp/agentshore.yaml",
      }),



    readPersistedBudgetImpl: vi
      .fn()
      .mockReturnValue({ mode: "capped", total: 25 }),
  };
}

function renderScreen(
  adapter: ChooseProjectAdapter,
  onProjectSelected?: (path: string) => void | Promise<void>,
  onQuickStartFailed?: (
    path: string,
    error: Error,
    failedStep: QuickStartSetupStep,
  ) => void,
) {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route
          path="/"
          element={
            <ChooseProjectScreen
              adapter={adapter}
              onProjectSelected={onProjectSelected}
              onQuickStartFailed={onQuickStartFailed}
            />
          }
        />
        <Route path="/setup/readiness" element={<div data-testid="readiness">Readiness</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

const ENTRIES: RecentEntry[] = [
  {
    path: "/Users/user/example-repo",
    label: "example-repo",
    last_started: "2026-05-15T00:00:00+00:00",
    last_exit_reason: null,
    has_valid_config: true,
  },
  {
    path: "/Users/example/sample-project",
    label: "sample-project",
    last_started: "2026-05-10T00:00:00+00:00",
    last_exit_reason: "user_quit",
    has_valid_config: false,
  },
];

describe("ChooseProjectScreen", () => {
  it("renders rows with Ready badge when has_valid_config is true", async () => {
    const adapter = makeAdapter(ENTRIES);
    renderScreen(adapter);

    await waitFor(() => expect(adapter.list).toHaveBeenCalled());
    const readyRow = await screen.findByTestId("recent-row-/Users/user/example-repo");
    expect(within(readyRow).getByText("Ready")).toBeInTheDocument();
  });

  it("renders rows with Known badge when has_valid_config is false", async () => {
    const adapter = makeAdapter(ENTRIES);
    renderScreen(adapter);

    const knownRow = await screen.findByTestId("recent-row-/Users/example/sample-project");
    expect(within(knownRow).getByText("Known")).toBeInTheDocument();
  });

  it("renders empty state when adapter returns []", async () => {
    const adapter = makeAdapter([]);
    renderScreen(adapter);

    await waitFor(() => expect(adapter.list).toHaveBeenCalled());
    expect(await screen.findByText(/No recent projects yet/i)).toBeInTheDocument();
  });

  it("clicking a row selects and navigates to /setup/readiness", async () => {
    const adapter = makeAdapter(ENTRIES);
    renderScreen(adapter);
    const user = userEvent.setup();

    const row = await screen.findByTestId("recent-row-/Users/user/example-repo");
    await user.click(row);

    await waitFor(() => {
      expect(adapter.select).toHaveBeenCalledWith("/Users/user/example-repo");
    });
    expect(await screen.findByTestId("readiness")).toBeInTheDocument();
    await waitFor(() => {
      expect(adapter.touch).toHaveBeenCalledWith("/Users/user/example-repo");
    });
    const selectOrder = adapter.select.mock.invocationCallOrder[0];
    const touchOrder = adapter.touch.mock.invocationCallOrder[0];
    expect(selectOrder).toBeLessThan(touchOrder);
  });

  it("shows a busy state while opening a repository from the dialog", async () => {
    const adapter = makeAdapter(ENTRIES);
    adapter.openDirectory.mockResolvedValue("/Users/user/example-repo");
    let resolveSelect: (value: unknown) => void = () => undefined;
    adapter.select.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSelect = resolve;
        }),
    );
    renderScreen(adapter);
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Open repository" }));

    await waitFor(() => {
      expect(adapter.select).toHaveBeenCalledWith("/Users/user/example-repo");
    });
    expect(screen.getByRole("button", { name: "Opening..." })).toBeDisabled();
    expect(
      await screen.findByText("Opening", { selector: "span" }),
    ).toBeInTheDocument();

    resolveSelect({ path: "/Users/user/example-repo" });
    expect(await screen.findByTestId("readiness")).toBeInTheDocument();
  });

  it("does not run setup hydration from the chooser click path", async () => {
    const adapter = makeAdapter(ENTRIES);
    const onProjectSelected = vi.fn().mockResolvedValue(undefined);
    renderScreen(adapter, onProjectSelected);
    const user = userEvent.setup();

    const row = await screen.findByTestId("recent-row-/Users/user/example-repo");
    await user.click(row);

    expect(await screen.findByTestId("readiness")).toBeInTheDocument();
    expect(onProjectSelected).not.toHaveBeenCalled();
  });

  it("ignores onProjectSelected failures because chooser does not call it", async () => {
    const adapter = makeAdapter(ENTRIES);
    const onProjectSelected = vi.fn().mockRejectedValue(new Error("inspect failed"));
    renderScreen(adapter, onProjectSelected);
    const user = userEvent.setup();

    const row = await screen.findByTestId("recent-row-/Users/user/example-repo");
    await user.click(row);

    expect(await screen.findByTestId("readiness")).toBeInTheDocument();
    expect(onProjectSelected).not.toHaveBeenCalled();
  });

  describe("Quick Start button (issue #565)", () => {
    const READY_ENTRY: RecentEntry = {
      path: "/Users/user/example-repo",
      label: "example-repo",
      last_started: "2026-05-15T00:00:00+00:00",
      last_exit_reason: null,
      has_valid_config: true,
    };
    const KNOWN_ENTRY: RecentEntry = {
      path: "/Users/example/sample-project",
      label: "sample-project",
      last_started: "2026-05-10T00:00:00+00:00",
      last_exit_reason: "user_quit",
      has_valid_config: false,
    };
    const NEVER_STARTED_ENTRY: RecentEntry = {
      path: "/Users/example/never-run",
      label: "never-run",


      last_started: "",
      last_exit_reason: null,
      has_valid_config: true,
    };

    it("renders Quick Start on Ready+launched rows with persisted setup", async () => {
      const adapter = makeAdapter([READY_ENTRY]);
      renderScreen(adapter);

      const row = await screen.findByTestId("recent-row-/Users/user/example-repo");
      expect(within(row).getByText("Quick Start")).toBeInTheDocument();
      expect(adapter.hasPersistedSetup).toHaveBeenCalledWith("/Users/user/example-repo");
    });

    it("does NOT render Quick Start when no persisted setup exists", async () => {
      const adapter = makeAdapter([READY_ENTRY]);
      adapter.hasPersistedSetup = vi.fn().mockReturnValue(false);
      renderScreen(adapter);

      const row = await screen.findByTestId("recent-row-/Users/user/example-repo");
      expect(within(row).queryByText("Quick Start")).not.toBeInTheDocument();
    });

    it("does NOT render Quick Start on Ready-but-never-launched rows", async () => {
      const adapter = makeAdapter([NEVER_STARTED_ENTRY]);
      renderScreen(adapter);

      const row = await screen.findByTestId("recent-row-/Users/example/never-run");
      expect(within(row).queryByText("Quick Start")).not.toBeInTheDocument();
    });

    it("does NOT render Quick Start on Known (not Ready) rows", async () => {
      const adapter = makeAdapter([KNOWN_ENTRY]);
      renderScreen(adapter);

      const row = await screen.findByTestId(
        "recent-row-/Users/example/sample-project",
      );
      expect(within(row).queryByText("Quick Start")).not.toBeInTheDocument();
    });

    it("clicking Quick Start invokes the shared helper without entering Setup", async () => {
      const adapter = makeAdapter([READY_ENTRY]);
      renderScreen(adapter);
      const user = userEvent.setup();

      const button = await screen.findByTestId(
        "quick-start-/Users/user/example-repo",
      );
      await user.click(button);

      await waitFor(() => {
        expect(adapter.touch).toHaveBeenCalledWith("/Users/user/example-repo");
        expect(adapter.quickStart).toHaveBeenCalledTimes(1);
      });
      const [pathArg, opts] = adapter.quickStart.mock.calls[0] as [
        string,
        StartFromPersistedSetupOptions,
      ];
      expect(pathArg).toBe("/Users/user/example-repo");
      expect(typeof opts.navigate).toBe("function");


      expect(adapter.select).toHaveBeenCalledWith("/Users/user/example-repo");
      expect(screen.queryByTestId("readiness")).not.toBeInTheDocument();
    });

    it("persists the localStorage budget via setBudget BEFORE invoking the helper", async () => {





      const adapter = makeAdapter([READY_ENTRY]);
      adapter.readPersistedBudgetImpl = vi
        .fn()
        .mockReturnValue({ mode: "capped", total: 42 });
      renderScreen(adapter);
      const user = userEvent.setup();

      const button = await screen.findByTestId(
        "quick-start-/Users/user/example-repo",
      );
      await user.click(button);

      await waitFor(() => {
        expect(adapter.setBudgetImpl).toHaveBeenCalledTimes(1);
        expect(adapter.quickStart).toHaveBeenCalledTimes(1);
      });
      expect(adapter.setBudgetImpl).toHaveBeenCalledWith({
        enabled: true,
        total: 42,
        time_enabled: false,
        time_total_minutes: 0,
      });


      const budgetOrder =
        adapter.setBudgetImpl.mock.invocationCallOrder[0];
      const helperOrder = adapter.quickStart.mock.invocationCallOrder[0];
      expect(budgetOrder).toBeLessThan(helperOrder);
    });

    it("skips setBudget when no budget was persisted but still calls the helper", async () => {



      const adapter = makeAdapter([READY_ENTRY]);
      adapter.readPersistedBudgetImpl = vi.fn().mockReturnValue(null);
      renderScreen(adapter);
      const user = userEvent.setup();

      const button = await screen.findByTestId(
        "quick-start-/Users/user/example-repo",
      );
      await user.click(button);

      await waitFor(() => {
        expect(adapter.quickStart).toHaveBeenCalledTimes(1);
      });
      expect(adapter.setBudgetImpl).not.toHaveBeenCalled();
    });

    it("routes a setBudget failure through onQuickStartFailed with failedStep=budget and skips the helper", async () => {
      const adapter = makeAdapter([READY_ENTRY]);
      adapter.setBudgetImpl = vi
        .fn()
        .mockRejectedValue(new Error("yaml write failed"));
      const onQuickStartFailed = vi.fn();
      renderScreen(adapter, undefined, onQuickStartFailed);
      const user = userEvent.setup();

      const button = await screen.findByTestId(
        "quick-start-/Users/user/example-repo",
      );
      await user.click(button);

      await waitFor(() => {
        expect(onQuickStartFailed).toHaveBeenCalledTimes(1);
      });
      const [path, err, step] = onQuickStartFailed.mock.calls[0];
      expect(path).toBe("/Users/user/example-repo");
      expect(err).toBeInstanceOf(Error);
      expect((err as Error).message).toBe("yaml write failed");
      expect(step).toBe("budget");


      expect(adapter.quickStart).not.toHaveBeenCalled();
    });

    it("maps the helper's ``select`` failure to the readiness setup step", async () => {
      const adapter = makeAdapter([READY_ENTRY]);
      adapter.quickStart = vi.fn(
        async (_path: string, opts: StartFromPersistedSetupOptions = {}) => {
          opts.onError?.(new Error("project gone"), "select");
        },
      );
      const onQuickStartFailed = vi.fn();
      renderScreen(adapter, undefined, onQuickStartFailed);
      const user = userEvent.setup();

      const button = await screen.findByTestId(
        "quick-start-/Users/user/example-repo",
      );
      await user.click(button);

      await waitFor(() => {
        expect(onQuickStartFailed).toHaveBeenCalledTimes(1);
      });
      const [, err, step] = onQuickStartFailed.mock.calls[0];
      expect((err as Error).message).toBe("project gone");
      expect(step).toBe("readiness");
    });

    it("maps the helper's ``start`` failure to the start setup step", async () => {
      const adapter = makeAdapter([READY_ENTRY]);
      adapter.quickStart = vi.fn(
        async (_path: string, opts: StartFromPersistedSetupOptions = {}) => {
          opts.onError?.(new Error("sidecar busy"), "start");
        },
      );
      const onQuickStartFailed = vi.fn();
      renderScreen(adapter, undefined, onQuickStartFailed);
      const user = userEvent.setup();

      const button = await screen.findByTestId(
        "quick-start-/Users/user/example-repo",
      );
      await user.click(button);

      await waitFor(() => {
        expect(onQuickStartFailed).toHaveBeenCalledTimes(1);
      });
      const [, , step] = onQuickStartFailed.mock.calls[0];
      expect(step).toBe("start");
    });

    it("falls back to local error banner when no onQuickStartFailed wired", async () => {
      const adapter = makeAdapter([READY_ENTRY]);
      adapter.quickStart = vi.fn(
        async (_path: string, opts: StartFromPersistedSetupOptions = {}) => {
          opts.onError?.(new Error("backend unreachable"), "select");
        },
      );
      renderScreen(adapter);
      const user = userEvent.setup();

      const button = await screen.findByTestId(
        "quick-start-/Users/user/example-repo",
      );
      await user.click(button);

      const alert = await screen.findByRole("alert");
      expect(alert.textContent).toContain("Quick Start failed");
      expect(alert.textContent).toContain("backend unreachable");
    });



    it("exports the QuickStartSetupStep union with the budget step", () => {
      const sample: QuickStartSetupStep = "budget";
      expect(sample).toBe("budget");
    });
  });

  it("clicking Remove on a row calls remove and removes the row from view", async () => {
    const adapter = makeAdapter(ENTRIES);
    renderScreen(adapter);
    const user = userEvent.setup();

    const row = await screen.findByTestId("recent-row-/Users/user/example-repo");
    const removeButton = within(row).getByRole("button", { name: /remove/i });
    await user.click(removeButton);

    await waitFor(() => {
      expect(adapter.remove).toHaveBeenCalledWith("/Users/user/example-repo");
    });
    await waitFor(() => {
      expect(screen.queryByTestId("recent-row-/Users/user/example-repo")).not.toBeInTheDocument();
    });

    expect(screen.getByTestId("recent-row-/Users/example/sample-project")).toBeInTheDocument();

    expect(adapter.touch).not.toHaveBeenCalled();
    expect(adapter.select).not.toHaveBeenCalled();
  });
});
