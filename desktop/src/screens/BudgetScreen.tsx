import { useCallback, useEffect, useRef, useState, type JSX } from "react";
import { useNavigate } from "react-router-dom";

import styles from "./BudgetScreen.module.css";

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
 * Each dimension can be capped or Unlimited on its own (you can cap dollars
 * but leave time unlimited, or vice versa).
 */
export const BUDGET_MIN_USD = 20;
export const BUDGET_MAX_USD = 1000;
export const BUDGET_STEP_USD = 5;
export const BUDGET_DEFAULT_USD = 200;
export const BUDGET_DRAIN_RESERVE_USD = 5;

export const TIME_MIN_MINUTES = 60;
export const TIME_MAX_MINUTES = 4320;
export const TIME_STEP_MINUTES = 60;
export const TIME_DEFAULT_MINUTES = 1440;
export const TIME_DRAIN_RESERVE_MINUTES = 20;

export type BudgetMode = "capped" | "unlimited";

export interface BudgetSelection {
  mode: BudgetMode;
  /** Dollars when ``mode === "capped"``; ignored when unlimited (kept on
   *  state so toggling back to ``capped`` restores the last picked value). */
  total: number;
  /** Time dimension, independent of the dollar dimension. */
  timeMode: BudgetMode;
  /** Minutes when ``timeMode === "capped"``; kept on state when unlimited so
   *  toggling back to ``capped`` restores the last picked value. */
  timeMinutes: number;
}

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

function clampToSlider(value: number): number {
  if (!Number.isFinite(value)) return BUDGET_DEFAULT_USD;
  if (value < BUDGET_MIN_USD) return BUDGET_MIN_USD;
  if (value > BUDGET_MAX_USD) return BUDGET_MAX_USD;
  // Snap to nearest step so the live label always matches a valid slider stop.
  const steps = Math.round((value - BUDGET_MIN_USD) / BUDGET_STEP_USD);
  return BUDGET_MIN_USD + steps * BUDGET_STEP_USD;
}

function clampTimeToSlider(value: number): number {
  if (!Number.isFinite(value)) return TIME_DEFAULT_MINUTES;
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

export function BudgetScreen({
  selection,
  onChange,
  onSave,
}: BudgetScreenProps): JSX.Element {
  const navigate = useNavigate();
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  // When a dimension is unlimited we keep its last picked value around so the
  // slider snaps back to it (rather than the default) when re-selecting Capped.
  // If it is below the minimum (the default-state case from App.tsx, which
  // seeds 0 for unlimited), surface the typical starting point instead.
  const sliderValue =
    selection.total >= BUDGET_MIN_USD ? clampToSlider(selection.total) : BUDGET_DEFAULT_USD;
  const timeSliderValue =
    selection.timeMinutes >= TIME_MIN_MINUTES
      ? clampTimeToSlider(selection.timeMinutes)
      : TIME_DEFAULT_MINUTES;

  const isCapped = selection.mode === "capped";
  const isTimeCapped = selection.timeMode === "capped";

  const setMode = useCallback(
    (mode: BudgetMode) => {
      if (mode === selection.mode) return;
      onChange({ ...selection, mode, total: sliderValue });
    },
    [onChange, selection, sliderValue],
  );

  const onSliderChange = useCallback(
    (event: React.ChangeEvent<HTMLInputElement>) => {
      const next = clampToSlider(Number.parseInt(event.target.value, 10));
      onChange({ ...selection, mode: "capped", total: next });
    },
    [onChange, selection],
  );

  const setTimeMode = useCallback(
    (mode: BudgetMode) => {
      if (mode === selection.timeMode) return;
      onChange({ ...selection, timeMode: mode, timeMinutes: timeSliderValue });
    },
    [onChange, selection, timeSliderValue],
  );

  const onTimeSliderChange = useCallback(
    (event: React.ChangeEvent<HTMLInputElement>) => {
      const next = clampTimeToSlider(Number.parseInt(event.target.value, 10));
      onChange({ ...selection, timeMode: "capped", timeMinutes: next });
    },
    [onChange, selection],
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
    // Match TargetBranchScreen's save-then-navigate flow. When no
    // ``onSave`` adapter is supplied (the existing test renders), skip
    // straight to navigation so behaviour matches the original screen.
    if (onSave === undefined) {
      navigate("/setup/start");
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      // Persist the selection the user actually sees — if a dimension is capped
      // the displayed slider value may differ from ``selection`` when the
      // stored value is below the slider floor (unlimited→capped restore path).
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
        <h1>Budget</h1>
        <p>
          Set soft caps for this session. AgentShore stops assigning new plays within{" "}
          {formatDollars(BUDGET_DRAIN_RESERVE_USD)} of the dollar cap and{" "}
          {TIME_DRAIN_RESERVE_MINUTES} minutes before the time cap, while agents already working can
          finish so their work is not wasted. Final spend and runtime may land slightly above a cap.
        </p>
      </header>

      <section className={styles.panel} aria-label="Dollar budget selection">
        <label className={styles.modeRow}>
          <input
            type="radio"
            name="budget-mode"
            value="capped"
            checked={isCapped}
            onChange={() => setMode("capped")}
            data-testid="budget-mode-capped"
          />
          <span>Soft cap</span>
        </label>

        <div
          className={`${styles.cappedBlock} ${isCapped ? "" : styles["cappedBlock--disabled"]}`}
          aria-hidden={!isCapped}
        >
          <span className={styles.sliderBounds}>{formatDollars(BUDGET_MIN_USD)}</span>
          <input
            type="range"
            min={BUDGET_MIN_USD}
            max={BUDGET_MAX_USD}
            step={BUDGET_STEP_USD}
            value={sliderValue}
            disabled={!isCapped}
            onChange={onSliderChange}
            aria-label="Session soft cap in US dollars"
            data-testid="budget-slider"
            className={styles.slider}
          />
          <span className={`${styles.sliderBounds} ${styles.sliderBoundsRight}`}>
            {formatDollars(BUDGET_MAX_USD)}
          </span>
          <span className={styles.valueRow}>
            <strong data-testid="budget-slider-value">{formatDollars(sliderValue)}</strong>
          </span>
        </div>

        <label className={styles.modeRow}>
          <input
            type="radio"
            name="budget-mode"
            value="unlimited"
            checked={!isCapped}
            onChange={() => setMode("unlimited")}
            data-testid="budget-mode-unlimited"
          />
          <span>Unlimited</span>
        </label>

        <p className={styles.liveLabel} data-testid="budget-live-label">
          <strong>{liveLabel}</strong>
        </p>
      </section>

      <section className={styles.panel} aria-label="Time budget selection">
        <label className={styles.modeRow}>
          <input
            type="radio"
            name="budget-time-mode"
            value="capped"
            checked={isTimeCapped}
            onChange={() => setTimeMode("capped")}
            data-testid="budget-time-mode-capped"
          />
          <span>Time soft cap</span>
        </label>

        <div
          className={`${styles.cappedBlock} ${isTimeCapped ? "" : styles["cappedBlock--disabled"]}`}
          aria-hidden={!isTimeCapped}
        >
          <span className={styles.sliderBounds}>{formatHours(TIME_MIN_MINUTES)}</span>
          <input
            type="range"
            min={TIME_MIN_MINUTES}
            max={TIME_MAX_MINUTES}
            step={TIME_STEP_MINUTES}
            value={timeSliderValue}
            disabled={!isTimeCapped}
            onChange={onTimeSliderChange}
            aria-label="Session time soft cap in hours"
            data-testid="budget-time-slider"
            className={styles.slider}
          />
          <span className={`${styles.sliderBounds} ${styles.sliderBoundsRight}`}>
            {formatHours(TIME_MAX_MINUTES)}
          </span>
          <span className={styles.valueRow}>
            <strong data-testid="budget-time-slider-value">{formatHours(timeSliderValue)}</strong>
          </span>
        </div>

        <label className={styles.modeRow}>
          <input
            type="radio"
            name="budget-time-mode"
            value="unlimited"
            checked={!isTimeCapped}
            onChange={() => setTimeMode("unlimited")}
            data-testid="budget-time-mode-unlimited"
          />
          <span>Unlimited</span>
        </label>

        <p className={styles.liveLabel} data-testid="budget-time-live-label">
          <strong>{timeLiveLabel}</strong>
        </p>
      </section>

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
