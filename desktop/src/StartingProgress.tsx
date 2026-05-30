import { Link } from "react-router-dom";
import type { StartupStepState, StepStatus } from "./startupSteps";

export interface StartingProgressProps {
  steps: StartupStepState[];
  projectName?: string;
  onRetry: (stepId: string) => void;
  onCancel: () => void;
  /** Surface a hard error from the session.start RPC itself (call failed,
   *  not a per-step progress error). When set, the checklist sits frozen
   *  and this banner shows the failure. */
  errorMessage?: string | null;
}

function stepOrdinal(index: number, status: StepStatus): string {
  if (status === "ok") return "OK";
  if (status === "failed") return "ERR";
  return String(index + 1);
}

function StepRow({
  step,
  index,
  onRetry,
}: {
  step: StartupStepState;
  index: number;
  onRetry: (stepId: string) => void;
}) {
  const isFailed = step.status === "failed";
  const isRunning = step.status === "running";
  const isPending = step.status === "pending";

  return (
    <div
      className={`fm-step fm-step--${step.status}`}
      data-testid={`step-${step.id}`}
      aria-label={`${step.label}: ${step.status}`}
    >
      <div className="fm-step__indicator">
        {isRunning ? (
          <span className="fm-step__spinner" role="progressbar" aria-label="Running" />
        ) : (
          <span aria-hidden="true">{stepOrdinal(index, step.status)}</span>
        )}
      </div>

      <div className="fm-step__body">
        <strong className="fm-step__label">{step.label}</strong>
        <span className="fm-step__desc">{step.description}</span>

        {isFailed && (
          <div className="fm-step__repair" role="alert">
            <span className="fm-step__error" data-testid={`error-${step.id}`}>
              {step.error ?? "An unexpected error occurred."}
            </span>
            <div className="fm-step__actions">
              {step.repairScreen ? (
                <Link
                  to={step.repairScreen}
                  className="fm-btn fm-btn--secondary"
                  data-testid={`repair-link-${step.id}`}
                >
                  Go to setup
                </Link>
              ) : null}
              <button
                type="button"
                className="fm-btn fm-btn--primary"
                data-testid={`retry-${step.id}`}
                onClick={() => onRetry(step.id)}
              >
                Retry
              </button>
            </div>
          </div>
        )}
      </div>

      {isPending && <div className="fm-step__wait" aria-hidden="true" />}
    </div>
  );
}

/**
 * Screen 8 — Starting Progress.
 *
 * Renders the startup checklist driven by session.start $/progress events.
 * Failed steps turn red inline, show the error, and offer contextual repair
 * (Retry or a link to the relevant setup screen) per §10.8.
 */
export function StartingProgress({
  steps,
  projectName,
  onRetry,
  onCancel,
  errorMessage,
}: StartingProgressProps) {
  const completedCount = steps.filter((s) => s.status === "ok").length;
  const totalCount = steps.length;

  return (
    <div className="fm-starting" data-testid="starting-progress">
      <header className="fm-starting__header">
        <div className="fm-starting__title-group">
          <h2>Starting AgentShore</h2>
          {projectName && (
            <p className="fm-starting__project">{projectName}</p>
          )}
          <p className="fm-starting__subtitle">
            Applying setup choices, launching the session, and preparing the dashboard.
          </p>
        </div>
        <div className="fm-starting__header-actions">
          <button
            type="button"
            className="fm-btn fm-btn--danger"
            data-testid="cancel-startup"
            onClick={onCancel}
          >
            Cancel startup
          </button>
        </div>
      </header>

      <div className="fm-starting__content">
        {errorMessage != null && (
          <div className="fm-panel fm-starting__error" role="alert" data-testid="starting-error">
            <strong>session.start failed</strong>
            <span>{errorMessage}</span>
          </div>
        )}
        <section className="fm-panel fm-panel--checklist" aria-label="Startup checklist">
          <h3>
            Startup checklist{" "}
            <span className="fm-starting__progress-count" data-testid="progress-count">
              {completedCount}/{totalCount}
            </span>
          </h3>
          <div className="fm-timeline" role="list">
            {steps.map((step, index) => (
              <div key={step.id} role="listitem">
                <StepRow step={step} index={index} onRetry={onRetry} />
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}
