import { useCallback, useEffect, useState, type JSX } from "react";

import {
  BUDGET_DEFAULT_USD,
  BUDGET_MAX_USD,
  BUDGET_MIN_USD,
  BUDGET_STEP_USD,
  TIME_DEFAULT_MINUTES,
  TIME_MAX_MINUTES,
  TIME_MIN_MINUTES,
  TIME_STEP_MINUTES,
  appliedToSelection,
  clampDollars,
  clampMinutes,
  formatDollars,
  formatHours,
  selectionToLiveInput,
  type BudgetSelection,
} from "../rpc/budget";
import { JsonRpcError } from "../rpc/jsonrpc";
import { getBudget, setBudgetLive } from "../rpc/sessionClient";
import { CapSliderPanel } from "./CapSliderPanel";
import styles from "./AdjustBudgetDialog.module.css";

/**
 * Live in-session budget editor (issue #43). Opened from the desktop
 * File > Adjust Budget… menu. Reads the running session's current caps via
 * ``session.get_budget`` and prefills two {@link CapSliderPanel}s; on submit
 * applies the edited caps via ``session.set_budget``.
 */

export interface AdjustBudgetDialogProps {
  /** Close the dialog (cancel or after a successful apply). */
  onClose: () => void;
  /**
   * The session is winding down (draining / shutting_down). This is an
   * absolute cap OVERRIDE, which silently no-ops past drain, so when locked we
   * surface a banner and disable Apply rather than letting the RPC fail
   * silently (#244).
   */
  locked?: boolean;
}

export function AdjustBudgetDialog({
  onClose,
  locked = false,
}: AdjustBudgetDialogProps): JSX.Element {
  const [selection, setSelection] = useState<BudgetSelection | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

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

  const dollarValue =
    selection && selection.total >= BUDGET_MIN_USD
      ? clampDollars(selection.total)
      : BUDGET_DEFAULT_USD;
  const timeValue =
    selection && selection.timeMinutes >= TIME_MIN_MINUTES
      ? clampMinutes(selection.timeMinutes)
      : TIME_DEFAULT_MINUTES;

  const onDollarChange = useCallback(
    ({ capped, value }: { capped: boolean; value: number }) => {
      setSelection((prev) =>
        prev === null
          ? prev
          : { ...prev, mode: capped ? "capped" : "unlimited", total: value },
      );
    },
    [],
  );

  const onTimeChange = useCallback(
    ({ capped, value }: { capped: boolean; value: number }) => {
      setSelection((prev) =>
        prev === null
          ? prev
          : {
              ...prev,
              timeMode: capped ? "capped" : "unlimited",
              timeMinutes: value,
            },
      );
    },
    [],
  );

  const onSubmit = useCallback(async () => {
    if (selection === null) return;
    setSaving(true);
    setSubmitError(null);
    const toApply: BudgetSelection = {
      mode: selection.mode,
      total: selection.mode === "capped" ? dollarValue : selection.total,
      timeMode: selection.timeMode,
      timeMinutes:
        selection.timeMode === "capped" ? timeValue : selection.timeMinutes,
    };
    try {
      await setBudgetLive(selectionToLiveInput(toApply));
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
  }, [dollarValue, onClose, selection, timeValue]);

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
            Set this running session's caps. These values replace the current
            caps outright (they are not added on top). New plays stop near each
            cap; agents already working finish so their work is not wasted.
          </p>
        </header>

        {locked && (
          <p
            className={styles.error}
            role="alert"
            data-testid="adjust-budget-locked"
          >
            Session is winding down — budget can't be changed.
          </p>
        )}

        {loadError !== null && (
          <p
            className={styles.error}
            role="alert"
            data-testid="adjust-budget-load-error"
          >
            {loadError}
          </p>
        )}

        {selection === null && loadError === null && !locked && (
          <p
            className={styles.loading}
            role="status"
            data-testid="adjust-budget-loading"
          >
            Loading current budget…
          </p>
        )}

        {selection !== null && (
          <div className={styles.body}>
            <CapSliderPanel
              label="Set dollar cap to…"
              radioName="adjust-budget-mode"
              min={BUDGET_MIN_USD}
              max={BUDGET_MAX_USD}
              step={BUDGET_STEP_USD}
              format={formatDollars}
              value={dollarValue}
              capped={selection.mode === "capped"}
              onChange={onDollarChange}
              cappedLabel="Soft cap"
              testId="budget"
            />
            <CapSliderPanel
              label="Set time cap to…"
              radioName="adjust-budget-time-mode"
              min={TIME_MIN_MINUTES}
              max={TIME_MAX_MINUTES}
              step={TIME_STEP_MINUTES}
              format={formatHours}
              value={timeValue}
              capped={selection.timeMode === "capped"}
              onChange={onTimeChange}
              cappedLabel="Time soft cap"
              testId="budget-time"
            />
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
            disabled={saving || selection === null || locked}
            data-testid="adjust-budget-submit"
          >
            {saving ? "Applying…" : "Apply"}
          </button>
        </div>
      </div>
    </div>
  );
}
