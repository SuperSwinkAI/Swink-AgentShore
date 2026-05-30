import { useCallback, useEffect, useState } from "react";

import type { SidecarCrashedPayload } from "../services/sidecarEvents";

import styles from "./RecoveryScreen.module.css";

export interface TrackedAgent {
  agent_id: string;
  agent_type: string;
  pid: number;
}

export interface RecoveryAdapter {
  openLog: (path: string) => Promise<void>;
  restartSidecar: () => Promise<void>;
  quitApp: () => Promise<void>;
  trackedAgents: () => Promise<TrackedAgent[]>;
  killAllAgents: () => Promise<TrackedAgent[]>;
}

async function defaultOpenLog(path: string): Promise<void> {
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("open_path_in_default_app", { path });
}

async function defaultRestartSidecar(): Promise<void> {
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("restart_sidecar");
}

async function defaultQuitApp(): Promise<void> {
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("quit_app");
}

async function defaultTrackedAgents(): Promise<TrackedAgent[]> {
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<TrackedAgent[]>("tracked_agent_pids");
}

async function defaultKillAllAgents(): Promise<TrackedAgent[]> {
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<TrackedAgent[]>("kill_all_agents");
}

export const defaultRecoveryAdapter: RecoveryAdapter = {
  openLog: defaultOpenLog,
  restartSidecar: defaultRestartSidecar,
  quitApp: defaultQuitApp,
  trackedAgents: defaultTrackedAgents,
  killAllAgents: defaultKillAllAgents,
};

export interface RecoveryScreenProps {
  payload: SidecarCrashedPayload | null;
  adapter?: RecoveryAdapter;
}

export function RecoveryScreen({
  payload,
  adapter = defaultRecoveryAdapter,
}: RecoveryScreenProps): JSX.Element {
  const [agents, setAgents] = useState<TrackedAgent[]>([]);

  useEffect(() => {
    let cancelled = false;
    adapter
      .trackedAgents()
      .then((list) => {
        if (!cancelled) setAgents(list);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [adapter]);

  const onOpenLog = useCallback(() => {
    if (payload?.log_file_path) {
      void adapter.openLog(payload.log_file_path).catch(() => undefined);
    }
  }, [adapter, payload]);

  const onRestart = useCallback(() => {
    void adapter.restartSidecar().catch(() => undefined);
  }, [adapter]);

  const onQuit = useCallback(() => {
    void adapter.quitApp().catch(() => undefined);
  }, [adapter]);

  const onKillAll = useCallback(() => {
    void adapter
      .killAllAgents()
      .then(() => setAgents([]))
      .catch(() => undefined);
  }, [adapter]);

  const stderrLines = payload?.last_stderr_lines ?? [];
  const exitCode = payload?.exit_code ?? null;
  const logPath = payload?.log_file_path ?? null;

  return (
    <main className={styles.screen}>
      <header className={styles.header}>
        <h1>AgentShore sidecar stopped responding</h1>
        <p className={styles.subtitle}>
          The Python sidecar process exited unexpectedly. Recovery is manual —
          inspect the log, restart the sidecar, or quit the app.
        </p>
      </header>

      <div className={styles.summary} role="status">
        <span data-testid="exit-code">
          exit code: {exitCode !== null ? exitCode : "unknown"}
        </span>
        <span data-testid="log-path">log: {logPath ?? "no log file recorded"}</span>
      </div>

      {stderrLines.length > 0 ? (
        <pre
          aria-label="Last sidecar stderr"
          className={styles.stderr}
          data-testid="stderr-pane"
        >
          {stderrLines.join("\n")}
        </pre>
      ) : (
        <p className={styles.empty} data-testid="stderr-empty">
          No stderr output captured before the crash.
        </p>
      )}

      <section className={styles.agentsSection} data-testid="agents-section">
        <h2 className={styles.agentsHeading}>Tracked agent subprocesses</h2>
        {agents.length === 0 ? (
          <p className={styles.empty} data-testid="agents-empty">
            No tracked agent subprocesses.
          </p>
        ) : (
          <>
            <ul className={styles.agentsList} data-testid="agents-list">
              {agents.map((a) => (
                <li key={a.agent_id} data-testid={`agent-row-${a.agent_id}`}>
                  <span className={styles.agentId}>{a.agent_id}</span>
                  <span className={styles.agentMeta}>
                    {a.agent_type} · pid {a.pid}
                  </span>
                </li>
              ))}
            </ul>
            <button
              type="button"
              className={`${styles.button} ${styles.buttonDanger}`}
              onClick={onKillAll}
              data-testid="kill-all-agents"
            >
              Kill all ({agents.length})
            </button>
          </>
        )}
      </section>

      <div className={styles.actions}>
        <button
          type="button"
          className={styles.button}
          onClick={onOpenLog}
          disabled={!logPath}
          data-testid="open-log"
        >
          Open log file
        </button>
        <button
          type="button"
          className={`${styles.button} ${styles.buttonPrimary}`}
          onClick={onRestart}
          data-testid="restart-sidecar"
        >
          Restart sidecar
        </button>
        <button
          type="button"
          className={`${styles.button} ${styles.buttonDanger}`}
          onClick={onQuit}
          data-testid="quit-app"
        >
          Quit app
        </button>
      </div>
    </main>
  );
}
