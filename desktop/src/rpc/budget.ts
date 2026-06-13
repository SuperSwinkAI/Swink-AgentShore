/**
 * Budget types, constants, clamp helpers, and formatters shared between
 * the Setup wizard (BudgetScreen) and the live-session editor
 * (AdjustBudgetDialog). Both surfaces use the same slider geometry.
 */

// ---------------------------------------------------------------------------
// Slider geometry (mirrors src/agentshore/budget.py constants)
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Wire types
// ---------------------------------------------------------------------------

export type BudgetMode = "capped" | "unlimited";

/**
 * UI selection state for the two-dimensional budget surface (Setup wizard +
 * AdjustBudgetDialog). Not sent over the wire directly; converted by
 * {@link budgetSelectionToLiveInput} or ``budgetSelectionToConfig``.
 */
export interface BudgetSelection {
  mode: BudgetMode;
  /** Dollars when ``mode === "capped"``; kept on state when unlimited so
   *  toggling back to ``capped`` restores the last picked value. */
  total: number;
  /** Time dimension, independent of the dollar dimension. */
  timeMode: BudgetMode;
  /** Minutes when ``timeMode === "capped"``; kept on state when unlimited. */
  timeMinutes: number;
}

/**
 * Payload for ``session.set_budget`` / ``session.get_budget`` (live session).
 * Previously named ``BudgetRpcInput`` in ``rpc/sessionClient.ts``.
 */
export interface LiveBudgetInput {
  enabled: boolean;
  total: number;
  time_enabled: boolean;
  time_total_minutes: number;
}

/**
 * The applied budget echoed back by ``session.set_budget`` /
 * ``session.get_budget``. Includes live consumption on top of the caps.
 */
export interface AppliedBudget {
  enabled: boolean;
  total: number;
  spent: number;
  remaining: number;
  time_enabled: boolean;
  time_total_minutes: number;
  time_elapsed_minutes: number;
  time_remaining_minutes: number;
}

/**
 * Payload for ``project.set_budget`` (pre-session config persist).
 * Previously named ``BudgetRpcInput`` in ``rpc/projectClient.ts``.
 */
export interface ProjectBudgetInput {
  enabled: boolean;
  total: number;
  warning_threshold?: number;
  time_enabled?: boolean;
  time_total_minutes?: number;
}

// ---------------------------------------------------------------------------
// Clamp helpers
// ---------------------------------------------------------------------------

export function clampDollars(value: number): number {
  if (!Number.isFinite(value)) return BUDGET_MIN_USD;
  if (value < BUDGET_MIN_USD) return BUDGET_MIN_USD;
  if (value > BUDGET_MAX_USD) return BUDGET_MAX_USD;
  const steps = Math.round((value - BUDGET_MIN_USD) / BUDGET_STEP_USD);
  return BUDGET_MIN_USD + steps * BUDGET_STEP_USD;
}

export function clampMinutes(value: number): number {
  if (!Number.isFinite(value)) return TIME_MIN_MINUTES;
  if (value < TIME_MIN_MINUTES) return TIME_MIN_MINUTES;
  if (value > TIME_MAX_MINUTES) return TIME_MAX_MINUTES;
  const steps = Math.round((value - TIME_MIN_MINUTES) / TIME_STEP_MINUTES);
  return TIME_MIN_MINUTES + steps * TIME_STEP_MINUTES;
}

// ---------------------------------------------------------------------------
// Formatters
// ---------------------------------------------------------------------------

export function formatDollars(amount: number): string {
  return `$${amount.toLocaleString("en-US")}`;
}

export function formatHours(minutes: number): string {
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  if (mins === 0) return `${hours}h`;
  return `${hours}h ${mins}m`;
}

// ---------------------------------------------------------------------------
// Conversion helpers
// ---------------------------------------------------------------------------

/** Convert a {@link BudgetSelection} into a live-session ``session.set_budget`` payload. */
export function selectionToLiveInput(selection: BudgetSelection): LiveBudgetInput {
  return {
    enabled: selection.mode === "capped",
    total:
      selection.mode === "capped" ? clampDollars(selection.total) : selection.total,
    time_enabled: selection.timeMode === "capped",
    time_total_minutes:
      selection.timeMode === "capped"
        ? clampMinutes(selection.timeMinutes)
        : selection.timeMinutes,
  };
}

/** Build a {@link BudgetSelection} from the applied caps the sidecar returns. */
export function appliedToSelection(applied: AppliedBudget): BudgetSelection {
  return {
    mode: applied.enabled ? "capped" : "unlimited",
    total: applied.total,
    timeMode: applied.time_enabled ? "capped" : "unlimited",
    timeMinutes: applied.time_total_minutes,
  };
}
