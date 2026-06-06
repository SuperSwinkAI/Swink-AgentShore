import { useContext, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

import { Dashboard } from "@agentshore/dashboard";

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
  useEffect(() => {
    let unlisten: UnlistenFn | null = null;
    let cancelled = false;
    void listen("menu:adjust_budget", () => {
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
      />
      {budgetDialogOpen && (
        <AdjustBudgetDialog onClose={() => setBudgetDialogOpen(false)} />
      )}
    </>
  );
}
