import { useCallback, useState } from "react";

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
}

export interface StartGateBlockers {
  targetBranch: boolean;
  agents: boolean;
  identities: boolean;
}

export interface StartScreenAdapter {
  openFile: () => Promise<string | null>;
  openFolder: () => Promise<string | null>;
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
  adapter?: StartScreenAdapter;
}

export function StartScreen({
  blockers,
  selection,
  onChange,
  onStart,
  adapter = defaultAdapter,
}: StartScreenProps): JSX.Element {
  const [error, setError] = useState<string | null>(null);
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
        onChange({ seedInputPath: path });
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
        onChange({ seedInputPath: path });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [adapter, onChange]);

  const onClear = useCallback(() => {
    setError(null);
    onChange({ seedInputPath: null });
  }, [onChange]);

  const hasSeed = selection.seedInputPath !== null;

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

      <button
        type="button"
        className={styles.startButton}
        disabled={gateBlocked || starting}
        onClick={() => {
          setStarting(true);
          onStart(selection);
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
