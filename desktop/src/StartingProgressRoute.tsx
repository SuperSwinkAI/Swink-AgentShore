import { useContext, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { SessionContext } from "./services/sessionContext";
import { StartingProgress } from "./StartingProgress";
import { startSession, type StartSessionResult } from "./services/sessionClient";
import { subscribeProgress } from "./services/sidecarEvents";
import { applyProgressEvent, buildInitialSteps } from "./startupSteps";

/**
 * Belt-and-suspenders dismissal cap for the session-starting overlay.
 * In the normal path the overlay dismisses on the first
 * instantiate_agent play_event (~20-30s). If the orchestrator never
 * gets that far (silent hang, bridge failure post-handshake), this
 * floor stops the modal from hiding the UI forever.
 */
const SESSION_STARTING_TIMEOUT_MS = 120_000;

const FINAL_STEP_ID = "first_snapshot";

interface StartingLocationState {
  seedInputPath?: string | null;
  /** Per-session timelapse-capture override from the Start screen toggle. */
  timelapse?: boolean;
  /**
   * Issue #582 handoff from ``startSessionFromPersistedSetup`` (Repeat
   * via #561, Quick Start via #565). When ``sessionStarted`` is true and
   * ``startResult`` is present, the helper already fired
   * ``session.start`` before navigating — this route MUST skip its own
   * dispatch to avoid a double-start. Setting both fields together is
   * the contract; either alone is treated as the normal Setup → Start
   * path and the route fires ``session.start`` itself.
   */
  sessionStarted?: boolean;
  startResult?: StartSessionResult | null;
  /**
   * Legacy: earlier draft of the same handoff (PR #578 review note). No
   * production caller sets this today; retained so an unconverted
   * caller still short-circuits the second dispatch rather than
   * regressing back to a double-start.
   */
  preflightResult?: StartSessionResult | null;
}

function dashboardUrlFromEndpoint(endpoint: unknown): string | null {
  // session.start returns ipc_endpoint = {kind, host, port, [url]}. The
  // embedded bridge serves both the dashboard SPA (HTTP) and the state
  // stream (WS) on the same loopback port; the iframe wants the HTTP
  // form.
  if (typeof endpoint !== "object" || endpoint === null) return null;
  const raw = endpoint as Record<string, unknown>;
  const host = typeof raw.host === "string" ? raw.host : null;
  const port = typeof raw.port === "number" ? raw.port : null;
  if (host === null || port === null) return null;
  return `http://${host}:${port}/`;
}

export function StartingProgressRoute(): JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const { setDashboardUrl, setSessionStarting } = useContext(SessionContext);
  const [steps, setSteps] = useState(() => buildInitialSteps());
  const [startError, setStartError] = useState<string | null>(null);
  const [startResult, setStartResult] = useState<StartSessionResult | null>(null);

  // Floor: dismiss the overlay after SESSION_STARTING_TIMEOUT_MS even
  // if nothing fires, so a silent orchestrator hang doesn't lock the UI
  // behind the modal indefinitely.
  useEffect(() => {
    const handle = setTimeout(
      () => setSessionStarting(false),
      SESSION_STARTING_TIMEOUT_MS,
    );
    return () => clearTimeout(handle);
  }, [setSessionStarting]);

  // Hard errors from session.start RPC: clear the overlay so the user
  // can see the inline error and the Retry / Cancel buttons rather
  // than staring at a spinner.
  useEffect(() => {
    if (startError !== null) {
      setSessionStarting(false);
    }
  }, [startError, setSessionStarting]);

  // Subscribe to $/progress, then fire session.start. The earlier
  // implementation only subscribed and waited for events — but nothing
  // was actually starting the session, so the checklist sat at 0/6
  // forever (desktop-krdi).
  useEffect(() => {
    let cancelled = false;
    let unlisten: (() => void) | null = null;

    void subscribeProgress((params) => {
      if (cancelled) return;
      if (!params.step || !params.status) return;
      setSteps((current) =>
        applyProgressEvent(current, params.step!, params.status!, params.error ?? null),
      );
    })
      .then((fn) => {
        if (cancelled) {
          fn();
          return;
        }
        unlisten = fn;
        // Issue #582: ``startSessionFromPersistedSetup`` (Repeat / Quick
        // Start) fires session.start before navigating and hands us the
        // resolved result via location state. We must NOT re-fire the
        // RPC in that case — that's the double-start regression.
        const state = location.state as StartingLocationState | null;
        const handoffResult =
          state?.sessionStarted === true && state.startResult != null
            ? state.startResult
            : (state?.preflightResult ?? null);
        if (handoffResult !== null) {
          // The helper already finished session.start before this
          // route mounted, so the $/progress stream is already
          // drained. Latch the final step ok ourselves so the
          // gate effect can transition straight to the dashboard.
          setStartResult(handoffResult);
          setSteps((current) =>
            applyProgressEvent(current, FINAL_STEP_ID, "ok", null),
          );
          return;
        }
        // Listener is registered — now fire session.start. The
        // progress_token correlates the $/progress notifications back
        // to this specific call (DESIGN §2.4).
        const seedInputPath = state?.seedInputPath ?? null;
        void startSession({
          progressToken: `desktop-start-${Date.now()}`,
          seedInputPath,
          ...(state?.timelapse !== undefined ? { timelapse: state.timelapse } : {}),
        })
          .then((result) => {
            if (cancelled) return;
            setStartResult(result);
          })
          .catch((err: unknown) => {
            if (cancelled) return;
            setStartError(err instanceof Error ? err.message : String(err));
          });
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setStartError(err instanceof Error ? err.message : String(err));
        }
      });

    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, [location.state]);

  useEffect(() => {
    const finalStep = steps.find((s) => s.id === FINAL_STEP_ID);
    if (finalStep?.status !== "ok" || startResult === null) {
      // Wait for BOTH the final progress event AND the session.start
      // RPC's response. Without the response we don't have the
      // ipc_endpoint, so navigating now lands on a blank
      // "Dashboard not available" screen.
      return;
    }
    const url = dashboardUrlFromEndpoint(startResult.ipc_endpoint);
    setDashboardUrl(url);
    navigate("/session/dashboard", { replace: true });
  }, [navigate, setDashboardUrl, startResult, steps]);

  return (
    <StartingProgress
      steps={steps}
      onRetry={() => {
        setSessionStarting(false);
        navigate("/setup/start");
      }}
      onCancel={() => {
        setSessionStarting(false);
        navigate("/setup/start");
      }}
      errorMessage={startError}
    />
  );
}
