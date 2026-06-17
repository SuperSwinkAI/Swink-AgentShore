import {
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type JSX,
} from "react";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

import { SessionContext } from "../services/sessionContext";
import {
  getPreferences,
  setPreferences,
  type PreferencesData,
} from "../rpc/preferencesClient";
import styles from "./AppMenu.module.css";

/**
 * App-global native-menu controller. Mounts once in {@link App} (outside the
 * route table) so the standard-app menu items work on every screen, not just
 * the session dashboard. Wires the Rust `menu:*` events emitted from
 * `build_app_menu` (lib.rs):
 *
 *   - `menu:preferences`        → placeholder Preferences dialog (Cmd+,)
 *   - `menu:keyboard_shortcuts` → keyboard-shortcut cheat-sheet
 *   - `menu:open_logs`          → reveal the active project's log folder
 *   - `menu:copy_diagnostics`   → copyable diagnostics dialog
 *   - `menu:check_updates`      → manual update check
 *
 * It also runs a silent update check once on mount, prompting only when an
 * update exists. Session-scoped menu items (Stop Session / Adjust Budget) stay
 * in SessionDashboardScreen because they act on the running session.
 */

/** A pending application update, normalized away from the plugin's `Update`. */
export interface AvailableUpdate {
  version: string;
  currentVersion: string;
  notes: string | null;
  /** Download + install the update in place. */
  install: () => Promise<void>;
}

/** Diagnostics payload emitted by Rust (`menu:copy_diagnostics`). */
export interface DiagnosticsPayload {
  app: string;
  version: string;
  os: string;
  arch: string;
}

/**
 * Side-effecting hooks, injectable so the controller is testable without the
 * Tauri runtime. Defaults dynamically import the relevant plugin/command so
 * module load never fails outside a WebView (mirrors RecoveryScreen).
 */
export interface AppMenuAdapter {
  checkForUpdate: () => Promise<AvailableUpdate | null>;
  relaunch: () => Promise<void>;
  openLogFolder: (projectPath: string | null) => Promise<void>;
  copyText: (text: string) => Promise<boolean>;
  getPreferences: () => Promise<PreferencesData>;
  setPreferences: (disabledPlays: string[]) => Promise<PreferencesData>;
}

async function defaultCheckForUpdate(): Promise<AvailableUpdate | null> {
  const { check } = await import("@tauri-apps/plugin-updater");
  const update = await check();
  if (!update) return null;
  return {
    version: update.version,
    currentVersion: update.currentVersion,
    notes: update.body ?? null,
    install: () => update.downloadAndInstall(),
  };
}

async function defaultRelaunch(): Promise<void> {
  // restart_sidecar calls app.restart(), which relaunches the (now updated)
  // binary — avoids pulling in @tauri-apps/plugin-process just for relaunch.
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("restart_sidecar");
}

async function defaultOpenLogFolder(projectPath: string | null): Promise<void> {
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("open_log_folder", { projectPath });
}

async function defaultCopyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export const defaultAppMenuAdapter: AppMenuAdapter = {
  checkForUpdate: defaultCheckForUpdate,
  relaunch: defaultRelaunch,
  openLogFolder: defaultOpenLogFolder,
  copyText: defaultCopyText,
  getPreferences,
  setPreferences,
};

const IS_MAC =
  typeof navigator !== "undefined" &&
  /mac/i.test(navigator.platform || navigator.userAgent || "");
const MOD = IS_MAC ? "⌘" : "Ctrl";

const SHORTCUTS: Array<{ label: string; keys: string }> = [
  { label: "Adjust Budget", keys: `${MOD}+B` },
  { label: "Stop Session", keys: `${MOD}+Shift+.` },
  { label: "Preferences", keys: `${MOD}+,` },
  { label: "Close Window", keys: `${MOD}+W` },
  { label: "Open Demo Dashboard", keys: `${MOD}+Shift+D` },
];

function formatDiagnostics(
  diag: DiagnosticsPayload,
  projectPath: string | null,
): string {
  const lines = [`${diag.app} ${diag.version}`, `OS: ${diag.os} (${diag.arch})`];
  if (projectPath) {
    lines.push(`Project: ${projectPath}`);
  }
  return lines.join("\n");
}

type DialogKind =
  | "none"
  | "preferences"
  | "shortcuts"
  | "diagnostics"
  | "update"
  | "no-update"
  | "update-error";

export interface AppMenuProps {
  adapter?: AppMenuAdapter;
}

export function AppMenu({
  adapter = defaultAppMenuAdapter,
}: AppMenuProps): JSX.Element | null {
  const { lastProjectPath } = useContext(SessionContext);
  const [dialog, setDialog] = useState<DialogKind>("none");
  const [diagnostics, setDiagnostics] = useState<DiagnosticsPayload | null>(null);
  const [diagnosticsCopied, setDiagnosticsCopied] = useState(false);
  const [update, setUpdate] = useState<AvailableUpdate | null>(null);
  const [installing, setInstalling] = useState(false);

  // lastProjectPath can change after mount (project selection); read the
  // latest value inside event handlers without re-subscribing every time.
  const projectPathRef = useRef(lastProjectPath);
  projectPathRef.current = lastProjectPath;

  const close = useCallback(() => {
    setDialog("none");
    setDiagnosticsCopied(false);
  }, []);

  const runUpdateCheck = useCallback(
    async (manual: boolean) => {
      // Overlapping checks are harmless (idempotent; the latest result wins
      // via setState), so we don't guard re-entry — a guard would silently
      // drop a manual click that lands while the mount check is still in
      // flight.
      try {
        const found = await adapter.checkForUpdate();
        if (found) {
          setUpdate(found);
          setDialog("update");
        } else if (manual) {
          setDialog("no-update");
        }
      } catch (err) {
        // Silent (launch) checks swallow errors — no network, dev build, or
        // an unsigned bundle shouldn't nag. A manual check surfaces them.
        if (manual) {
          setDialog("update-error");
        } else {
          console.warn("[agentshore-desktop] silent update check failed", err);
        }
      }
    },
    [adapter],
  );

  // Silent check once on mount.
  useEffect(() => {
    void runUpdateCheck(false);
  }, [runUpdateCheck]);

  // Subscribe to every app-global menu event. Each listen() resolves to an
  // unlisten fn we collect and tear down on unmount (cancel-aware so a fast
  // unmount before the promise resolves still unsubscribes).
  useEffect(() => {
    let cancelled = false;
    const unlisteners: UnlistenFn[] = [];
    const track = (p: Promise<UnlistenFn>) => {
      void p
        .then((fn) => {
          if (cancelled) fn();
          else unlisteners.push(fn);
        })
        .catch(() => undefined);
    };

    track(listen("menu:preferences", () => setDialog("preferences")));
    track(listen("menu:keyboard_shortcuts", () => setDialog("shortcuts")));
    track(
      listen<DiagnosticsPayload>("menu:copy_diagnostics", (event) => {
        setDiagnostics(event.payload);
        setDiagnosticsCopied(false);
        setDialog("diagnostics");
      }),
    );
    track(
      listen("menu:open_logs", () => {
        void adapter
          .openLogFolder(projectPathRef.current)
          .catch(() => undefined);
      }),
    );
    track(listen("menu:check_updates", () => void runUpdateCheck(true)));

    return () => {
      cancelled = true;
      for (const fn of unlisteners) fn();
    };
  }, [adapter, runUpdateCheck]);

  const onCopyDiagnostics = useCallback(() => {
    if (!diagnostics) return;
    const text = formatDiagnostics(diagnostics, projectPathRef.current);
    void adapter.copyText(text).then((ok) => setDiagnosticsCopied(ok));
  }, [adapter, diagnostics]);

  const onInstallUpdate = useCallback(() => {
    if (!update) return;
    setInstalling(true);
    void (async () => {
      try {
        await update.install();
        await adapter.relaunch();
      } catch (err) {
        console.error("[agentshore-desktop] update install failed", err);
        setInstalling(false);
        setDialog("update-error");
      }
    })();
  }, [adapter, update]);

  if (dialog === "none") {
    return null;
  }

  if (dialog === "preferences") {
    return <PreferencesDialog adapter={adapter} onClose={close} />;
  }

  if (dialog === "shortcuts") {
    return (
      <Modal
        title="Keyboard Shortcuts"
        testId="keyboard-shortcuts-dialog"
        onClose={close}
      >
        <dl className={styles.shortcutList}>
          {SHORTCUTS.map((s) => (
            <div className={styles.shortcutRow} key={s.label}>
              <dt className={styles.shortcutLabel}>{s.label}</dt>
              <dd className={styles.shortcutKeys}>{s.keys}</dd>
            </div>
          ))}
        </dl>
      </Modal>
    );
  }

  if (dialog === "diagnostics") {
    const text = diagnostics
      ? formatDiagnostics(diagnostics, projectPathRef.current)
      : "";
    return (
      <Modal
        title="Diagnostics"
        description="Copy this when filing a bug report."
        testId="diagnostics-dialog"
        onClose={close}
        primary={{
          label: diagnosticsCopied ? "Copied" : "Copy",
          onClick: onCopyDiagnostics,
        }}
      >
        <pre className={styles.diagnostics} data-testid="diagnostics-text">
          {text}
        </pre>
        {diagnosticsCopied && (
          <p className={styles.copied} role="status">
            Copied to clipboard.
          </p>
        )}
      </Modal>
    );
  }

  if (dialog === "update" && update) {
    return (
      <Modal
        title="Update Available"
        description={`Version ${update.version} is available — you have ${update.currentVersion}.`}
        testId="update-dialog"
        onClose={installing ? undefined : close}
        cancelLabel="Later"
        primary={{
          label: installing ? "Installing…" : "Install & Restart",
          onClick: onInstallUpdate,
          disabled: installing,
        }}
      >
        {update.notes && (
          <pre className={styles.notes} data-testid="update-notes">
            {update.notes}
          </pre>
        )}
      </Modal>
    );
  }

  if (dialog === "no-update") {
    return (
      <Modal
        title="You're Up to Date"
        description="AgentShore is running the latest version."
        testId="no-update-dialog"
        onClose={close}
      />
    );
  }

  if (dialog === "update-error") {
    return (
      <Modal
        title="Update Check Failed"
        description="Couldn't check for updates. Check your connection and try again."
        testId="update-error-dialog"
        onClose={close}
      />
    );
  }

  return null;
}

interface ModalAction {
  label: string;
  onClick: () => void;
  disabled?: boolean;
}

function Modal({
  title,
  description,
  testId,
  children,
  onClose,
  cancelLabel = "Close",
  primary,
}: {
  title: string;
  description?: string;
  testId: string;
  children?: React.ReactNode;
  /** Omit to make the dialog non-dismissable (e.g. mid-install). */
  onClose?: () => void;
  cancelLabel?: string;
  primary?: ModalAction;
}): JSX.Element {
  return (
    <div
      className={styles.overlay}
      role="dialog"
      aria-modal="true"
      aria-label={title}
      data-testid={testId}
    >
      <div className={styles.dialog}>
        <header className={styles.header}>
          <h2>{title}</h2>
          {description && <p>{description}</p>}
        </header>
        {children && <div className={styles.body}>{children}</div>}
        <div className={styles.actions}>
          {onClose && (
            <button
              type="button"
              className={styles.button}
              onClick={onClose}
              data-testid={`${testId}-close`}
            >
              {cancelLabel}
            </button>
          )}
          {primary && (
            <button
              type="button"
              className={`${styles.button} ${styles.buttonPrimary}`}
              onClick={primary.onClick}
              disabled={primary.disabled}
              data-testid={`${testId}-primary`}
            >
              {primary.label}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/** Humanize a PlayType value (`"run_qa"` → `"Run QA"`) for display. */
function playLabel(value: string): string {
  const words = value.split("_");
  return words
    .map((w) => (w.toLowerCase() === "qa" ? "QA" : w.charAt(0).toUpperCase() + w.slice(1)))
    .join(" ");
}

/**
 * Global Preferences dialog (File → Preferences). Lists the non-critical plays
 * the user is allowed to disable, each a checkbox, and persists the set via the
 * `preferences.*` RPCs. A live session picks up the change on its next config
 * reload. Only allowlisted plays are ever shown, so nothing here can stall
 * issue delivery.
 *
 * Owns its own load/edit/save state so the hooks stay unconditional (the parent
 * renders it only while the dialog is open).
 */
function PreferencesDialog({
  adapter,
  onClose,
}: {
  adapter: AppMenuAdapter;
  onClose: () => void;
}): JSX.Element {
  const [data, setData] = useState<PreferencesData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void adapter
      .getPreferences()
      .then((prefs) => {
        if (!cancelled) setData(prefs);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [adapter]);

  const togglePlay = useCallback((play: string) => {
    setData((prev) => {
      if (!prev) return prev;
      const disabled = prev.disabled_plays.includes(play)
        ? prev.disabled_plays.filter((p) => p !== play)
        : [...prev.disabled_plays, play];
      return { ...prev, disabled_plays: disabled };
    });
  }, []);

  const onSave = useCallback(() => {
    if (!data) return;
    setSaving(true);
    setError(null);
    void adapter
      .setPreferences(data.disabled_plays)
      .then(() => onClose())
      .catch((err: unknown) => {
        setError(err instanceof Error ? err.message : String(err));
        setSaving(false);
      });
  }, [adapter, data, onClose]);

  return (
    <Modal
      title="Preferences"
      description="Turn off non-critical plays. Delivery and self-heal plays can't be disabled."
      testId="preferences-dialog"
      onClose={onClose}
      cancelLabel="Cancel"
      primary={{ label: saving ? "Saving…" : "Save", onClick: onSave, disabled: saving || !data }}
    >
      {!data && !error && (
        <p className={styles.placeholder} data-testid="preferences-loading">
          Loading…
        </p>
      )}
      {data && (
        <ul className={styles.playList} data-testid="preferences-play-list">
          {data.disableable_plays.map((play) => (
            <li key={play} className={styles.playRow}>
              <label className={styles.playToggle}>
                <input
                  type="checkbox"
                  checked={data.disabled_plays.includes(play)}
                  onChange={() => togglePlay(play)}
                  data-testid={`preferences-play-${play}`}
                />
                <span>{playLabel(play)}</span>
              </label>
            </li>
          ))}
        </ul>
      )}
      {error && (
        <p className={styles.error} role="alert" data-testid="preferences-error">
          {error}
        </p>
      )}
    </Modal>
  );
}
