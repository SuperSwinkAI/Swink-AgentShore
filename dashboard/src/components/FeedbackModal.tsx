import React, { useEffect, useRef, useState } from "react";

// React port of `dashboard/src/hud/feedbackModal.ts`: visibility/reason via
// module-level notify*() (TopBarHud's pattern), the budget-vs-buttons sub-mode
// as local state, and button side-effects surfaced as props for the host
// (bootstrapDashboard or desktop shell) to wire to its transport + state.

const REASON_MESSAGES: Record<string, string> = {
  budget_exhausted: "Budget exhausted. No remaining funds.",
  budget_predictive:
    "Budget exhaustion predicted. Estimated few plays remaining.",
  loop_detected: "Loop detected. The orchestrator may be stuck.",
  stagnation: "Stagnation detected. No alignment progress recently.",
};

// Reasons whose unanswered modal auto-stops the session (#9). Mirrors engine
// backstop (lifecycle._AUTO_STOP_PAUSE_REASONS): an unanswered prompt would
// wedge the loop (observed ~8h AFK). Explicit user/ipc pauses are excluded, so
// a deliberate operator pause is never auto-stopped.
export const AUTO_STOP_REASONS: ReadonlySet<string> = new Set([
  "loop_detected",
  "stagnation",
  "budget_exhausted",
  "budget_predictive",
]);

/** Default unanswered-prompt countdown, matching feedback.unanswered_timeout_seconds. */
export const DEFAULT_AUTO_STOP_SECONDS = 120;

/** True when an unanswered modal for *reason* should auto-stop the session. */
export function shouldAutoStop(reason: string | null): boolean {
  return reason !== null && AUTO_STOP_REASONS.has(reason);
}

interface FeedbackModalState {
  visible: boolean;
  reason: string | null;
}

const listeners = new Set<(s: FeedbackModalState) => void>();
let latestState: FeedbackModalState = { visible: false, reason: null };

export function notifyFeedbackModalShow(reason: string): void {
  latestState = { visible: true, reason };
  listeners.forEach((fn) => fn(latestState));
}

export function notifyFeedbackModalHide(): void {
  latestState = { visible: false, reason: latestState.reason };
  listeners.forEach((fn) => fn(latestState));
}

function useFeedbackModalState(): FeedbackModalState {
  const [state, setState] = useState<FeedbackModalState>(latestState);
  useEffect(() => {
    listeners.add(setState);
    setState(latestState);
    return () => {
      listeners.delete(setState);
    };
  }, []);
  return state;
}

export interface FeedbackModalProps {
  /** Send `{command: "feedback_response", action: "continue"}` and clear pending. */
  onContinue?: () => void;
  /** Send `{command: "feedback_response", action: "pause"}` and clear pending. */
  onPause?: () => void;
  /** Send `{command: "drain"}` and clear pending. */
  onStop?: () => void;
  /** Send `{command: "hard_stop"}` after confirm, and clear pending. */
  onHardStop?: () => void;
  /** Send `{command: "adjust_budget", delta_usd: <delta>}` and clear pending. */
  onAdjustBudget?: (deltaUsd: number) => void;
  /**
   * Seconds before an unanswered automated-escalation prompt auto-stops the
   * session (#9). Defaults to {@link DEFAULT_AUTO_STOP_SECONDS}. Set <= 0 to
   * disable the client-side countdown (the engine backstop still applies).
   */
  autoStopSeconds?: number;
}

export function FeedbackModal({
  onContinue,
  onPause,
  onStop,
  onHardStop,
  onAdjustBudget,
  autoStopSeconds = DEFAULT_AUTO_STOP_SECONDS,
}: FeedbackModalProps): React.ReactElement {
  const { visible, reason } = useFeedbackModalState();
  const [budgetMode, setBudgetMode] = useState(false);
  const [budgetValue, setBudgetValue] = useState("");
  const [stopSending, setStopSending] = useState(false);
  const [secondsLeft, setSecondsLeft] = useState<number | null>(null);
  const budgetInputRef = useRef<HTMLInputElement | null>(null);
  // Keep the latest onStop without resetting the countdown when the host passes
  // a new callback identity each render.
  const onStopRef = useRef(onStop);
  useEffect(() => {
    onStopRef.current = onStop;
  }, [onStop]);

  // Reset inner sub-modes when the modal is dismissed or a new reason shown.
  useEffect(() => {
    setBudgetMode(false);
    setBudgetValue("");
    setStopSending(false);
  }, [visible, reason]);

  useEffect(() => {
    if (budgetMode) budgetInputRef.current?.focus();
  }, [budgetMode]);

  // Unanswered-prompt auto-stop countdown (#9). Runs only for automated
  // escalations and only while the main buttons show — the budget sub-flow
  // counts as a response and suspends it. On expiry it drains like Stop,
  // mirroring the engine backstop; whichever fires first drains cleanly.
  useEffect(() => {
    if (!visible || budgetMode || !shouldAutoStop(reason) || autoStopSeconds <= 0) {
      setSecondsLeft(null);
      return;
    }
    setSecondsLeft(autoStopSeconds);
    let remaining = autoStopSeconds;
    const id = setInterval(() => {
      remaining -= 1;
      if (remaining <= 0) {
        clearInterval(id);
        setSecondsLeft(0);
        notifyFeedbackModalHide();
        onStopRef.current?.();
      } else {
        setSecondsLeft(remaining);
      }
    }, 1000);
    return () => clearInterval(id);
  }, [visible, reason, budgetMode, autoStopSeconds]);

  const reasonText = reason
    ? (REASON_MESSAGES[reason] ?? reason)
    : "--";

  function completeSelection(callback?: () => void): void {
    notifyFeedbackModalHide();
    callback?.();
  }

  function submitBudget(): void {
    const delta = parseFloat(budgetValue);
    if (!isFinite(delta) || delta <= 0) return;
    notifyFeedbackModalHide();
    onAdjustBudget?.(delta);
  }

  function handleHardStop(): void {
    if (
      !window.confirm(
        "Hard stop will kill all in-flight work immediately. Confirm?",
      )
    )
      return;
    completeSelection(onHardStop);
  }

  function handleStop(): void {
    setStopSending(true);
    completeSelection(onStop);
  }

  return (
    <div id="feedback-modal" className={visible ? "visible" : undefined}>
      <div className="modal-box">
        <div className="modal-title">Feedback Required</div>
        <div className="modal-reason" id="feedback-reason">
          {reasonText}
        </div>
        {secondsLeft !== null && (
          <div className="modal-reason" id="feedback-autostop">
            No response — auto-stopping in {secondsLeft}s
          </div>
        )}
        <div
          id="feedback-budget-row"
          style={{ display: budgetMode ? "flex" : "none" }}
        >
          <input
            ref={budgetInputRef}
            type="number"
            id="feedback-budget-amount"
            min={1}
            max={1000}
            step={1}
            placeholder="$ amount"
            value={budgetValue}
            onChange={(e) => setBudgetValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") submitBudget();
              if (e.key === "Escape") setBudgetMode(false);
            }}
          />
          <button
            type="button"
            className="modal-btn primary"
            id="feedback-budget-confirm"
            onClick={submitBudget}
          >
            Add
          </button>
          <button
            type="button"
            className="modal-btn"
            id="feedback-budget-cancel"
            onClick={() => setBudgetMode(false)}
          >
            Cancel
          </button>
        </div>
        <div
          className="modal-actions"
          id="feedback-main-buttons"
          style={{ display: budgetMode ? "none" : "flex" }}
        >
          <button
            type="button"
            className="modal-btn primary"
            id="feedback-continue"
            onClick={() => completeSelection(onContinue)}
          >
            Continue
          </button>
          <button
            type="button"
            className="modal-btn primary"
            id="feedback-add-budget"
            onClick={() => setBudgetMode(true)}
          >
            Add Budget
          </button>
          <button
            type="button"
            className="modal-btn"
            id="feedback-pause"
            onClick={() => completeSelection(onPause)}
          >
            Pause
          </button>
          <button
            type="button"
            className="modal-btn"
            id="feedback-stop"
            onClick={handleStop}
            disabled={stopSending}
          >
            {stopSending ? "Sending…" : "Stop"}
          </button>
          <button
            type="button"
            className="modal-btn danger"
            id="feedback-hard-stop"
            onClick={handleHardStop}
          >
            Hard Stop
          </button>
        </div>
      </div>
    </div>
  );
}

export default FeedbackModal;
