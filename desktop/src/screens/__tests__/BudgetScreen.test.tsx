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
  TIME_DEFAULT_MINUTES,
  TIME_MAX_MINUTES,
  TIME_MIN_MINUTES,
  type BudgetSelection,
} from "../BudgetScreen";

/** Build a full BudgetSelection, defaulting the time dimension to unlimited. */
function sel(partial: Partial<BudgetSelection>): BudgetSelection {
  return {
    mode: "unlimited",
    total: 0,
    timeMode: "unlimited",
    timeMinutes: TIME_DEFAULT_MINUTES,
    ...partial,
  };
}

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
    renderScreen(sel({ mode: "unlimited", total: 0 }));
    expect(screen.getByTestId("budget-live-label")).toHaveTextContent("Budget: Unlimited");
    expect(screen.getByTestId("budget-mode-unlimited")).toBeChecked();
    expect(screen.getByTestId("budget-mode-capped")).not.toBeChecked();
  });

  it("renders the live label with the dollar amount when capped", () => {
    renderScreen(sel({ mode: "capped", total: 250 }));
    expect(screen.getByTestId("budget-live-label")).toHaveTextContent("Soft cap: $250");
    expect(screen.getByTestId("budget-slider-value")).toHaveTextContent("$250");
  });

  it("explains the soft-cap reserve and overrun behavior", () => {
    renderScreen(sel({ mode: "capped", total: 250 }));
    expect(
      screen.getByText(
        new RegExp(
          `soft caps.*within \\$${BUDGET_DRAIN_RESERVE_USD}.*20 minutes before the time cap.*already working can finish`,
          "iu",
        ),
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Soft cap")).toBeInTheDocument();
  });

  it("disables the slider when Unlimited is selected", () => {
    renderScreen(sel({ mode: "unlimited", total: 0 }));
    const slider = screen.getByTestId("budget-slider") as HTMLInputElement;
    expect(slider).toBeDisabled();
  });

  it("enables the slider when Capped is selected", () => {
    renderScreen(sel({ mode: "capped", total: 200 }));
    const slider = screen.getByTestId("budget-slider") as HTMLInputElement;
    expect(slider).not.toBeDisabled();
  });

  it("exposes the documented min, max, and step on the slider", () => {
    renderScreen(sel({ mode: "capped", total: 200 }));
    const slider = screen.getByTestId("budget-slider") as HTMLInputElement;
    expect(slider.min).toBe(String(BUDGET_MIN_USD));
    expect(slider.max).toBe(String(BUDGET_MAX_USD));
    expect(slider.step).toBe("5");
  });

  it("clamps slider value to the minimum when set below $20", () => {
    const onChange = vi.fn();
    renderScreen(sel({ mode: "capped", total: 200 }), onChange);
    const slider = screen.getByTestId("budget-slider") as HTMLInputElement;
    // Simulate a browser-clamped value at the floor.
    fireEvent.change(slider, { target: { value: String(BUDGET_MIN_USD) } });
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ mode: "capped", total: BUDGET_MIN_USD }),
    );
  });

  it("clamps slider value to the maximum when set above $1000", () => {
    const onChange = vi.fn();
    renderScreen(sel({ mode: "capped", total: 200 }), onChange);
    const slider = screen.getByTestId("budget-slider") as HTMLInputElement;
    fireEvent.change(slider, { target: { value: String(BUDGET_MAX_USD) } });
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ mode: "capped", total: BUDGET_MAX_USD }),
    );
  });

  it("switching to Capped emits payload with mode='capped' and the last picked total", async () => {
    const onChange = vi.fn();
    renderScreen(sel({ mode: "unlimited", total: 0 }), onChange);
    const user = userEvent.setup();

    await user.click(screen.getByTestId("budget-mode-capped"));

    // When unlimited had total=0, the screen restores the default dollar
    // amount rather than emitting an invalid (below-min) payload.
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ mode: "capped", total: BUDGET_DEFAULT_USD }),
    );
  });

  it("switching to Unlimited emits payload with mode='unlimited' and preserves total", async () => {
    const onChange = vi.fn();
    renderScreen(sel({ mode: "capped", total: 500 }), onChange);
    const user = userEvent.setup();

    await user.click(screen.getByTestId("budget-mode-unlimited"));

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ mode: "unlimited", total: 500 }),
    );
  });

  it("emits a capped-mode payload when the slider moves", () => {
    const onChange = vi.fn();
    renderScreen(sel({ mode: "capped", total: 200 }), onChange);
    const slider = screen.getByTestId("budget-slider") as HTMLInputElement;
    fireEvent.change(slider, { target: { value: "455" } });
    // 455 is not a step boundary; the screen snaps to the nearest $5 step.
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ mode: "capped", total: 455 }));
  });

  it("Back navigates to /setup/agents", async () => {
    renderScreen(sel({ mode: "unlimited", total: 0 }));
    const user = userEvent.setup();

    await user.click(screen.getByTestId("budget-back"));
    await waitFor(() => expect(screen.getByTestId("agents-sentinel")).toBeInTheDocument());
  });

  it("Continue navigates to /setup/start", async () => {
    renderScreen(sel({ mode: "capped", total: 200 }));
    const user = userEvent.setup();

    await user.click(screen.getByTestId("budget-continue"));
    await waitFor(() => expect(screen.getByTestId("start-sentinel")).toBeInTheDocument());
  });

  it("Continue invokes onSave with the capped selection before navigating", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderScreen(sel({ mode: "capped", total: 250 }), () => {}, onSave);
    const user = userEvent.setup();

    await user.click(screen.getByTestId("budget-continue"));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ mode: "capped", total: 250 }));
    await waitFor(() => expect(screen.getByTestId("start-sentinel")).toBeInTheDocument());
  });

  it("Continue invokes onSave with the unlimited selection", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderScreen(sel({ mode: "unlimited", total: 0 }), () => {}, onSave);
    const user = userEvent.setup();

    await user.click(screen.getByTestId("budget-continue"));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ mode: "unlimited", total: 0 }));
    await waitFor(() => expect(screen.getByTestId("start-sentinel")).toBeInTheDocument());
  });

  it("flushes onSave when leaving via Back (rail-style navigation, not Continue)", async () => {
    // Regression: editing the budget then leaving via the left rail / Back
    // used to skip onSave entirely, so the change reached localStorage but
    // never agentshore.yaml. Leaving by any path must persist the selection.
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderScreen(sel({ mode: "capped", total: 300 }), () => {}, onSave);
    const user = userEvent.setup();

    await user.click(screen.getByTestId("budget-back"));

    // Back navigates to /setup/agents → BudgetScreen unmounts → flush fires.
    await waitFor(() => expect(screen.getByTestId("agents-sentinel")).toBeInTheDocument());
    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ mode: "capped", total: 300 })),
    );
  });

  it("does not double-save on unmount after a successful Continue", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const { unmount } = renderScreen(sel({ mode: "capped", total: 250 }), () => {}, onSave);
    const user = userEvent.setup();

    await user.click(screen.getByTestId("budget-continue"));
    await waitFor(() => expect(screen.getByTestId("start-sentinel")).toBeInTheDocument());
    // Continue already saved once; unmounting must not trigger a second write.
    unmount();
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it("surfaces an inline error and stays on screen when onSave rejects", async () => {
    const onSave = vi.fn().mockRejectedValue(new Error("rpc boom"));
    renderScreen(sel({ mode: "capped", total: 200 }), () => {}, onSave);
    const user = userEvent.setup();

    await user.click(screen.getByTestId("budget-continue"));

    await waitFor(() => expect(onSave).toHaveBeenCalled());
    const errorBanner = await screen.findByTestId("budget-save-error");
    expect(errorBanner).toHaveTextContent(/rpc boom/u);
    // Continue button is re-enabled and we never navigated away.
    expect(screen.queryByTestId("start-sentinel")).not.toBeInTheDocument();
    expect(screen.getByTestId("budget-continue")).not.toBeDisabled();
  });

  // ----- Time dimension (independent soft cap) ----------------------------- //

  it("renders the time live label as Unlimited when timeMode is unlimited", () => {
    renderScreen(sel({ timeMode: "unlimited" }));
    expect(screen.getByTestId("budget-time-live-label")).toHaveTextContent("Time: Unlimited");
    expect(screen.getByTestId("budget-time-mode-unlimited")).toBeChecked();
  });

  it("renders the time live label in hours when capped", () => {
    renderScreen(sel({ timeMode: "capped", timeMinutes: 1440 }));
    expect(screen.getByTestId("budget-time-live-label")).toHaveTextContent("Time cap: 24h");
    expect(screen.getByTestId("budget-time-slider-value")).toHaveTextContent("24h");
  });

  it("exposes the documented 1h–72h bounds on the time slider", () => {
    renderScreen(sel({ timeMode: "capped", timeMinutes: 1440 }));
    const slider = screen.getByTestId("budget-time-slider") as HTMLInputElement;
    expect(slider.min).toBe(String(TIME_MIN_MINUTES));
    expect(slider.max).toBe(String(TIME_MAX_MINUTES));
  });

  it("disables the time slider when Time Unlimited is selected", () => {
    renderScreen(sel({ timeMode: "unlimited" }));
    expect(screen.getByTestId("budget-time-slider")).toBeDisabled();
  });

  it("the two dimensions are independent (cap dollars, leave time unlimited)", () => {
    renderScreen(sel({ mode: "capped", total: 200, timeMode: "unlimited" }));
    expect(screen.getByTestId("budget-mode-capped")).toBeChecked();
    expect(screen.getByTestId("budget-time-mode-unlimited")).toBeChecked();
  });

  it("emits a time payload when the time slider moves", () => {
    const onChange = vi.fn();
    renderScreen(sel({ timeMode: "capped", timeMinutes: 1440 }), onChange);
    const slider = screen.getByTestId("budget-time-slider") as HTMLInputElement;
    fireEvent.change(slider, { target: { value: "120" } });
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ timeMode: "capped", timeMinutes: 120 }),
    );
  });

  it("Continue persists the time selection alongside dollars", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderScreen(
      sel({ mode: "capped", total: 200, timeMode: "capped", timeMinutes: 720 }),
      () => {},
      onSave,
    );
    const user = userEvent.setup();

    await user.click(screen.getByTestId("budget-continue"));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        mode: "capped",
        total: 200,
        timeMode: "capped",
        timeMinutes: 720,
      }),
    );
  });
});
