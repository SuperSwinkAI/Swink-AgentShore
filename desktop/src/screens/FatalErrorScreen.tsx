import { useCallback } from "react";

import styles from "./FatalErrorScreen.module.css";

/**
 * Mirrors ``sidecar::SupervisorStartError`` from the Rust side (serde
 * tag = "kind", rename_all = snake_case).
 */
export type FatalShellInfo =
  | { kind: "build_id_mismatch"; expected: string; received: string }
  | { kind: "other"; reason: string };

export interface FatalErrorAdapter {
  /** Optional path to the sidecar log file. ``Open log file`` is
   *  disabled when this is null. */
  logFilePath?: string | null;
  openLog: (path: string) => Promise<void>;
  quitApp: () => Promise<void>;
}

async function defaultOpenLog(path: string): Promise<void> {
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("open_path_in_default_app", { path });
}

async function defaultQuitApp(): Promise<void> {
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("quit_app");
}

export const defaultFatalErrorAdapter: FatalErrorAdapter = {
  logFilePath: null,
  openLog: defaultOpenLog,
  quitApp: defaultQuitApp,
};

export interface FatalErrorScreenProps {
  info: FatalShellInfo | null;
  adapter?: FatalErrorAdapter;
}

function HeadlineFor({ info }: { info: FatalShellInfo }): JSX.Element {
  if (info.kind === "build_id_mismatch") {
    return (
      <>
        <h1>AgentShore build mismatch</h1>
        <p className={styles.subtitle}>
          The desktop shell and the Python sidecar were built from different
          revisions. The shell will not run until both halves match. Reinstall
          the desktop app from the same release tag, or rebuild both halves
          from a clean checkout.
        </p>
      </>
    );
  }
  return (
    <>
      <h1>AgentShore sidecar failed to start</h1>
      <p className={styles.subtitle}>
        The Python sidecar did not finish handshake. Inspect the log for
        details, or quit and try again.
      </p>
    </>
  );
}

function DetailFor({ info }: { info: FatalShellInfo }): JSX.Element {
  if (info.kind === "build_id_mismatch") {
    return (
      <dl className={styles.detail} data-testid="fatal-detail">
        <dt>Expected build_id (shell)</dt>
        <dd data-testid="expected-build-id">{info.expected}</dd>
        <dt>Received build_id (sidecar)</dt>
        <dd data-testid="received-build-id">{info.received}</dd>
      </dl>
    );
  }
  return (
    <dl className={styles.detail} data-testid="fatal-detail">
      <dt>Reason</dt>
      <dd data-testid="fatal-reason">{info.reason}</dd>
    </dl>
  );
}

export function FatalErrorScreen({
  info,
  adapter = defaultFatalErrorAdapter,
}: FatalErrorScreenProps): JSX.Element {
  const logPath = adapter.logFilePath ?? null;

  const onOpenLog = useCallback(() => {
    if (logPath) {
      void adapter.openLog(logPath).catch(() => undefined);
    }
  }, [adapter, logPath]);

  const onQuit = useCallback(() => {
    void adapter.quitApp().catch(() => undefined);
  }, [adapter]);

  if (info === null) {
    // Defensive: someone navigated to /fatal-error without a payload.
    // Render a minimal Quit-only screen so the user is never trapped.
    return (
      <main className={styles.screen}>
        <header className={styles.header}>
          <h1>AgentShore is in an unknown fatal state</h1>
          <p className={styles.subtitle}>No diagnostic information available.</p>
        </header>
        <div className={styles.actions}>
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

  return (
    <main className={styles.screen}>
      <header className={styles.header}>
        <HeadlineFor info={info} />
      </header>

      <DetailFor info={info} />

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
