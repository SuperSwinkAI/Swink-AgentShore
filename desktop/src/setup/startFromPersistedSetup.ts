/**
 * Start a session from persisted project setup through Quick Start.
 *
 * Select the active project, inspect its configuration, hydrate its budget,
 * and navigate to /starting with the persisted seed input path.
 * StartingProgressRoute owns session.start. Budget changes are persisted
 * through project.set_budget before the session starts.
 *
 * RPC errors are reported to opts.onError with the failed step; they are
 * not rethrown to the click handler.
 */

import type { NavigateFunction } from "react-router-dom";

import {
  budgetHydrationToSelection,
  budgetSelectionToConfig,
  inspectProject,
  selectProject,
} from "../rpc/projectClient";


const SETUP_STORAGE_KEY = "agentshore.desktop.setup.v1";

/**
 * Pure localStorage probe — does a persisted setup snapshot exist for this
 * desktop? Quick Start (#565) uses this to gate its button on the Choose-a-
 * Project rows. Returns false on any access error (locked-down WebView,
 * malformed JSON, missing object shape) so the UI never shows Quick Start
 * for state it can't actually replay.
 */
export function persistedSetupExists(
  storage: Pick<Storage, "getItem"> | null = typeof localStorage !== "undefined"
    ? localStorage
    : null,
): boolean {
  if (storage === null) return false;
  try {
    const raw = storage.getItem(SETUP_STORAGE_KEY);
    if (raw === null || raw.length === 0) return false;
    const parsed: unknown = JSON.parse(raw);
    return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed);
  } catch {
    return false;
  }
}

/**
 * Read the persisted ``budget`` slice from the Setup-rail localStorage
 * snapshot. Quick Start (#565) needs this to round-trip the budget into
 * ``project.set_budget`` (#580) BEFORE firing ``session.start``, because
 * ``sessionClient.startSession`` only forwards ``progress_token`` and
 * ``seed_input_path`` on the wire — a ``budget`` field on the params would
 * be silently dropped. Returns ``null`` when no budget was persisted; the
 * caller is expected to skip the set_budget round-trip in that case and
 * fall back to whatever ``agentshore.yaml`` already records.
 */
export function readPersistedBudget(): {
  mode: "capped" | "unlimited";
  total: number;
  timeMode: "capped" | "unlimited";
  timeMinutes: number;
} | null {
  if (typeof localStorage === "undefined") return null;
  try {
    const raw = localStorage.getItem(SETUP_STORAGE_KEY);
    if (raw === null || raw.length === 0) return null;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return null;
    }
    const budget = (parsed as Record<string, unknown>).budget;
    if (typeof budget !== "object" || budget === null || Array.isArray(budget)) {
      return null;
    }
    const v = budget as Record<string, unknown>;
    const mode = v.mode === "capped" ? "capped" : v.mode === "unlimited" ? "unlimited" : null;
    if (mode === null) return null;
    const totalRaw = v.total;
    const total =
      typeof totalRaw === "number" && Number.isFinite(totalRaw) && totalRaw >= 0
        ? totalRaw
        : 0;

    const timeMode = v.timeMode === "capped" ? "capped" : "unlimited";
    const timeMinutesRaw = v.timeMinutes;
    const timeMinutes =
      typeof timeMinutesRaw === "number" && Number.isFinite(timeMinutesRaw) && timeMinutesRaw >= 0
        ? timeMinutesRaw
        : 0;
    return { mode, total, timeMode, timeMinutes };
  } catch {
    return null;
  }
}

export type StartFromPersistedSetupStep =
  | "select"
  | "inspect"
  | "start"
  | "navigate";

export interface StartFromPersistedSetupOptions {
  /** Called when any step in the pipeline fails. ``failedStep`` lets the
   *  caller surface a step-specific banner ("couldn't open project",
   *  "couldn't start session") without parsing the error message. */
  onError?: (err: unknown, failedStep: StartFromPersistedSetupStep) => void;
  /** React Router navigate handle. Provided by callers so this helper stays
   *  testable without a MemoryRouter — pass ``useNavigate()`` from the
   *  component that owns the click. */
  navigate?: NavigateFunction;
  /** Override for the seedInputPath persisted in localStorage. Tests use
   *  this to bypass the localStorage read. */
  seedInputPathOverride?: string | null;
  /** Overridable RPC seam for tests. Default is the production
   *  ``projectClient.selectProject``. */
  selectProjectImpl?: typeof selectProject;
  /** Overridable RPC seam for tests. */
  inspectProjectImpl?: typeof inspectProject;
}

interface PersistedSetupSeed {
  seedInputPath: string | null;
}

function readPersistedSeed(): PersistedSetupSeed {



  try {
    const raw = localStorage.getItem(SETUP_STORAGE_KEY);
    if (!raw) return { seedInputPath: null };
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) {
      return { seedInputPath: null };
    }
    const start = (parsed as Record<string, unknown>).startSelection;
    if (typeof start !== "object" || start === null) {
      return { seedInputPath: null };
    }
    const seed = (start as Record<string, unknown>).seedInputPath;
    if (typeof seed === "string" && seed.length > 0) {
      return { seedInputPath: seed };
    }
    return { seedInputPath: null };
  } catch {
    return { seedInputPath: null };
  }
}

/**
 * Re-select the project, replay the persisted setup, and trigger a fresh
 * session. Resolves once ``session.start`` has been dispatched (the actual
 * bringup is driven by the ``/starting`` route's progress listener).
 *
 * @param projectPath - absolute path to the project to re-select.
 * @param opts - error handler + test seams.
 */
export async function startSessionFromPersistedSetup(
  projectPath: string,
  opts: StartFromPersistedSetupOptions = {},
): Promise<void> {
  const selectImpl = opts.selectProjectImpl ?? selectProject;
  const inspectImpl = opts.inspectProjectImpl ?? inspectProject;




  try {
    await selectImpl(projectPath);
  } catch (err) {
    opts.onError?.(err, "select");
    return;
  }





  try {
    const inspect = await inspectImpl();
    const budget = budgetHydrationToSelection(inspect.agentshore_yaml?.parsed?.budget);
    if (budget !== null) {
      void budgetSelectionToConfig(budget);
    }
  } catch (err) {
    opts.onError?.(err, "inspect");
    return;
  }





  const { seedInputPath } =
    opts.seedInputPathOverride !== undefined
      ? { seedInputPath: opts.seedInputPathOverride }
      : readPersistedSeed();
  if (opts.navigate) {
    try {
      opts.navigate("/starting", {
        state: { seedInputPath },
      });
    } catch (err) {
      opts.onError?.(err, "navigate");
    }
  }
}
