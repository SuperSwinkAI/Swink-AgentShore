/**
 * Shared "start a session from the previously-persisted setup" helper.
 *
 * Two endpoints converge on this code path (issue #561 Repeat button on the
 * End-Session Report; issue #565 Quick Start tile on the Chooser): both
 * skip the setup rail and want a session running against whatever was
 * already configured for the project — same target branch, same agents,
 * same identities, same budget.
 *
 * The flow:
 *
 *   1. Re-select the project (``project.select``) so the sidecar's
 *      ``ServerState.active_project_path`` matches what the caller wants.
 *      Required: ``session.start`` reads its config off the active project,
 *      and the sidecar may have been told to forget the previous selection
 *      (``project.deselect``) on its way to the ESR / chooser screen.
 *   2. Read the project's agentshore.yaml via ``project.inspect`` and hydrate
 *      the budget slice so the caller can layer the persisted-local budget
 *      back on top via ``budgetSelectionToConfig``. The budget is not
 *      currently sent on the ``session.start`` wire (the sidecar reads it
 *      out of ``agentshore.yaml``); the call still goes through so callers
 *      that DO want to mutate budget-on-disk before start (a near-term
 *      ``project.set_budget`` RPC arrives via #580) can plug in here.
 *   3. Navigate to ``/starting`` with the persisted seed input path so
 *      StartingProgressRoute fires ``session.start`` itself and the user
 *      sees the progress steps in real time.
 *
 * Errors from any RPC are surfaced to ``opts.onError`` with a string
 * identifying which step failed (``select`` / ``inspect`` / ``start``).
 * Callers typically translate those into an inline banner; rethrowing is
 * intentionally avoided so a click handler that doesn't await still gets
 * a useful error path.
 *
 * Co-owned by issues #561 (Repeat) and #565 (Quick Start). The signature is
 * intentionally minimal so both endpoints stay in lockstep — if you need
 * more knobs add them to ``opts`` rather than forking the signature.
 */

import type { NavigateFunction } from "react-router-dom";

import {
  budgetHydrationToSelection,
  budgetSelectionToConfig,
  parseProjectYaml,
} from "./projectYaml";
import { inspectProject, selectProject } from "../rpc/projectClient";


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
    // Time dimension. Absent on older snapshots — default to unlimited.
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
  // Defensive — localStorage access can throw inside locked-down WebViews;
  // a missing seed is fine, return null so the session.start RPC defaults
  // to its own seed_project resolution.
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

  // Step 1: re-select. ``project.select`` is idempotent on the sidecar
  // side — if the active project already matches, this still succeeds and
  // refreshes the recent-touch timestamp.
  try {
    await selectImpl(projectPath);
  } catch (err) {
    opts.onError?.(err, "select");
    return;
  }

  // Step 2: inspect + hydrate budget. The budget is read here so callers
  // that want to round-trip it through a future ``project.set_budget`` RPC
  // (#580) have a single place to slot in. Currently we only re-compute it
  // for side-effect-free symmetry with the setup rail's hydration flow.
  try {
    const inspect = await inspectImpl();
    const hydration = parseProjectYaml(inspect.agentshore_yaml?.raw ?? null);
    const budget = budgetHydrationToSelection(hydration.budget);
    if (budget !== null) {
      // budgetSelectionToConfig is the shape ``project.set_budget`` will
      // accept once #580 lands. Compute it eagerly so a future write step
      // is a one-liner — and so a typo in either helper trips at runtime
      // here rather than only inside a not-yet-existing RPC.
      void budgetSelectionToConfig(budget);
    }
  } catch (err) {
    opts.onError?.(err, "inspect");
    return;
  }

  // Step 3: navigate to the starting-progress screen and let IT fire
  // ``session.start``. This way the user sees the progress steps in real
  // time instead of the modal being skipped. The route's existing
  // non-handoff path (``sessionStarted`` falsy) handles the RPC dispatch.
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
