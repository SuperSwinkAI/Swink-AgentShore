import { type JSX } from "react";
import styles from "./CapSliderPanel.module.css";

export interface CapSliderPanelProps {
  /** Accessibility label for the panel's ``aria-label`` attribute. */
  label: string;
  /** Radio button name attribute (must be unique per page). */
  radioName: string;
  min: number;
  max: number;
  step: number;
  /** Format the current slider value for display. */
  format: (value: number) => string;
  /** Current slider value. */
  value: number;
  /** When ``true`` the cap radio is selected; when ``false`` unlimited is selected. */
  capped: boolean;
  /** Called when the radio or slider changes. */
  onChange: (next: { capped: boolean; value: number }) => void;
  /** Label for the "capped" radio option. */
  cappedLabel?: string;
  /** Label for the "unlimited" radio option. */
  unlimitedLabel?: string;
  /** data-testid prefix; defaults to the radioName. */
  testId?: string;
}

/**
 * A "capped / unlimited" radio + slider panel shared between the Setup wizard
 * (BudgetScreen) and the live session editor (AdjustBudgetDialog).
 *
 * Keeps exactly one job: render one soft-cap dimension. The caller owns state.
 */
export function CapSliderPanel({
  label,
  radioName,
  min,
  max,
  step,
  format,
  value,
  capped,
  onChange,
  cappedLabel = "Soft cap",
  unlimitedLabel = "Unlimited",
  testId,
}: CapSliderPanelProps): JSX.Element {
  const tid = testId ?? radioName;
  return (
    <section className={styles.panel} aria-label={label}>
      <label className={styles.modeRow}>
        <input
          type="radio"
          name={radioName}
          value="capped"
          checked={capped}
          onChange={() => onChange({ capped: true, value })}
          data-testid={`${tid}-mode-capped`}
        />
        <span>{cappedLabel}</span>
      </label>
      <div
        className={`${styles.cappedBlock} ${capped ? "" : styles["cappedBlock--disabled"]}`}
        aria-hidden={!capped}
      >
        <span className={styles.sliderBounds}>{format(min)}</span>
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          disabled={!capped}
          onChange={(e) =>
            onChange({
              capped: true,
              value: Number.parseInt(e.target.value, 10),
            })
          }
          aria-label={label}
          data-testid={`${tid}-slider`}
          className={styles.slider}
        />
        <span className={`${styles.sliderBounds} ${styles.sliderBoundsRight}`}>
          {format(max)}
        </span>
        <span className={styles.valueRow}>
          <strong data-testid={`${tid}-slider-value`}>{format(value)}</strong>
        </span>
      </div>
      <label className={styles.modeRow}>
        <input
          type="radio"
          name={radioName}
          value="unlimited"
          checked={!capped}
          onChange={() => onChange({ capped: false, value })}
          data-testid={`${tid}-mode-unlimited`}
        />
        <span>{unlimitedLabel}</span>
      </label>
    </section>
  );
}
