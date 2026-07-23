import type { BudgetSelection } from "../screens/BudgetScreen";
import type { StartSelection } from "../screens/StartScreen";

export type SetupState = {
  targetBranch: string;
  enabledAgents: string[];
  identities: string[];
  budget: BudgetSelection;
  startSelection: StartSelection;
  /** Whether the optional timelapse-capture feature is installed (from yaml). */
  timelapseInstalled: boolean;
  /** Gate issue pickup to issues opened by trusted identities
   *  (``trusted_ids.restrict_issues_to_trusted_authors``). */
  trustedIssueEnforcement: boolean;
  /** Trusted-source GitHub logins (``trusted_ids.github_logins``), mirrored
   *  from the sidecar so the panel can pre-paint before its own ``list()``
   *  resolves. The TrustedSourcesScreen still self-loads — this is the
   *  hydration parity copy, not a replacement. */
  trustedSources: string[];
};

export type SetupScreen =
  | "readiness"
  | "target-branch"
  | "identities"
  | "agents"
  | "budget"
  | "start";

export const SETUP_STORAGE_KEY = "agentshore.desktop.setup.v1";
export const SETUP_SCREENS: Array<{ id: SetupScreen; label: string }> = [
  { id: "readiness", label: "Readiness" },
  { id: "target-branch", label: "Target Branch" },
  { id: "identities", label: "Trusted Identities" },
  { id: "agents", label: "Agents" },
  { id: "budget", label: "Budget" },
  { id: "start", label: "Start" },
];
export const SETUP_SCREEN_IDS = new Set<string>(SETUP_SCREENS.map((screen) => screen.id));

export const defaultSetupState: SetupState = {
  targetBranch: "main",
  enabledAgents: ["codex", "claude_code", "antigravity"],
  identities: [],
  budget: { mode: "unlimited", total: 0, timeMode: "unlimited", timeMinutes: 1440 },
  startSelection: { seedInputPath: null },
  timelapseInstalled: false,
  trustedIssueEnforcement: false,
  trustedSources: [],
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseBudgetSelection(value: unknown): BudgetSelection {
  if (!isRecord(value)) {
    return defaultSetupState.budget;
  }
  const mode: BudgetSelection["mode"] = value.mode === "capped" ? "capped" : "unlimited";
  const totalRaw = value.total;
  const total =
    typeof totalRaw === "number" && Number.isFinite(totalRaw) && totalRaw >= 0
      ? totalRaw
      : defaultSetupState.budget.total;
  // Time dimension (independent). Older persisted snapshots predate these
  // fields — fall back to the defaults so the wizard stays back-compatible.
  const timeMode: BudgetSelection["timeMode"] =
    value.timeMode === "capped" ? "capped" : "unlimited";
  const timeMinutesRaw = value.timeMinutes;
  const timeMinutes =
    typeof timeMinutesRaw === "number" && Number.isFinite(timeMinutesRaw) && timeMinutesRaw >= 0
      ? timeMinutesRaw
      : defaultSetupState.budget.timeMinutes;
  return { mode, total, timeMode, timeMinutes };
}

function parseStartSelection(value: unknown): StartSelection {
  if (!isRecord(value)) {
    return defaultSetupState.startSelection;
  }
  const seedInputPath =
    typeof value.seedInputPath === "string" && value.seedInputPath.length > 0
      ? value.seedInputPath
      : typeof value.seedFilePath === "string" && value.seedFilePath.length > 0
      ? value.seedFilePath
      : null;
  return { seedInputPath };
}

export function isSetupScreen(value: string | undefined): value is SetupScreen {
  return value !== undefined && SETUP_SCREEN_IDS.has(value);
}

export function loadStoredSetup(): SetupState {
  try {
    const raw = localStorage.getItem(SETUP_STORAGE_KEY);
    if (!raw) {
      return defaultSetupState;
    }
    const parsed: unknown = JSON.parse(raw);
    if (!isRecord(parsed)) {
      return defaultSetupState;
    }
    return {
      targetBranch:
        typeof parsed.targetBranch === "string" && parsed.targetBranch.length > 0
          ? parsed.targetBranch
          : defaultSetupState.targetBranch,
      enabledAgents: Array.isArray(parsed.enabledAgents)
        ? parsed.enabledAgents.filter((value): value is string => typeof value === "string")
        : defaultSetupState.enabledAgents,
      identities: Array.isArray(parsed.identities)
        ? parsed.identities.filter((value): value is string => typeof value === "string")
        : defaultSetupState.identities,
      budget: parseBudgetSelection(parsed.budget),
      startSelection: parseStartSelection(parsed.startSelection),
      timelapseInstalled: parsed.timelapseInstalled === true,
      trustedIssueEnforcement: parsed.trustedIssueEnforcement === true,
      trustedSources: Array.isArray(parsed.trustedSources)
        ? parsed.trustedSources.filter(
            (value): value is string => typeof value === "string",
          )
        : defaultSetupState.trustedSources,
    };
  } catch {
    return defaultSetupState;
  }
}

export function persistSetup(next: SetupState): void {
  localStorage.setItem(SETUP_STORAGE_KEY, JSON.stringify(next));
}
