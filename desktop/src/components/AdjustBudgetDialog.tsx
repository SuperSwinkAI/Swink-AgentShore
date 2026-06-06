import { useCallback, useEffect, useState } from "react";

import {
  BUDGET_MAX_USD,
  BUDGET_MIN_USD,
  BUDGET_STEP_USD,
  TIME_MAX_MINUTES,
  TIME_MIN_MINUTES,
  TIME_STEP_MINUTES,
  type BudgetMode,
  type BudgetSelection,
} from "../screens/BudgetScreen";
import budgetStyles from "../screens/BudgetScreen.module.css";
import { JsonRpcError } from "../rpc/jsonrpc";
import {
  getBudget,
  setBudgetLive,
  type AppliedBudget,
  type BudgetRpcInput,
} from "../rpc/sessionClient";
import { budgetSelectionToConfig } from "../setup/projectYaml";
import styles from "./AdjustBudgetDialog.module.css";

/**
 * Live in-session budget editor (issue #43). Opened from the desktop
 * File > Adjust Budget… menu. On open it reads the running session's current
 * caps via ``session.get_budget`` and prefills the same control set the Setup
 * BudgetScreen uses; on submit it applies the edited caps via
 * ``session.set_budget``. Two independent soft caps (dollars / wall-clock),
 * each cappable or unlimited on its own.
 */

export interface AdjustBudgetDialogProps {
  /** Close the dialog (cancel or after a successful apply). */
  onClose: () => void;
}

/** Build a BudgetSelection from the applied caps the sidecar returns. */
function appliedToSelection(applied: AppliedBudget): BudgetSelection {
  return {
    mode: applied.enabled ? "capped" : "unlimited",
    total: applied.total,
    timeMode: applied.time_enabled ? "capped" : "unlimited",
    timeMinutes: applied.time_total_minutes,
  };
}

function clampDollars(value: number): number {
  if (!Number.isFinite(value)) return BUDGET_MIN_USD;
  if (value < BUDGET_MIN_USD) return BUDGET_MIN_USD;
  if (value > BUDGET_MAX_USD) return BUDGET_MAX_USD;
  const steps = Math.round((value - BUDGET_MIN_USD) / BUDGET_STEP_USD);
  return BUDGET_MIN_USD + steps * BUDGET_STEP_USD;
}

function clampMinutes(value: number): number {
  if (!Number.isFinite(value)) return TIME_MIN_MINUTES;
  if (value < TIME_MIN_MINUTES) return TIME_MIN_MINUTES;
  if (value > TIME_MAX_MINUTES) return TIME_MAX_MINUTES;
  const steps = Math.round((value - TIME_MIN_MINUTES) / TIME_STEP_MINUTES);
  return TIME_MIN_MINUTES + steps * TIME_STEP_MINUTES;
}

function formatDollars(amount: number): string {
  return `$${amount.toLocaleString("en-US")}`;
}

function formatHours(minutes: number): string {
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  if (mins === 0) return `${hours}h`;
  return `${hours}h ${mins}m`;
}

export function AdjustBudgetDialog({
  onClose,
}: AdjustBudgetDialogProps): JSX.Element {
  // ``null`` until the initial getBudget() resolves so we don't render the
  // controls against placeholder caps that could flash a wrong value.
  const [selection, setSelection] = useState<BudgetSelection | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [applied, setApplied] = useState<AppliedBudget | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getBudget()
      .then((result) => {
        if (cancelled) return;
        setSelection(appliedToSelection(result.budget));
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setLoadError(
          `Unable to read the current budget: ${err instanceof Error ? err.message : String(err)}`,
        );
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const dollarSliderValue =
    selection && selection.total >= BUDGET_MIN_USD
      ? clampDollars(selection.total)
      : BUDGET_MIN_USD;
  const timeSliderValue =
    selection && selection.timeMinutes >= TIME_MIN_MINUTES
      ? clampMinutes(selection.timeMinutes)
      : TIME_MIN_MINUTES;

  const isCapped = selection?.mode === "capped";
  const isTimeCapped = selection?.timeMode === "capped";

  const setMode = useCallback((mode: BudgetMode) => {
    setSelection((prev) =>
      prev === null
        ? prev
        : {
            ...prev,
            mode,
            total: prev.total >= BUDGET_MIN_USD ? prev.total : BUDGET_MIN_USD,
          },
    );
  }, []);

  const onDollarSlider = useCallback(
    (event: React.ChangeEvent<HTMLInputElement>) => {
      const next = clampDollars(Number.parseInt(event.target.value, 10));
      setSelection((prev) =>
        prev === null ? prev : { ...prev, mode: "capped", total: next },
      );
    },
    [],
  );

  const setTimeMode = useCallback((mode: BudgetMode) => {
    setSelection((prev) =>
      prev === null
        ? prev
        : {
            ...prev,
            timeMode: mode,
            timeMinutes:
              prev.timeMinutes >= TIME_MIN_MINUTES
                ? prev.timeMinutes
                : TIME_MIN_MINUTES,
          },
    );
  }, []);

  const onTimeSlider = useCallback(
    (event: React.ChangeEvent<HTMLInputElement>) => {
      const next = clampMinutes(Number.parseInt(event.target.value, 10));
      setSelection((prev) =>
        prev === null
          ? prev
          : { ...prev, timeMode: "capped", timeMinutes: next },
      );
    },
    [],
  );

  const onSubmit = useCallback(async () => {
    if (selection === null) return;
    setSaving(true);
    setSubmitError(null);
    const toApply: BudgetSelection = {
      mode: isCapped ? "capped" : "unlimited",
      total: isCapped ? dollarSliderValue : selection.total,
      timeMode: isTimeCapped ? "capped" : "unlimited",
      timeMinutes: isTimeCapped ? timeSliderValue : selection.timeMinutes,
    };
    const payload: BudgetRpcInput = budgetSelectionToConfig(toApply);
    try {
      const result = await setBudgetLive(payload);
      setApplied(result.budget);
      onClose();
    } catch (err) {
      const message =
        err instanceof JsonRpcError || err instanceof Error
          ? err.message
          : String(err);
      setSubmitError(`Unable to update budget: ${message}`);
    } finally {
      setSaving(false);
    }
  }, [
    dollarSliderValue,
    isCapped,
    isTimeCapped,
    onClose,
    selection,
    timeSliderValue,
  ]);

  return (
    <div
      className={styles.overlay}
      role="dialog"
      aria-modal="true"
      aria-label="Adjust Budget"
      data-testid="adjust-budget-dialog"
    >
      <div className={styles.dialog}>
        <header className={styles.header}>
          <h2>Adjust Budget</h2>
          <p>
            Re-cap this running session. New plays stop near each cap; agents
            already working finish so their work is not wasted.
          </p>
        </header>

        {loadError !== null && (
          <p
            className={styles.error}
            role="alert"
            data-testid="adjust-budget-load-error"
          >
            {loadError}
          </p>
        )}

        {selection !== null && (
          <div className={styles.body}>
            <section
              className={budgetStyles.panel}
              aria-label="Dollar budget selection"
            >
              <label className={budgetStyles.modeRow}>
                <input
                  type="radio"
                  name="adjust-budget-mode"
                  value="capped"
                  checked={isCapped}
                  onChange={() => setMode("capped")}
                  data-testid="budget-mode-capped"
                />
                <span>Soft cap</span>
              </label>
              <div
                className={`${budgetStyles.cappedBlock} ${
                  isCapped ? "" : budgetStyles["cappedBlock--disabled"]
                }`}
                aria-hidden={!isCapped}
              >
                <span className={budgetStyles.sliderBounds}>
                  {formatDollars(BUDGET_MIN_USD)}
                </span>
                <input
                  type="range"
                  min={BUDGET_MIN_USD}
                  max={BUDGET_MAX_USD}
                  step={BUDGET_STEP_USD}
                  value={dollarSliderValue}
                  disabled={!isCapped}
                  onChange={onDollarSlider}
                  aria-label="Session soft cap in US dollars"
                  data-testid="budget-slider"
                  className={budgetStyles.slider}
                />
                <span
                  className={`${budgetStyles.sliderBounds} ${budgetStyles.sliderBoundsRight}`}
                >
                  {formatDollars(BUDGET_MAX_USD)}
                </span>
                <span className={budgetStyles.valueRow}>
                  <strong data-testid="budget-slider-value">
                    {formatDollars(dollarSliderValue)}
                  </strong>
                </span>
              </div>
              <label className={budgetStyles.modeRow}>
                <input
                  type="radio"
                  name="adjust-budget-mode"
                  value="unlimited"
                  checked={!isCapped}
                  onChange={() => setMode("unlimited")}
                  data-testid="budget-mode-unlimited"
                />
                <span>Unlimited</span>
              </label>
            </section>

            <section
              className={budgetStyles.panel}
              aria-label="Time budget selection"
            >
              <label className={budgetStyles.modeRow}>
                <input
                  type="radio"
                  name="adjust-budget-time-mode"
                  value="capped"
                  checked={isTimeCapped}
                  onChange={() => setTimeMode("capped")}
                  data-testid="budget-time-mode-capped"
                />
                <span>Time soft cap</span>
              </label>
              <div
                className={`${budgetStyles.cappedBlock} ${
                  isTimeCapped ? "" : budgetStyles["cappedBlock--disabled"]
                }`}
                aria-hidden={!isTimeCapped}
              >
                <span className={budgetStyles.sliderBounds}>
                  {formatHours(TIME_MIN_MINUTES)}
                </span>
                <input
                  type="range"
                  min={TIME_MIN_MINUTES}
                  max={TIME_MAX_MINUTES}
                  step={TIME_STEP_MINUTES}
                  value={timeSliderValue}
                  disabled={!isTimeCapped}
                  onChange={onTimeSlider}
                  aria-label="Session time soft cap in hours"
                  data-testid="budget-time-slider"
                  className={budgetStyles.slider}
                />
                <span
                  className={`${budgetStyles.sliderBounds} ${budgetStyles.sliderBoundsRight}`}
                >
                  {formatHours(TIME_MAX_MINUTES)}
                </span>
                <span className={budgetStyles.valueRow}>
                  <strong data-testid="budget-time-slider-value">
                    {formatHours(timeSliderValue)}
                  </strong>
                </span>
              </div>
              <label className={budgetStyles.modeRow}>
                <input
                  type="radio"
                  name="adjust-budget-time-mode"
                  value="unlimited"
                  checked={!isTimeCapped}
                  onChange={() => setTimeMode("unlimited")}
                  data-testid="budget-time-mode-unlimited"
                />
                <span>Unlimited</span>
              </label>
            </section>
          </div>
        )}

        {submitError !== null && (
          <p
            className={styles.error}
            role="alert"
            data-testid="adjust-budget-error"
          >
            {submitError}
          </p>
        )}

        {/* applied is set transiently before onClose; surfaced for tests that
            assert the applied values came back from the RPC. */}
        {applied !== null && (
          <p
            className={styles.applied}
            data-testid="adjust-budget-applied"
            hidden
          >
            {JSON.stringify(applied)}
          </p>
        )}

        <div className={styles.actions}>
          <button
            type="button"
            className={styles.button}
            onClick={onClose}
            data-testid="adjust-budget-cancel"
          >
            Cancel
          </button>
          <button
            type="button"
            className={`${styles.button} ${styles.buttonPrimary}`}
            onClick={() => {
              void onSubmit();
            }}
            disabled={saving || selection === null}
            data-testid="adjust-budget-submit"
          >
            {saving ? "Applying…" : "Apply"}
          </button>
        </div>
      </div>
    </div>
  );
}
