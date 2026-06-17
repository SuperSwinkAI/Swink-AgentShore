import { useCallback, useEffect, useRef, useState, type JSX } from "react";
import { useNavigate } from "react-router-dom";

import {
  BUDGET_DEFAULT_USD,
  BUDGET_DRAIN_RESERVE_USD,
  BUDGET_MAX_USD,
  BUDGET_MIN_USD,
  BUDGET_STEP_USD,
  TIME_DEFAULT_MINUTES,
  TIME_MAX_MINUTES,
  TIME_MIN_MINUTES,
  TIME_STEP_MINUTES,
  TIME_DRAIN_RESERVE_MINUTES,
  clampDollars,
  clampMinutes,
  formatDollars,
  formatHours,
  type BudgetMode,
  type BudgetSelection,
} from "../rpc/budget";
import { CapSliderPanel } from "../components/CapSliderPanel";
import styles from "./BudgetScreen.module.css";

// Re-export constants/types so existing imports from this module keep working.
export {
  BUDGET_DEFAULT_USD,
  BUDGET_DRAIN_RESERVE_USD,
  BUDGET_MAX_USD,
  BUDGET_MIN_USD,
  BUDGET_STEP_USD,
  TIME_DEFAULT_MINUTES,
  TIME_MAX_MINUTES,
  TIME_MIN_MINUTES,
  TIME_STEP_MINUTES,
  TIME_DRAIN_RESERVE_MINUTES,
  type BudgetMode,
  type BudgetSelection,
};

/**
 * Session budget surface for the Setup rail (issue #571). Two independent
 * soft caps:
 *
 * - **Dollars** — slider range matches the backend ``BudgetConfig`` constraint
 *   ``MIN_ENABLED_BUDGET_USD = 20.0`` (``src/agentshore/budget.py``). The
 *   $1,000 ceiling is a UX cap; the dataclass accepts any non-negative float,
 *   so a user who wants more can edit ``agentshore.yaml`` directly.
 * - **Time** — wall-clock soft cap, validated 1h–72h by the backend
 *   (``MIN/MAX_TIME_BUDGET_MINUTES``). AgentShore stops assigning new plays 20
 *   minutes before the cap and lets in-flight agents finish.
 *
 * Each dimension can be capped or Unlimited on its own.
 */

export interface BudgetScreenProps {
  selection: BudgetSelection;
  onChange: (next: BudgetSelection) => void;
  /**
   * Persist the current selection before navigation. App.tsx wires this
   * to ``setBudget`` (project.set_budget RPC) so capped/unlimited choices
   * land in agentshore.yaml — not just localStorage (issue #571 follow-up).
   * Continue still navigates even if the save fails; the error is surfaced
   * inline so the user can retry.
   */
  onSave?: (selection: BudgetSelection) => Promise<void>;
}

export function BudgetScreen({
  selection,
  onChange,
  onSave,
}: BudgetScreenProps): JSX.Element {
  const navigate = useNavigate();
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [estimateInfoOpen, setEstimateInfoOpen] = useState(false);

  // When a dimension is unlimited we keep its last picked value around so the
  // slider snaps back to it (rather than the default) when re-selecting Capped.
  // If it is below the minimum (the default-state case from App.tsx, which
  // seeds 0 for unlimited), surface the typical starting point instead.
  const sliderValue =
    selection.total >= BUDGET_MIN_USD ? clampDollars(selection.total) : BUDGET_DEFAULT_USD;
  const timeSliderValue =
    selection.timeMinutes >= TIME_MIN_MINUTES
      ? clampMinutes(selection.timeMinutes)
      : TIME_DEFAULT_MINUTES;

  const isCapped = selection.mode === "capped";
  const isTimeCapped = selection.timeMode === "capped";

  const onDollarChange = useCallback(
    ({ capped, value }: { capped: boolean; value: number }) => {
      const mode: BudgetMode = capped ? "capped" : "unlimited";
      if (mode === selection.mode && value === sliderValue) return;
      onChange({ ...selection, mode, total: capped ? value : sliderValue });
    },
    [onChange, selection, sliderValue],
  );

  const onTimeChange = useCallback(
    ({ capped, value }: { capped: boolean; value: number }) => {
      const timeMode: BudgetMode = capped ? "capped" : "unlimited";
      if (timeMode === selection.timeMode && value === timeSliderValue) return;
      onChange({
        ...selection,
        timeMode,
        timeMinutes: capped ? value : timeSliderValue,
      });
    },
    [onChange, selection, timeSliderValue],
  );

  const liveLabel = isCapped ? `Soft cap: ${formatDollars(sliderValue)}` : "Budget: Unlimited";
  const timeLiveLabel = isTimeCapped
    ? `Time cap: ${formatHours(timeSliderValue)}`
    : "Time: Unlimited";

  // The canonical selection to persist — identical to onContinue's payload.
  const persistable: BudgetSelection = {
    mode: isCapped ? "capped" : "unlimited",
    total: isCapped ? sliderValue : selection.total,
    timeMode: isTimeCapped ? "capped" : "unlimited",
    timeMinutes: isTimeCapped ? timeSliderValue : selection.timeMinutes,
  };

  // Flush the budget to agentshore.yaml when the user leaves this screen by
  // ANY exit path. The left rail and Back navigate without clicking Continue,
  // and onSave previously fired only from onContinue — so edits made then
  // abandoned via the rail reached localStorage (onChange) but never the YAML.
  // Keep the latest value + onSave in refs so the unmount cleanup writes the
  // final selection without re-subscribing on every keystroke.
  const persistableRef = useRef(persistable);
  persistableRef.current = persistable;
  const onSaveRef = useRef(onSave);
  onSaveRef.current = onSave;
  // onContinue already awaited an explicit save (and surfaced any error), so
  // the unmount flush must not double-write after a successful Continue.
  const savedByContinueRef = useRef(false);

  useEffect(
    () => () => {
      if (savedByContinueRef.current) return;
      const save = onSaveRef.current;
      if (save) void save(persistableRef.current).catch(() => undefined);
    },
    [],
  );

  const onContinue = useCallback(async () => {
    if (onSave === undefined) {
      navigate("/setup/start");
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      const toPersist: BudgetSelection = {
        mode: isCapped ? "capped" : "unlimited",
        total: isCapped ? sliderValue : selection.total,
        timeMode: isTimeCapped ? "capped" : "unlimited",
        timeMinutes: isTimeCapped ? timeSliderValue : selection.timeMinutes,
      };
      await onSave(toPersist);
      savedByContinueRef.current = true;
      navigate("/setup/start");
    } catch (err) {
      setSaveError(`Unable to save budget: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSaving(false);
    }
  }, [isCapped, isTimeCapped, navigate, onSave, selection, sliderValue, timeSliderValue]);

  return (
    <main className={styles.screen} data-testid="budget-screen">
      <header className={styles.header}>
        <div className={styles.titleRow}>
          <h1>Budget</h1>
          <button
            type="button"
            className={styles.infoButton}
            onClick={() => setEstimateInfoOpen((open) => !open)}
            aria-expanded={estimateInfoOpen}
            aria-label="About dollar estimates"
            data-testid="budget-estimate-info"
          >
            i
          </button>
        </div>
        <p>
          Set soft caps for this session. AgentShore stops assigning new plays within{" "}
          {formatDollars(BUDGET_DRAIN_RESERVE_USD)} of the dollar cap and{" "}
          {TIME_DRAIN_RESERVE_MINUTES} minutes before the time cap, while agents already working can
          finish so their work is not wasted. Final spend and runtime may land slightly above a cap.
        </p>
        {estimateInfoOpen && (
          <p className={styles.estimateNote} data-testid="budget-estimate-note">
            Dollar figures are estimates, not a bill. Claude reports its own API-equivalent cost;
            Codex and Gemini are derived from published per-token rates; agents that don&apos;t
            report usage (Grok, Antigravity) are charged the session&apos;s average play cost. Treat
            the running total as a best-guess guardrail, not an invoice.
          </p>
        )}
      </header>

      <CapSliderPanel
        label="Dollar budget selection"
        radioName="budget-mode"
        min={BUDGET_MIN_USD}
        max={BUDGET_MAX_USD}
        step={BUDGET_STEP_USD}
        format={formatDollars}
        value={sliderValue}
        capped={isCapped}
        onChange={onDollarChange}
        cappedLabel="Soft cap"
        testId="budget"
      />
      <p className={styles.liveLabel} data-testid="budget-live-label">
        <strong>{liveLabel}</strong>
      </p>

      <CapSliderPanel
        label="Time budget selection"
        radioName="budget-time-mode"
        min={TIME_MIN_MINUTES}
        max={TIME_MAX_MINUTES}
        step={TIME_STEP_MINUTES}
        format={formatHours}
        value={timeSliderValue}
        capped={isTimeCapped}
        onChange={onTimeChange}
        cappedLabel="Time soft cap"
        testId="budget-time"
      />
      <p className={styles.liveLabel} data-testid="budget-time-live-label">
        <strong>{timeLiveLabel}</strong>
      </p>

      {saveError !== null && (
        <p className={styles.saveError} data-testid="budget-save-error" role="alert">
          {saveError}
        </p>
      )}

      <div className={styles.actions}>
        <button
          type="button"
          className={styles.button}
          onClick={() => navigate("/setup/agents")}
          data-testid="budget-back"
        >
          Back
        </button>
        <button
          type="button"
          className={`${styles.button} ${styles.buttonPrimary}`}
          onClick={() => {
            void onContinue();
          }}
          disabled={saving}
          data-testid="budget-continue"
        >
          {saving ? "Saving…" : "Continue to Start"}
        </button>
      </div>
    </main>
  );
}
