import { useCallback, useState } from "react";

import {
  installTimelapse as installTimelapseRpc,
  type TimelapseInstallResult,
} from "../rpc/projectClient";

import styles from "./StartScreen.module.css";

/**
 * AgentShore's first play is always ``seed_project``. The user's only real
 * choice at startup is whether to hand seed_project an explicit seed
 * file/folder (which biases the initial backlog) or let it sweep the
 * project tree on its own. There is no separate "Resume" mode — every
 * session picks up wherever the project state already is.
 */
export interface StartSelection {
  /**
   * Absolute path to an optional seed file or folder. ``null`` means the
   * seed_project play will sweep the project tree without a hint.
   */
  seedInputPath: string | null;
  /**
   * Per-session override for timelapse capture. ``undefined`` means "use the
   * default", which is on whenever the feature is installed.
   */
  timelapse?: boolean;
}

export interface StartGateBlockers {
  targetBranch: boolean;
  agents: boolean;
  identities: boolean;
}

export interface StartScreenAdapter {
  openFile: () => Promise<string | null>;
  openFolder: () => Promise<string | null>;
  /** Auto-install the optional timelapse-capture CLI + deps. */
  installTimelapse?: () => Promise<TimelapseInstallResult>;
}

async function defaultOpenFile(): Promise<string | null> {
  const { open } = await import("@tauri-apps/plugin-dialog");
  const result = await open({
    directory: false,
    multiple: false,
    filters: [{ name: "Seed file", extensions: ["yaml", "yml", "md", "txt", "json"] }],
  });
  return typeof result === "string" ? result : null;
}

const defaultAdapter: StartScreenAdapter = {
  openFile: defaultOpenFile,
  openFolder: async () => {
    const { open } = await import("@tauri-apps/plugin-dialog");
    const result = await open({ directory: true, multiple: false });
    return typeof result === "string" ? result : null;
  },
  installTimelapse: installTimelapseRpc,
};

export interface StartScreenProps {
  /**
   * Which minimum-viable gate items are unmet per DESIGN §10.4
   * (target branch + ≥2 agents + ≥2 identities).
   */
  blockers: StartGateBlockers;
  /** Current selection — usually held in SetupLayout state. */
  selection: StartSelection;
  /** Replace the current selection. */
  onChange: (next: StartSelection) => void;
  /** Caller-supplied click handler — receives final selection. */
  onStart: (selection: StartSelection) => void;
  /** Whether the optional timelapse-capture feature is installed. */
  timelapseAvailable?: boolean;
  /**
   * Called after the timelapse toolchain installs successfully from this
   * screen, so the parent can flip its ``timelapseInstalled`` state and the
   * control becomes the per-session record toggle.
   */
  onTimelapseInstalled?: () => void;
  adapter?: StartScreenAdapter;
}

export function StartScreen({
  blockers,
  selection,
  onChange,
  onStart,
  timelapseAvailable = false,
  onTimelapseInstalled,
  adapter = defaultAdapter,
}: StartScreenProps): JSX.Element {
  const [error, setError] = useState<string | null>(null);
  const [timelapseBusy, setTimelapseBusy] = useState(false);
  const [timelapseError, setTimelapseError] = useState<string | null>(null);
  // Latches true the moment the user clicks Start so the button itself
  // shows immediate feedback ("Starting...") on the click frame, even
  // before the full-screen overlay has a chance to paint. Without this
  // the user sees a dead button for the brief window between click and
  // overlay paint and assumes the click didn't register.
  const [starting, setStarting] = useState(false);

  const gateBlocked = blockers.targetBranch || blockers.agents || blockers.identities;

  const blockerMessages: string[] = [];
  if (blockers.targetBranch) blockerMessages.push("a target branch");
  if (blockers.agents) blockerMessages.push("at least two enabled agents");
  if (blockers.identities) blockerMessages.push("at least two GitHub identities");

  const onPickFile = useCallback(async () => {
    setError(null);
    try {
      const path = await adapter.openFile();
      if (path !== null) {
        onChange({ ...selection, seedInputPath: path });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [adapter, onChange]);

  const onPickFolder = useCallback(async () => {
    setError(null);
    try {
      const path = await adapter.openFolder();
      if (path !== null) {
        onChange({ ...selection, seedInputPath: path });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [adapter, onChange]);

  const onClear = useCallback(() => {
    setError(null);
    onChange({ ...selection, seedInputPath: null });
  }, [onChange, selection]);

  // Checking the box when the toolchain is absent kicks off the install
  // (Homebrew ffmpeg/Node + the timelapse-capture CLI). The box latches
  // checked+disabled for the duration so the click registers immediately —
  // the previous Readiness-screen control only flipped on success minutes
  // later, which read as an unselectable checkbox.
  const onInstallTimelapse = useCallback(async () => {
    if (adapter.installTimelapse === undefined) return;
    setTimelapseError(null);
    setTimelapseBusy(true);
    try {
      const result = await adapter.installTimelapse();
      if (result.success) {
        onTimelapseInstalled?.();
        onChange({ ...selection, timelapse: true });
      } else {
        setTimelapseError(result.message);
      }
    } catch (err) {
      setTimelapseError(err instanceof Error ? err.message : String(err));
    } finally {
      setTimelapseBusy(false);
    }
  }, [adapter, onChange, onTimelapseInstalled, selection]);

  const hasSeed = selection.seedInputPath !== null;
  // Default the toggle on whenever the feature is installed; an explicit
  // per-session choice (selection.timelapse) wins.
  const timelapseOn = selection.timelapse ?? true;

  return (
    <main className={styles.screen} data-testid="start-screen">
      <header className={styles.header}>
        <h1>Start session</h1>
        <p>
          Start is the single completeness gate. AgentShore applies your setup choices, brings up the
          sidecar, and opens the dashboard. The first play (<code>seed_project</code>) runs
          automatically — you can optionally point it at a seed file or folder to bias the initial
          backlog.
        </p>
      </header>

      <section className={styles.section} aria-labelledby="seed-input-title">
        <h2 id="seed-input-title" className={styles.sectionTitle}>
          Seed input (optional)
        </h2>
        <p className={styles.actionDescription}>
          {hasSeed
            ? "seed_project will read this path on the first play to shape the initial epic/story/task graph."
            : "Without a seed, seed_project will sweep the project files on its own to draft the initial backlog."}
        </p>
        <div className={styles.fileRow}>
          <span
            className={`${styles.filePath} ${hasSeed ? "" : styles.filePathEmpty}`}
            data-testid="seed-file-path"
          >
            {selection.seedInputPath ?? "No seed selected — agent will sweep the project"}
          </span>
          <button
            type="button"
            className={styles.button}
            onClick={() => void onPickFile()}
            data-testid="seed-file-pick"
          >
            {hasSeed ? "Change file…" : "Pick file…"}
          </button>
          <button
            type="button"
            className={styles.button}
            onClick={() => void onPickFolder()}
            data-testid="seed-folder-pick"
          >
            {hasSeed ? "Change folder…" : "Pick folder…"}
          </button>
          {hasSeed && (
            <button
              type="button"
              className={styles.button}
              onClick={onClear}
              data-testid="seed-clear"
            >
              Clear
            </button>
          )}
        </div>
      </section>

      <section className={styles.section} aria-labelledby="timelapse-title">
        <h2 id="timelapse-title" className={styles.sectionTitle}>
          Timelapse capture
        </h2>
        {timelapseAvailable ? (
          <label className={styles.actionDescription} data-testid="timelapse-toggle-row">
            <input
              type="checkbox"
              checked={timelapseOn}
              onChange={(e) => onChange({ ...selection, timelapse: e.target.checked })}
              data-testid="timelapse-toggle"
            />{" "}
            Record a timelapse video of the dashboard for this session. The MP4 opens
            automatically when the session ends.
          </label>
        ) : (
          <>
            <label className={styles.actionDescription} data-testid="timelapse-install-row">
              <input
                type="checkbox"
                checked={timelapseBusy}
                disabled={timelapseBusy}
                onChange={(e) => {
                  if (e.target.checked) void onInstallTimelapse();
                }}
                data-testid="timelapse-install"
              />{" "}
              <strong>Timelapse capture</strong> — record a timelapse video of the dashboard
              each session. Installs the timelapse-capture CLI, ffmpeg, and a headless browser
              (a few minutes).
            </label>
            {timelapseBusy && (
              <p className={styles.hint} role="status" data-testid="timelapse-installing">
                Installing timelapse-capture and dependencies… this can take a few minutes.
              </p>
            )}
            {timelapseError !== null && (
              <div
                className={`${styles.hint} ${styles.hintError}`}
                role="alert"
                data-testid="timelapse-install-error"
              >
                {timelapseError}
              </div>
            )}
          </>
        )}
      </section>

      <button
        type="button"
        className={styles.startButton}
        disabled={gateBlocked || starting}
        onClick={() => {
          setStarting(true);
          onStart({
            ...selection,
            timelapse: timelapseAvailable ? timelapseOn : undefined,
          });
        }}
        data-testid="start-session"
      >
        {starting ? "Starting…" : "Start session"}
      </button>

      {gateBlocked && (
        <div className={styles.hint} role="status" data-testid="start-gate-hint">
          Complete required inputs before starting: {blockerMessages.join(", ")}.
        </div>
      )}
      {error !== null && (
        <div className={`${styles.hint} ${styles.hintError}`} role="alert">
          {error}
        </div>
      )}
    </main>
  );
}
