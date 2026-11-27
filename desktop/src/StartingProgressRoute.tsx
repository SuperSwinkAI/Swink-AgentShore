import { useContext, useEffect, useRef, useState, type JSX } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { SessionContext } from "./services/sessionContext";
import { StartingProgress } from "./StartingProgress";
import { setTrustedIssueEnforcement } from "./rpc/projectClient";
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
   * The setup rail's "only work issues from trusted identities" choice
   * (``trusted_ids.restrict_issues_to_trusted_authors``). The setup-rail
   * toggle persists via a best-effort RPC and the checkbox hydrates from
   * localStorage, so agentshore.yaml can silently disagree with what the
   * user selected. When present, this route reconciles the value into the
   * file via ``project.set_trusted_issue_enforcement`` BEFORE firing
   * session.start, because the orchestrator reads the gate off
   * agentshore.yaml at start. Absent on the Repeat / Quick Start paths,
   * which replay the already-configured project and trust the file as-is.
   */
  trustedIssueEnforcement?: boolean;
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
  const expectedProgressTokenRef = useRef<string | number | null>(null);




  useEffect(() => {
    const handle = setTimeout(
      () => setSessionStarting(false),
      SESSION_STARTING_TIMEOUT_MS,
    );
    return () => clearTimeout(handle);
  }, [setSessionStarting]);




  useEffect(() => {
    if (startError !== null) {
      setSessionStarting(false);
    }
  }, [startError, setSessionStarting]);





  useEffect(() => {
    let cancelled = false;
    let unlisten: (() => void) | null = null;

    const state = location.state as StartingLocationState | null;
    const handoffResult =
      state?.sessionStarted === true && state.startResult != null
        ? state.startResult
        : (state?.preflightResult ?? null);
    const progressToken =
      handoffResult === null
        ? `desktop-start-${Date.now()}-${Math.random().toString(36).slice(2)}`
        : null;
    expectedProgressTokenRef.current = progressToken;

    void subscribeProgress((params) => {
      if (cancelled) return;
      const expectedToken = expectedProgressTokenRef.current;
      if (expectedToken !== null && params.token !== expectedToken) return;
      if (!params.step || !params.status) return;
      setSteps((current) =>
        applyProgressEvent(current, params.step!, params.status!, params.error ?? null),
      );
    })
      .then(async (fn) => {
        if (cancelled) {
          fn();
          return;
        }
        unlisten = fn;




        if (handoffResult !== null) {




          setStartResult(handoffResult);
          setSteps((current) =>
            applyProgressEvent(current, FINAL_STEP_ID, "ok", null),
          );
          return;
        }





        const wantGate = state?.trustedIssueEnforcement;
        if (wantGate !== undefined) {
          try {
            await setTrustedIssueEnforcement(wantGate);
          } catch (err) {
            if (cancelled) return;
            if (wantGate) {




              setStartError(
                `Couldn't enable "only work issues from trusted identities": ${
                  err instanceof Error ? err.message : String(err)
                }`,
              );
              return;
            }
          }
        }
        if (cancelled) return;



        const seedInputPath = state?.seedInputPath ?? null;
        void startSession({
          progressToken: progressToken ?? undefined,
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
      expectedProgressTokenRef.current = null;
      unlisten?.();
    };
  }, [location.state]);

  useEffect(() => {
    const finalStep = steps.find((s) => s.id === FINAL_STEP_ID);
    if (finalStep?.status !== "ok" || startResult === null) {




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
