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

  it("shows a loading indicator (not an empty dialog) while getBudget is in flight (#281)", async () => {
    // Reproduces the hang report: a slow get_budget prefill left the dialog
    // empty — header + buttons, no sliders, no error. Now a loading status
    // shows until the prefill resolves.
    let resolveBudget: (v: { budget: typeof APPLIED_CAPPED }) => void = () => {};
    getBudgetMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveBudget = resolve;
      }),
    );
    render(<AdjustBudgetDialog onClose={() => {}} />);

    // While pending: loading visible, no sliders, no error banner.
    expect(screen.getByTestId("adjust-budget-loading")).toHaveTextContent(
      "Loading current budget…",
    );
    expect(screen.getByTestId("adjust-budget-loading")).toHaveAttribute(
      "role",
      "status",
    );
    expect(screen.queryByTestId("budget-slider")).not.toBeInTheDocument();
    expect(screen.queryByTestId("adjust-budget-load-error")).not.toBeInTheDocument();

    // Once it resolves: loading gone, sliders present.
    resolveBudget({ budget: APPLIED_CAPPED });
    await waitFor(() =>
      expect(screen.getByTestId("budget-slider")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("adjust-budget-loading")).not.toBeInTheDocument();
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

  it("shows the locked banner and disables Apply when locked (#244)", async () => {
    getBudgetMock.mockResolvedValueOnce({ budget: APPLIED_CAPPED });
    render(<AdjustBudgetDialog onClose={() => {}} locked />);

    const banner = await screen.findByTestId("adjust-budget-locked");
    expect(banner).toHaveTextContent("winding down");
    expect(banner).toHaveAttribute("role", "alert");
    expect(screen.getByTestId("adjust-budget-submit")).toBeDisabled();
  });

  it("makes the OVERRIDE/absolute semantic explicit in the copy", async () => {
    getBudgetMock.mockResolvedValueOnce({ budget: APPLIED_CAPPED });
    render(<AdjustBudgetDialog onClose={() => {}} />);

    await waitFor(() =>
      expect(screen.getByTestId("budget-slider")).toBeInTheDocument(),
    );
    // Header sub-copy frames this as setting (not adding to) the caps.
    expect(screen.getByText(/Set this running session's caps/)).toBeInTheDocument();
    // Each slider panel's aria-label reads "Set … cap to…" — assert via the
    // testId'd inputs (the time panel is unlimited/aria-hidden here, so a
    // role query would miss it).
    expect(screen.getByTestId("budget-slider")).toHaveAttribute(
      "aria-label",
      "Set dollar cap to…",
    );
    expect(screen.getByTestId("budget-time-slider")).toHaveAttribute(
      "aria-label",
      "Set time cap to…",
    );
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
