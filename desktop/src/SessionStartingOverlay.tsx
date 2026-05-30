import { useContext } from "react";

import { SessionContext } from "./services/sessionContext";

/**
 * Top-level "Starting your session…" modal. Renders when
 * SessionContext.sessionStarting is true so the same overlay survives
 * the route transition from /starting (bringup checklist) to
 * /session/dashboard (empty office waiting for first agent). Dismissed
 * by SessionDashboardScreen when the first instantiate_agent play_event
 * arrives.
 *
 * The element is positioned fixed above all routes; it doesn't unmount
 * the underlying view, just covers it, so the bringup checklist + the
 * dashboard mount happen as normal behind the overlay.
 */
export function SessionStartingOverlay() {
  const { sessionStarting } = useContext(SessionContext);

  if (!sessionStarting) return null;

  return (
    <div className="fm-session-starting" role="status" aria-live="polite">
      <div className="fm-session-starting__card">
        <div className="fm-session-starting__spinner" aria-hidden="true" />
        <h2 className="fm-session-starting__title">Starting your session</h2>
        <p className="fm-session-starting__subtitle">
          Bringing up the bridge, installing skills, and dispatching the
          first agent. This usually takes 20-30 seconds.
        </p>
      </div>
    </div>
  );
}
