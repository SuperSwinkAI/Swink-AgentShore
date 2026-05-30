import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  listRecents,
  removeRecent,
  touchRecent,
  type RecentEntry,
} from "../rpc/recentsClient";
import {
  selectProject,
  setBudget,
  type BudgetRpcInput,
} from "../rpc/projectClient";
import { budgetSelectionToConfig } from "../setup/projectYaml";
import {
  persistedSetupExists,
  readPersistedBudget,
  startSessionFromPersistedSetup,
  type StartFromPersistedSetupStep,
} from "../setup/startFromPersistedSetup";

import styles from "./ChooseProjectScreen.module.css";

/**
 * Setup-rail step ids — kept in sync with App.tsx's ``SetupScreen`` union.
 * The Quick Start fallback banner (#565) needs a UI-facing step label, so
 * we map the helper's RPC-flavoured enum onto these names before handing
 * off to ``onQuickStartFailed``. A dedicated ``"budget"`` entry covers the
 * extra ``project.set_budget`` (#580) round-trip that Quick Start fires
 * BEFORE invoking the shared helper.
 */
export type QuickStartSetupStep =
  | "readiness"
  | "target-branch"
  | "identities"
  | "agents"
  | "budget"
  | "start";

function helperStepToUiStep(
  step: StartFromPersistedSetupStep,
): QuickStartSetupStep {
  // ``project.select`` / ``project.inspect`` failing means the chooser
  // couldn't even open the project — readiness is where the regular
  // Setup-rail surfaces those problems. ``start`` / ``navigate`` failing
  // means we got far enough that the start blockers panel is the right
  // place to land.
  switch (step) {
    case "select":
    case "inspect":
      return "readiness";
    case "start":
    case "navigate":
    default:
      return "start";
  }
}

export interface ChooseProjectAdapter {
  list: () => Promise<RecentEntry[]>;
  touch: (path: string) => Promise<void>;
  remove: (path: string) => Promise<void>;
  select: (path: string) => Promise<unknown>;
  openDirectory: () => Promise<string | null>;
  /**
   * Override the Quick Start dispatch path (issue #565). Defaults to
   * the shared ``startSessionFromPersistedSetup`` helper. Tests inject
   * a stub so the assertions stay deterministic.
   */
  quickStart?: typeof startSessionFromPersistedSetup;
  /**
   * Override the localStorage check that decides whether a row is
   * Quick-Start-eligible. Defaults to ``persistedSetupExists``.
   */
  hasPersistedSetup?: (path: string) => boolean;
  /**
   * Override ``project.set_budget`` (#580). Quick Start calls this
   * BEFORE the shared helper because ``session.start`` only forwards
   * ``progress_token`` and ``seed_input_path`` on the wire — a budget
   * change has to be persisted to ``agentshore.yaml`` first or it would
   * be silently dropped. Tests stub this to assert the call ordering.
   */
  setBudgetImpl?: (budget: BudgetRpcInput) => Promise<unknown>;
  /**
   * Override the localStorage budget read. Defaults to
   * ``readPersistedBudget``. Returns ``null`` to simulate older
   * persisted state without a budget slice — Quick Start must still
   * fire in that case (skipping the set_budget round-trip).
   */
  readPersistedBudgetImpl?: () => { mode: "capped" | "unlimited"; total: number } | null;
}

export interface ChooseProjectScreenProps {
  adapter?: ChooseProjectAdapter;
  /**
   * Called after a successful select() with the project path so the
   * shell can pre-populate setup state from agentshore.yaml before the
   * setup rail mounts. Failures here must not block navigation.
   */
  onProjectSelected?: (path: string) => void | Promise<void>;
  /**
   * Called when a Quick Start dispatch fails so the shell can fall
   * back into the regular Setup-rail flow with a banner pointing at
   * the failing step. The second argument is the helper's best guess
   * at which Setup screen the user should land on.
   */
  onQuickStartFailed?: (
    path: string,
    error: Error,
    failedStep: QuickStartSetupStep,
  ) => void;
}

async function defaultOpenDirectory(): Promise<string | null> {
  const { open } = await import("@tauri-apps/plugin-dialog");
  const result = await open({ directory: true, multiple: false });
  return typeof result === "string" ? result : null;
}

const defaultAdapter: ChooseProjectAdapter = {
  list: listRecents,
  touch: touchRecent,
  remove: removeRecent,
  select: selectProject,
  openDirectory: defaultOpenDirectory,
  quickStart: startSessionFromPersistedSetup,
  hasPersistedSetup: () => persistedSetupExists(),
  setBudgetImpl: setBudget,
  readPersistedBudgetImpl: readPersistedBudget,
};

function formatRelative(iso: string, now: Date = new Date()): string {
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) {
    return iso;
  }
  const diffMs = now.getTime() - then.getTime();
  const diffMin = Math.round(diffMs / 60_000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHour = Math.round(diffMin / 60);
  if (diffHour < 24) return `${diffHour}h ago`;
  const diffDay = Math.round(diffHour / 24);
  if (diffDay < 30) return `${diffDay}d ago`;
  return then.toLocaleDateString();
}

export function ChooseProjectScreen({
  adapter = defaultAdapter,
  onProjectSelected,
  onQuickStartFailed,
}: ChooseProjectScreenProps): JSX.Element {
  const navigate = useNavigate();
  const [entries, setEntries] = useState<RecentEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const quickStart = adapter.quickStart ?? startSessionFromPersistedSetup;
  const hasPersistedSetup =
    adapter.hasPersistedSetup ?? ((_path: string) => persistedSetupExists());
  const setBudgetCall = adapter.setBudgetImpl ?? setBudget;
  const readBudget = adapter.readPersistedBudgetImpl ?? readPersistedBudget;

  useEffect(() => {
    let cancelled = false;
    adapter
      .list()
      .then((result) => {
        if (!cancelled) {
          setEntries(result);
          setError(null);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setEntries([]);
          setError(err instanceof Error ? err.message : String(err));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [adapter]);

  const onOpenProject = useCallback(
    async (path: string) => {
      try {
        await adapter.touch(path);
        await adapter.select(path);
        if (onProjectSelected) {
          // Hydration runs best-effort — never block the user from
          // reaching the setup rail just because agentshore.yaml parsed
          // poorly or project.inspect failed.
          try {
            await onProjectSelected(path);
          } catch {
            // intentionally swallowed
          }
        }
        navigate("/setup/readiness");
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    },
    [adapter, navigate, onProjectSelected],
  );

  const onRemove = useCallback(
    async (path: string) => {
      try {
        await adapter.remove(path);
        setEntries((current) =>
          current === null ? current : current.filter((entry) => entry.path !== path),
        );
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    },
    [adapter],
  );

  const onOpenRepository = useCallback(async () => {
    try {
      const chosen = await adapter.openDirectory();
      if (chosen) {
        await onOpenProject(chosen);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [adapter, onOpenProject]);

  const onQuickStart = useCallback(
    async (path: string) => {
      setError(null);
      const reportFailure = (
        err: unknown,
        failedStep: QuickStartSetupStep,
      ) => {
        const error = err instanceof Error ? err : new Error(String(err));
        if (onQuickStartFailed) {
          onQuickStartFailed(path, error, failedStep);
        } else {
          setError(`Quick Start failed: ${error.message}`);
        }
      };

      try {
        await adapter.touch(path);

        // Select the project first so the sidecar's active_project_path
        // is set before any RPC that requires it (set_budget, inspect).
        // startSessionFromPersistedSetup also calls selectProject, but
        // that's idempotent — doing it here unblocks the budget write.
        try {
          await adapter.select(path);
        } catch (err) {
          reportFailure(err, "readiness");
          return;
        }

        // Persist the localStorage budget to agentshore.yaml via
        // project.set_budget (#580). Required because
        // ``sessionClient.startSession`` only serializes
        // ``progress_token`` + ``seed_input_path`` — a budget on the
        // params would be silently dropped. The helper then reads the
        // resolved budget back via ``project.inspect`` downstream. We
        // skip this round-trip when no budget was persisted (older
        // snapshot from before PR #576): in that case agentshore.yaml
        // keeps whatever it already had.
        const persistedBudget = readBudget();
        if (persistedBudget !== null) {
          try {
            await setBudgetCall(budgetSelectionToConfig(persistedBudget));
          } catch (err) {
            reportFailure(err, "budget");
            return;
          }
        }

        // Hand off to the shared helper for
        // select → inspect → start → navigate.
        await quickStart(path, {
          navigate,
          onError: (err, failedStep) => {
            reportFailure(err, helperStepToUiStep(failedStep));
          },
        });
      } catch (err) {
        reportFailure(err, "readiness");
      }
    },
    [
      adapter,
      navigate,
      onQuickStartFailed,
      quickStart,
      readBudget,
      setBudgetCall,
    ],
  );

  const loaded = entries !== null;
  const isEmpty = loaded && entries.length === 0;

  return (
    <main className={styles.screen}>
      <header className={styles.header}>
        <div className={styles.headerText}>
          <h1>Choose a project</h1>
          <p>AgentShore manages one repository at a time. Recent projects stay one click away.</p>
        </div>
        <div className={styles.actions}>
          <button
            type="button"
            className={`${styles.button} ${styles.buttonPrimary}`}
            onClick={() => void onOpenRepository()}
          >
            Open repository
          </button>
        </div>
      </header>

      {error !== null && (
        <div role="alert" className={styles.error}>
          {error}
        </div>
      )}

      <section className={styles.panel}>
        <div className={styles.panelHead}>
          <h2>Recent projects</h2>
          <span className={styles.small}>Sorted by last session</span>
        </div>

        {!loaded && <p>Loading…</p>}

        {isEmpty && (
          <div className={styles.empty}>
            <p>No recent projects yet</p>
            <button
              type="button"
              className={`${styles.button} ${styles.buttonPrimary}`}
              onClick={() => void onOpenRepository()}
            >
              Open repository
            </button>
          </div>
        )}

        {loaded && entries.length > 0 && (
          <div className={styles.list}>
            {entries.map((entry) => (
              <button
                key={entry.path}
                type="button"
                className={styles.row}
                data-testid={`recent-row-${entry.path}`}
                onClick={() => void onOpenProject(entry.path)}
              >
                <span className={styles.rowMain}>
                  <span className={styles.rowLabel}>{entry.label}</span>
                  <span className={styles.rowPath}>{entry.path}</span>
                </span>
                <span className={styles.rowMeta}>
                  <span className={styles.relative}>{formatRelative(entry.last_started)}</span>
                  {entry.last_exit_reason !== null && (
                    <span className={`${styles.badge} ${styles.badgeReason}`}>
                      {entry.last_exit_reason}
                    </span>
                  )}
                  {entry.has_valid_config ? (
                    <span className={`${styles.badge} ${styles.badgeReady}`}>Ready</span>
                  ) : (
                    <span className={`${styles.badge} ${styles.badgeKnown}`}>Known</span>
                  )}
                  {entry.has_valid_config &&
                    entry.last_started.length > 0 &&
                    hasPersistedSetup(entry.path) && (
                      <span
                        role="button"
                        tabIndex={0}
                        aria-label={`Quick Start ${entry.label}`}
                        data-testid={`quick-start-${entry.path}`}
                        className={`${styles.button} ${styles.buttonPrimary} ${styles.quickStartButton}`}
                        onClick={(event) => {
                          event.stopPropagation();
                          void onQuickStart(entry.path);
                        }}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.stopPropagation();
                            event.preventDefault();
                            void onQuickStart(entry.path);
                          }
                        }}
                      >
                        Quick Start
                      </span>
                    )}
                  <span
                    role="button"
                    tabIndex={0}
                    aria-label={`Remove ${entry.label}`}
                    className={styles.removeButton}
                    onClick={(event) => {
                      event.stopPropagation();
                      void onRemove(entry.path);
                    }}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.stopPropagation();
                        event.preventDefault();
                        void onRemove(entry.path);
                      }
                    }}
                  >
                    Remove
                  </span>
                </span>
              </button>
            ))}
          </div>
        )}
      </section>
    </main>
  );
}
