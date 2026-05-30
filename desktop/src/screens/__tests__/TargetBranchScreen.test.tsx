import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import {
  TargetBranchScreen,
  type TargetBranchAdapter,
} from "../TargetBranchScreen";
import type { BranchRow } from "../../rpc/projectClient";

function makeAdapter(rows: BranchRow[]): TargetBranchAdapter & {
  list: ReturnType<typeof vi.fn>;
  setTarget: ReturnType<typeof vi.fn>;
} {
  return {
    list: vi.fn().mockResolvedValue(rows),
    setTarget: vi.fn().mockResolvedValue({ target_branch: "main" }),
  };
}

function renderScreen(adapter: TargetBranchAdapter) {
  return render(
    <MemoryRouter>
      <TargetBranchScreen adapter={adapter} />
    </MemoryRouter>,
  );
}

const BRANCHES: BranchRow[] = [
  {
    name: "feature/x",
    is_default: false,
    is_current: false,
    is_remote: false,
    ahead: 2,
    behind: 1,
  },
  {
    name: "main",
    is_default: true,
    is_current: true,
    is_remote: false,
    ahead: 0,
    behind: 0,
  },
];

describe("TargetBranchScreen", () => {
  it("loads branches on mount and preselects the default branch", async () => {
    const adapter = makeAdapter(BRANCHES);
    renderScreen(adapter);

    await waitFor(() => expect(adapter.list).toHaveBeenCalledWith(false));

    // Default branch ("main") should be checked.
    const mainRow = await screen.findByTestId("branch-row-main");
    const radio = within(mainRow).getByRole("radio");
    expect(radio).toBeChecked();
  });

  it("sorts the default branch to the top", async () => {
    const adapter = makeAdapter(BRANCHES);
    renderScreen(adapter);

    await waitFor(() => expect(adapter.list).toHaveBeenCalled());

    // First row should be `main` (the default), not `feature/x`.
    const rows = await screen.findAllByRole("row");
    // rows[0] is the header; rows[1] is the first data row.
    expect(within(rows[1]).getByText("main")).toBeInTheDocument();
  });

  it("refreshes branches with refresh=true when Refresh is clicked", async () => {
    const adapter = makeAdapter(BRANCHES);
    renderScreen(adapter);
    const user = userEvent.setup();

    await waitFor(() => expect(adapter.list).toHaveBeenCalledWith(false));
    await user.click(screen.getByTestId("target-branch-refresh"));

    await waitFor(() => expect(adapter.list).toHaveBeenCalledWith(true));
    expect(await screen.findByRole("status")).toHaveTextContent("Branch list refreshed.");
  });

  it("calls setTarget with the selected branch on Save", async () => {
    const adapter = makeAdapter(BRANCHES);
    renderScreen(adapter);
    const user = userEvent.setup();

    await waitFor(() => expect(adapter.list).toHaveBeenCalled());
    await user.click(screen.getByTestId("target-branch-save"));

    await waitFor(() => expect(adapter.setTarget).toHaveBeenCalledWith("main"));
    expect(await screen.findByRole("status")).toHaveTextContent("Target branch set to main.");
  });

  it("surfaces an error banner when listBranches rejects", async () => {
    const adapter: TargetBranchAdapter = {
      list: vi.fn().mockRejectedValue(new Error("git fetch failed")),
      setTarget: vi.fn(),
    };
    renderScreen(adapter);

    expect(await screen.findByRole("alert")).toHaveTextContent(/Unable to load branches/);
  });
});
