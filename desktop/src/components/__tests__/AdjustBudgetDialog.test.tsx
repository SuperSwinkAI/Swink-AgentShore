import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const { getBudgetMock, setBudgetLiveMock } = vi.hoisted(() => ({
  getBudgetMock: vi.fn(),
  setBudgetLiveMock: vi.fn(),
}));

vi.mock("../../rpc/sessionClient", () => ({
  getBudget: getBudgetMock,
  setBudgetLive: setBudgetLiveMock,
}));

import { JsonRpcError } from "../../rpc/jsonrpc";
import { AdjustBudgetDialog } from "../AdjustBudgetDialog";

const APPLIED_CAPPED = {
  enabled: true,
  total: 250,
  spent: 10,
  remaining: 240,
  time_enabled: false,
  time_total_minutes: 0,
  time_elapsed_minutes: 5,
  time_remaining_minutes: 0,
};

describe("AdjustBudgetDialog", () => {
  beforeEach(() => {
    getBudgetMock.mockReset();
    setBudgetLiveMock.mockReset();
  });

  it("prefills the controls from getBudget()", async () => {
    getBudgetMock.mockResolvedValueOnce({ budget: APPLIED_CAPPED });
    render(<AdjustBudgetDialog onClose={() => {}} />);

    expect(getBudgetMock).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.getByTestId("budget-mode-capped")).toBeChecked(),
    );
    expect(screen.getByTestId("budget-slider")).toHaveValue("250");
    expect(screen.getByTestId("budget-time-mode-unlimited")).toBeChecked();
  });

  it("submits the edited selection via setBudgetLive and closes", async () => {
    getBudgetMock.mockResolvedValueOnce({ budget: APPLIED_CAPPED });
    setBudgetLiveMock.mockResolvedValueOnce({
      budget: { ...APPLIED_CAPPED, total: 500 },
    });
    const onClose = vi.fn();
    render(<AdjustBudgetDialog onClose={onClose} />);

    await waitFor(() =>
      expect(screen.getByTestId("budget-slider")).toHaveValue("250"),
    );
    fireEvent.change(screen.getByTestId("budget-slider"), {
      target: { value: "500" },
    });
    fireEvent.click(screen.getByTestId("adjust-budget-submit"));

    await waitFor(() =>
      expect(setBudgetLiveMock).toHaveBeenCalledWith({
        enabled: true,
        total: 500,
        time_enabled: false,
        time_total_minutes: 0,
      }),
    );
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it("shows an error and stays open when setBudgetLive fails", async () => {
    getBudgetMock.mockResolvedValueOnce({ budget: APPLIED_CAPPED });
    setBudgetLiveMock.mockRejectedValueOnce(
      new JsonRpcError({ code: -32000, message: "no active session" }),
    );
    const onClose = vi.fn();
    render(<AdjustBudgetDialog onClose={onClose} />);

    await waitFor(() =>
      expect(screen.getByTestId("budget-slider")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("adjust-budget-submit"));

    expect(await screen.findByTestId("adjust-budget-error")).toHaveTextContent(
      "no active session",
    );
    expect(onClose).not.toHaveBeenCalled();
  });

  it("closes without calling setBudgetLive on cancel", async () => {
    getBudgetMock.mockResolvedValueOnce({ budget: APPLIED_CAPPED });
    const onClose = vi.fn();
    render(<AdjustBudgetDialog onClose={onClose} />);

    await waitFor(() =>
      expect(screen.getByTestId("budget-slider")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("adjust-budget-cancel"));

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(setBudgetLiveMock).not.toHaveBeenCalled();
  });
});
