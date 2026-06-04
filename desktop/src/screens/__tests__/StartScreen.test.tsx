import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import {
  StartScreen,
  type StartGateBlockers,
  type StartScreenAdapter,
  type StartSelection,
} from "../StartScreen";

const READY: StartGateBlockers = { targetBranch: false, agents: false, identities: false };

function renderScreen(props: {
  blockers: StartGateBlockers;
  selection: StartSelection;
  onChange?: (next: StartSelection) => void;
  onStart?: (selection: StartSelection) => void;
  adapter?: StartScreenAdapter;
}) {
  const onChange = props.onChange ?? vi.fn();
  const onStart = props.onStart ?? vi.fn();
  const utils = render(
    <MemoryRouter>
      <StartScreen
        blockers={props.blockers}
        selection={props.selection}
        onChange={onChange}
        onStart={onStart}
        adapter={props.adapter}
      />
    </MemoryRouter>,
  );
  return { ...utils, onChange, onStart };
}

describe("StartScreen — gate", () => {
  it("enables Start when all blockers are clear, no seed required", () => {
    renderScreen({ blockers: READY, selection: { seedInputPath: null } });
    expect(screen.getByTestId("start-session")).not.toBeDisabled();
  });

  it("still enables Start when a seed is supplied", () => {
    renderScreen({ blockers: READY, selection: { seedInputPath: "/tmp/seed.yaml" } });
    expect(screen.getByTestId("start-session")).not.toBeDisabled();
  });

  it("disables Start when target branch is missing and shows hint", () => {
    renderScreen({
      blockers: { ...READY, targetBranch: true },
      selection: { seedInputPath: null },
    });
    expect(screen.getByTestId("start-session")).toBeDisabled();
    expect(screen.getByTestId("start-gate-hint")).toHaveTextContent(/target branch/);
  });

  it("hint lists every missing blocker", () => {
    renderScreen({
      blockers: { targetBranch: true, agents: true, identities: true },
      selection: { seedInputPath: null },
    });
    const hint = screen.getByTestId("start-gate-hint");
    expect(hint).toHaveTextContent(/target branch/);
    expect(hint).toHaveTextContent(/two enabled agents/);
    expect(hint).toHaveTextContent(/two GitHub identities/);
  });
});

describe("StartScreen — seed input", () => {
  it("renders the empty-state placeholder when no seed selected", () => {
    renderScreen({ blockers: READY, selection: { seedInputPath: null } });
    expect(screen.getByTestId("seed-file-path")).toHaveTextContent(/agent will sweep the project/);
    expect(screen.queryByTestId("seed-clear")).not.toBeInTheDocument();
  });

  it("calls adapter.openFile and forwards the path to onChange when picker resolves", async () => {
    const adapter: StartScreenAdapter = {
      openFile: vi.fn().mockResolvedValue("/tmp/seed.yaml"),
      openFolder: vi.fn().mockResolvedValue(null),
    };
    const onChange = vi.fn();
    renderScreen({
      blockers: READY,
      selection: { seedInputPath: null },
      onChange,
      adapter,
    });
    const user = userEvent.setup();

    await user.click(screen.getByTestId("seed-file-pick"));

    await waitFor(() =>
      expect(onChange).toHaveBeenCalledWith({ seedInputPath: "/tmp/seed.yaml" }),
    );
  });

  it("ignores a null openFile result (user cancelled the dialog)", async () => {
    const adapter: StartScreenAdapter = {
      openFile: vi.fn().mockResolvedValue(null),
      openFolder: vi.fn().mockResolvedValue(null),
    };
    const onChange = vi.fn();
    renderScreen({
      blockers: READY,
      selection: { seedInputPath: null },
      onChange,
      adapter,
    });
    const user = userEvent.setup();

    await user.click(screen.getByTestId("seed-file-pick"));

    await waitFor(() => expect(adapter.openFile).toHaveBeenCalled());
    expect(onChange).not.toHaveBeenCalled();
  });

  it("calls adapter.openFolder and forwards the path to onChange when folder picker resolves", async () => {
    const adapter: StartScreenAdapter = {
      openFile: vi.fn().mockResolvedValue(null),
      openFolder: vi.fn().mockResolvedValue("/tmp/seed-dir"),
    };
    const onChange = vi.fn();
    renderScreen({
      blockers: READY,
      selection: { seedInputPath: null },
      onChange,
      adapter,
    });
    const user = userEvent.setup();

    await user.click(screen.getByTestId("seed-folder-pick"));

    await waitFor(() =>
      expect(onChange).toHaveBeenCalledWith({ seedInputPath: "/tmp/seed-dir" }),
    );
  });

  it("renders the selected path and offers a Clear button", () => {
    renderScreen({ blockers: READY, selection: { seedInputPath: "/tmp/seed.yaml" } });
    expect(screen.getByTestId("seed-file-path")).toHaveTextContent("/tmp/seed.yaml");
    expect(screen.getByTestId("seed-clear")).toBeInTheDocument();
  });

  it("Clear emits null seedInputPath", async () => {
    const onChange = vi.fn();
    renderScreen({
      blockers: READY,
      selection: { seedInputPath: "/tmp/seed.yaml" },
      onChange,
    });
    const user = userEvent.setup();

    await user.click(screen.getByTestId("seed-clear"));

    expect(onChange).toHaveBeenCalledWith({ seedInputPath: null });
  });
});

describe("StartScreen — onStart invocation", () => {
  it("calls onStart with the current selection when the gate is open", async () => {
    const onStart = vi.fn();
    renderScreen({
      blockers: READY,
      selection: { seedInputPath: "/tmp/seed.yaml" },
      onStart,
    });
    const user = userEvent.setup();

    await user.click(screen.getByTestId("start-session"));

    expect(onStart).toHaveBeenCalledWith({ seedInputPath: "/tmp/seed.yaml" });
  });

  it("forwards a null seedInputPath when the user starts without a seed", async () => {
    const onStart = vi.fn();
    renderScreen({
      blockers: READY,
      selection: { seedInputPath: null },
      onStart,
    });
    const user = userEvent.setup();

    await user.click(screen.getByTestId("start-session"));

    expect(onStart).toHaveBeenCalledWith({ seedInputPath: null });
  });
});

function installAdapter(
  result: { success: boolean; message: string; installed: boolean } = {
    success: true,
    message: "ok",
    installed: true,
  },
): StartScreenAdapter & { installTimelapse: ReturnType<typeof vi.fn> } {
  return {
    openFile: vi.fn().mockResolvedValue(null),
    openFolder: vi.fn().mockResolvedValue(null),
    installTimelapse: vi.fn().mockResolvedValue(result),
  };
}

describe("StartScreen — timelapse toggle", () => {
  it("shows the install checkbox (not the session toggle) when not installed", () => {
    renderScreen({ blockers: READY, selection: { seedInputPath: null } });
    expect(screen.queryByTestId("timelapse-toggle")).not.toBeInTheDocument();
    expect(screen.getByTestId("timelapse-install")).toBeInTheDocument();
    expect(screen.getByTestId("timelapse-install")).not.toBeChecked();
  });

  it("checking install runs installTimelapse and notifies the parent on success", async () => {
    const adapter = installAdapter();
    const onTimelapseInstalled = vi.fn();
    const onChange = vi.fn();
    render(
      <MemoryRouter>
        <StartScreen
          blockers={READY}
          selection={{ seedInputPath: null }}
          onChange={onChange}
          onStart={vi.fn()}
          onTimelapseInstalled={onTimelapseInstalled}
          adapter={adapter}
        />
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await user.click(screen.getByTestId("timelapse-install"));

    await waitFor(() => expect(adapter.installTimelapse).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(onTimelapseInstalled).toHaveBeenCalledTimes(1));
    expect(onChange).toHaveBeenCalledWith({ seedInputPath: null, timelapse: true });
  });

  it("surfaces an install failure and stays unchecked", async () => {
    const adapter = installAdapter({ success: false, message: "brew missing", installed: false });
    const onTimelapseInstalled = vi.fn();
    render(
      <MemoryRouter>
        <StartScreen
          blockers={READY}
          selection={{ seedInputPath: null }}
          onChange={vi.fn()}
          onStart={vi.fn()}
          onTimelapseInstalled={onTimelapseInstalled}
          adapter={adapter}
        />
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await user.click(screen.getByTestId("timelapse-install"));

    expect(await screen.findByTestId("timelapse-install-error")).toHaveTextContent(/brew missing/);
    expect(screen.getByTestId("timelapse-install")).not.toBeChecked();
    expect(onTimelapseInstalled).not.toHaveBeenCalled();
  });

  it("shows the toggle on by default when installed", () => {
    render(
      <MemoryRouter>
        <StartScreen
          blockers={READY}
          selection={{ seedInputPath: null }}
          onChange={vi.fn()}
          onStart={vi.fn()}
          timelapseAvailable
        />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("timelapse-toggle")).toBeChecked();
  });

  it("passes the effective timelapse flag to onStart", async () => {
    const onStart = vi.fn();
    render(
      <MemoryRouter>
        <StartScreen
          blockers={READY}
          selection={{ seedInputPath: null, timelapse: false }}
          onChange={vi.fn()}
          onStart={onStart}
          timelapseAvailable
        />
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await user.click(screen.getByTestId("start-session"));
    expect(onStart).toHaveBeenCalledWith({ seedInputPath: null, timelapse: false });
  });

  it("toggling off reports timelapse=false via onChange", async () => {
    const onChange = vi.fn();
    render(
      <MemoryRouter>
        <StartScreen
          blockers={READY}
          selection={{ seedInputPath: null }}
          onChange={onChange}
          onStart={vi.fn()}
          timelapseAvailable
        />
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await user.click(screen.getByTestId("timelapse-toggle"));
    expect(onChange).toHaveBeenCalledWith({ seedInputPath: null, timelapse: false });
  });
});
