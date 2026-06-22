import { useContext, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

import { Dashboard, type SessionState } from "@agentshore/dashboard";

import { AdjustBudgetDialog } from "../components/AdjustBudgetDialog";
import { SessionContext } from "../services/sessionContext";
import { stopSession } from "../services/sessionClient";
import styles from "./SessionDashboardScreen.module.css";

/**
 * Mount the React-native dashboard inside the desktop shell. No iframe —
 * we render the same React tree the bridge SPA renders, and connect
 * directly to the embedded-bridge WebSocket. dashboardUrl is the bridge's
 * HTTP root (http://host:port/); convert to the matching ws://host:port/ws.
 */
function wsUrlFromHttp(url: string): string {
  return url.replace(/^http(s?):\/\//, "ws$1://").replace(/\/$/, "") + "/ws";
}

export function SessionDashboardScreen() {
  const { dashboardUrl, setEsr, setSessionStarting } =
    useContext(SessionContext);
  const navigate = useNavigate();
  const wsUrl = useMemo(
    () => (dashboardUrl ? wsUrlFromHttp(dashboardUrl) : null),
    [dashboardUrl],
  );

  // Wire the File > Stop Session menu (lib.rs build_app_menu) to a
  // graceful drain. Ignore re-fires while a stop is already in flight
  // so double-clicking the menu doesn't queue duplicate session.stop
  // RPCs.
  const stoppingRef = useRef(false);
  // File > Adjust Budget… (lib.rs build_app_menu) emits "menu:adjust_budget".
  // We open a modal that reads/writes the running session's budget over the
  // live session.get_budget / session.set_budget RPCs (issue #43).
  const [budgetDialogOpen, setBudgetDialogOpen] = useState(false);
  // Latest session lifecycle phase, fed by the Dashboard on every state_update.
  // Once draining / shutting_down the absolute "Adjust Budget" override silently
  // no-ops (the loop only dispatches end_agent past drain), so we lock the
  // control rather than letting it fail silently (#244).
  const [sessionState, setSessionState] = useState<SessionState | undefined>();
  const budgetLocked =
    sessionState === "draining" || sessionState === "shutting_down";
  // Read the latest locked value inside the menu listener (registered once) so a
  // stale closure doesn't open the dialog after drain begins — mirrors the
  // stoppingRef pattern below.
  const budgetLockedRef = useRef(false);
  budgetLockedRef.current = budgetLocked;
  useEffect(() => {
    let unlisten: UnlistenFn | null = null;
    let cancelled = false;
    void listen("menu:adjust_budget", () => {
      if (budgetLockedRef.current) {
        console.info(
          "[agentshore-desktop] Adjust Budget ignored — session is winding down",
        );
        return;
      }
      setBudgetDialogOpen(true);
    })
      .then((fn) => {
        if (cancelled) fn();
        else unlisten = fn;
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);
  useEffect(() => {
    let unlisten: UnlistenFn | null = null;
    let cancelled = false;
    void listen("menu:stop_session", async () => {
      if (stoppingRef.current) return;
      stoppingRef.current = true;
      try {
        const esr = await stopSession({ mode: "drain" });
        setEsr(esr);
        navigate("/session/esr");
      } catch (err) {
        stoppingRef.current = false;
        console.error("[agentshore-desktop] session.stop failed", err);
      }
    })
      .then((fn) => {
        if (cancelled) fn();
        else unlisten = fn;
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, [navigate, setEsr]);

  if (!wsUrl) {
    return (
      <main className={styles.fallback}>
        <h1>Dashboard not available</h1>
        <p>The in-session dashboard URL has not been provided yet.</p>
        <Link to="/">Return to start</Link>
      </main>
    );
  }
  // Mount the same dashboard tree the CLI bridge uses so desktop and CLI
  // dashboard chrome stay visually aligned.
  return (
    <>
      <Dashboard
        wsUrl={wsUrl}
        // Dismiss the "Starting your session..." overlay on whichever
        // signal arrives first: the first instantiate_agent dispatch
        // (fast-path when there's work) or the first state_update (the
        // engine is confirmed live even for a no-work session that never
        // spawns an agent — issue #10).
        onFirstAgentInstantiated={() => setSessionStarting(false)}
        onFirstStateUpdate={() => setSessionStarting(false)}
        onSessionStateChange={setSessionState}
      />
      {budgetDialogOpen && (
        <AdjustBudgetDialog
          onClose={() => setBudgetDialogOpen(false)}
          locked={budgetLocked}
        />
      )}
    </>
  );
}
