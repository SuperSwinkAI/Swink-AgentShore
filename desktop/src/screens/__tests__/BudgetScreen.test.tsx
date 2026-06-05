import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import {
  BUDGET_DEFAULT_USD,
  BUDGET_DRAIN_RESERVE_USD,
  BUDGET_MAX_USD,
  BUDGET_MIN_USD,
  BudgetScreen,
  type BudgetSelection,
} from "../BudgetScreen";

function renderScreen(
  selection: BudgetSelection,
  onChange: (next: BudgetSelection) => void = () => {},
  onSave?: (next: BudgetSelection) => Promise<void>,
) {
  return render(
    <MemoryRouter initialEntries={["/setup/budget"]}>
      <Routes>
        <Route
          path="/setup/budget"
          element={
            <BudgetScreen selection={selection} onChange={onChange} onSave={onSave} />
          }
        />
        <Route path="/setup/agents" element={<div data-testid="agents-sentinel">agents</div>} />
        <Route path="/setup/start" element={<div data-testid="start-sentinel">start</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("BudgetScreen", () => {
  it("renders the live label as Unlimited when mode is unlimited", () => {
    renderScreen({ mode: "unlimited", total: 0 });
    expect(screen.getByTestId("budget-live-label")).toHaveTextContent("Budget: Unlimited");
    expect(screen.getByTestId("budget-mode-unlimited")).toBeChecked();
    expect(screen.getByTestId("budget-mode-capped")).not.toBeChecked();
  });

  it("renders the live label with the dollar amount when capped", () => {
    renderScreen({ mode: "capped", total: 250 });
    expect(screen.getByTestId("budget-live-label")).toHaveTextContent("Soft cap: $250");
    expect(screen.getByTestId("budget-slider-value")).toHaveTextContent("$250");
  });

  it("explains the soft-cap reserve and overrun behavior", () => {
    renderScreen({ mode: "capped", total: 250 });
    expect(
      screen.getByText(
        new RegExp(
          `soft cap.*within \\$${BUDGET_DRAIN_RESERVE_USD}.*already working can finish.*slightly above the cap`,
          "iu",
        ),
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Soft cap")).toBeInTheDocument();
  });

  it("disables the slider when Unlimited is selected", () => {
    renderScreen({ mode: "unlimited", total: 0 });
    const slider = screen.getByTestId("budget-slider") as HTMLInputElement;
    expect(slider).toBeDisabled();
  });

  it("enables the slider when Capped is selected", () => {
    renderScreen({ mode: "capped", total: 200 });
    const slider = screen.getByTestId("budget-slider") as HTMLInputElement;
    expect(slider).not.toBeDisabled();
  });

  it("exposes the documented min, max, and step on the slider", () => {
    renderScreen({ mode: "capped", total: 200 });
    const slider = screen.getByTestId("budget-slider") as HTMLInputElement;
    expect(slider.min).toBe(String(BUDGET_MIN_USD));
    expect(slider.max).toBe(String(BUDGET_MAX_USD));
    expect(slider.step).toBe("5");
  });

  it("clamps slider value to the minimum when set below $20", () => {
    const onChange = vi.fn();
    renderScreen({ mode: "capped", total: 200 }, onChange);
    const slider = screen.getByTestId("budget-slider") as HTMLInputElement;
    // Simulate a browser-clamped value at the floor.
    fireEvent.change(slider, { target: { value: String(BUDGET_MIN_USD) } });
    expect(onChange).toHaveBeenCalledWith({ mode: "capped", total: BUDGET_MIN_USD });
  });

  it("clamps slider value to the maximum when set above $1000", () => {
    const onChange = vi.fn();
    renderScreen({ mode: "capped", total: 200 }, onChange);
    const slider = screen.getByTestId("budget-slider") as HTMLInputElement;
    fireEvent.change(slider, { target: { value: String(BUDGET_MAX_USD) } });
    expect(onChange).toHaveBeenCalledWith({ mode: "capped", total: BUDGET_MAX_USD });
  });

  it("switching to Capped emits payload with mode='capped' and the last picked total", async () => {
    const onChange = vi.fn();
    renderScreen({ mode: "unlimited", total: 0 }, onChange);
    const user = userEvent.setup();

    await user.click(screen.getByTestId("budget-mode-capped"));

    // When unlimited had total=0, the screen restores the default dollar
    // amount rather than emitting an invalid (below-min) payload.
    expect(onChange).toHaveBeenCalledWith({ mode: "capped", total: BUDGET_DEFAULT_USD });
  });

  it("switching to Unlimited emits payload with mode='unlimited' and preserves total", async () => {
    const onChange = vi.fn();
    renderScreen({ mode: "capped", total: 500 }, onChange);
    const user = userEvent.setup();

    await user.click(screen.getByTestId("budget-mode-unlimited"));

    expect(onChange).toHaveBeenCalledWith({ mode: "unlimited", total: 500 });
  });

  it("emits a capped-mode payload when the slider moves", () => {
    const onChange = vi.fn();
    renderScreen({ mode: "capped", total: 200 }, onChange);
    const slider = screen.getByTestId("budget-slider") as HTMLInputElement;
    fireEvent.change(slider, { target: { value: "455" } });
    // 455 is not a step boundary; the screen snaps to the nearest $5 step.
    expect(onChange).toHaveBeenCalledWith({ mode: "capped", total: 455 });
  });

  it("Back navigates to /setup/agents", async () => {
    renderScreen({ mode: "unlimited", total: 0 });
    const user = userEvent.setup();

    await user.click(screen.getByTestId("budget-back"));
    await waitFor(() => expect(screen.getByTestId("agents-sentinel")).toBeInTheDocument());
  });

  it("Continue navigates to /setup/start", async () => {
    renderScreen({ mode: "capped", total: 200 });
    const user = userEvent.setup();

    await user.click(screen.getByTestId("budget-continue"));
    await waitFor(() => expect(screen.getByTestId("start-sentinel")).toBeInTheDocument());
  });

  it("Continue invokes onSave with the capped selection before navigating", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderScreen({ mode: "capped", total: 250 }, () => {}, onSave);
    const user = userEvent.setup();

    await user.click(screen.getByTestId("budget-continue"));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith({ mode: "capped", total: 250 });
    await waitFor(() => expect(screen.getByTestId("start-sentinel")).toBeInTheDocument());
  });

  it("Continue invokes onSave with the unlimited selection", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderScreen({ mode: "unlimited", total: 0 }, () => {}, onSave);
    const user = userEvent.setup();

    await user.click(screen.getByTestId("budget-continue"));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith({ mode: "unlimited", total: 0 });
    await waitFor(() => expect(screen.getByTestId("start-sentinel")).toBeInTheDocument());
  });

  it("surfaces an inline error and stays on screen when onSave rejects", async () => {
    const onSave = vi.fn().mockRejectedValue(new Error("rpc boom"));
    renderScreen({ mode: "capped", total: 200 }, () => {}, onSave);
    const user = userEvent.setup();

    await user.click(screen.getByTestId("budget-continue"));

    await waitFor(() => expect(onSave).toHaveBeenCalled());
    const errorBanner = await screen.findByTestId("budget-save-error");
    expect(errorBanner).toHaveTextContent(/rpc boom/u);
    // Continue button is re-enabled and we never navigated away.
    expect(screen.queryByTestId("start-sentinel")).not.toBeInTheDocument();
    expect(screen.getByTestId("budget-continue")).not.toBeDisabled();
  });
});
