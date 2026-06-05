import { useCallback, useState } from "react";
import { useNavigate } from "react-router-dom";

import styles from "./BudgetScreen.module.css";

/**
 * Session budget surface for the Setup rail (issue #571). The slider's
 * range matches the backend ``BudgetConfig`` constraint:
 * ``MIN_ENABLED_BUDGET_USD = 20.0`` (``src/agentshore/budget.py``). The
 * upper bound is a UX cap — the dataclass itself accepts any non-negative
 * float, so the $1,000 ceiling here is the safe per-session range we let
 * users pick from the desktop. A user who really wants more can still
 * edit ``agentshore.yaml`` directly.
 */
export const BUDGET_MIN_USD = 20;
export const BUDGET_MAX_USD = 1000;
export const BUDGET_STEP_USD = 5;
export const BUDGET_DEFAULT_USD = 200;
export const BUDGET_DRAIN_RESERVE_USD = 5;

export type BudgetMode = "capped" | "unlimited";

export interface BudgetSelection {
  mode: BudgetMode;
  /** Dollars when ``mode === "capped"``; ignored when unlimited (kept on
   *  state so toggling back to ``capped`` restores the last picked value). */
  total: number;
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

function formatDollars(amount: number): string {
  return `$${amount.toLocaleString("en-US")}`;
}

export function BudgetScreen({
  selection,
  onChange,
  onSave,
}: BudgetScreenProps): JSX.Element {
  const navigate = useNavigate();
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  // When mode === "unlimited" we keep ``selection.total`` around so the
  // slider snaps back to the user's last pick rather than the default
  // when they re-select Capped. If ``total`` is below the minimum (the
  // default-state case from App.tsx, which seeds total=0 for unlimited),
  // surface the typical starting point instead.
  const sliderValue =
    selection.total >= BUDGET_MIN_USD ? clampToSlider(selection.total) : BUDGET_DEFAULT_USD;

  const setMode = useCallback(
    (mode: BudgetMode) => {
      if (mode === selection.mode) return;
      if (mode === "capped") {
        onChange({ mode: "capped", total: sliderValue });
      } else {
        onChange({ mode: "unlimited", total: sliderValue });
      }
    },
    [onChange, selection.mode, sliderValue],
  );

  const onSliderChange = useCallback(
    (event: React.ChangeEvent<HTMLInputElement>) => {
      const next = clampToSlider(Number.parseInt(event.target.value, 10));
      onChange({ mode: "capped", total: next });
    },
    [onChange],
  );

  const isCapped = selection.mode === "capped";
  const liveLabel = isCapped ? `Soft cap: ${formatDollars(sliderValue)}` : "Budget: Unlimited";

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
      // Persist the selection the user actually sees — if mode is capped
      // the displayed ``sliderValue`` may differ from ``selection.total``
      // when ``selection.total`` is below the slider floor (issue #571
      // unlimited→capped default-restore path).
      const toPersist: BudgetSelection = isCapped
        ? { mode: "capped", total: sliderValue }
        : { mode: "unlimited", total: selection.total };
      await onSave(toPersist);
      navigate("/setup/start");
    } catch (err) {
      setSaveError(`Unable to save budget: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSaving(false);
    }
  }, [isCapped, navigate, onSave, selection.total, sliderValue]);

  return (
    <main className={styles.screen} data-testid="budget-screen">
      <header className={styles.header}>
        <h1>Budget</h1>
        <p>
          Set a soft cap for this session. AgentShore stops assigning new plays within{" "}
          {formatDollars(BUDGET_DRAIN_RESERVE_USD)} of the cap, while agents already working can
          finish so their work is not wasted. Final spend may land slightly above the cap.
        </p>
      </header>

      <section className={styles.panel} aria-label="Budget selection">
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
