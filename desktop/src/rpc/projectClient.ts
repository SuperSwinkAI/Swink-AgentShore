import { callJsonRpc } from "./jsonrpc";

export interface ProjectSelection {
  path: string;
}

export interface BranchRow {
  name: string;
  is_default: boolean;
  is_current: boolean;
  is_remote: boolean;
  ahead: number;
  behind: number;
}

export interface RepoIdentity {
  is_git: boolean;
  root?: string;
  head_sha?: string;
  origin_url?: string | null;
}

/**
 * Typed hydration fields returned by ``project.inspect`` in
 * ``agentshore_yaml.parsed``. The Python sidecar parses agentshore.yaml with
 * the real config loader and surfaces these fields so the desktop setup flow
 * never needs its own YAML parser (issue #123).
 */
export interface ParsedYamlFields {
  target_branch: string | null;
  enabled_agents: string[];
  budget: {
    enabled: boolean;
    total: number;
    warning_threshold: number;
    time_enabled: boolean;
    time_total_minutes: number;
  };
  timelapse_enabled: boolean;
  timelapse_installed: boolean;
  trusted_sources: string[];
  identity_logins: string[];
  trusted_issue_enforcement: boolean;
}

export interface ProjectInspectResult {
  path: string;
  repo_identity: RepoIdentity;
  branch: string | null;
  detected_tools: string[];
  agentshore_yaml: {
    path: string;
    raw?: string;
    error?: string;
    /** Typed fields parsed by the Python sidecar. Present when the file was
     *  read and the config loader succeeded; absent on read error or parse
     *  failure. */
    parsed?: ParsedYamlFields;
  } | null;
  beads_status: { initialised: boolean };
  prerequisites: { git: boolean; bd: boolean; gh: boolean };
}

// ---------------------------------------------------------------------------
// Budget conversion helpers (moved here from setup/projectYaml.ts; issue #123)
// ---------------------------------------------------------------------------

/**
 * Re-shape a ``ParsedYamlFields`` budget into the SetupState ``budget`` shape
 * the desktop carries on the rail. Returns ``null`` when the input is absent or
 * has nothing actionable (the caller should keep its current value).
 */
export function budgetHydrationToSelection(
  hydration: ParsedYamlFields["budget"] | null | undefined,
): {
  mode: "capped" | "unlimited";
  total: number;
  timeMode: "capped" | "unlimited";
  timeMinutes: number;
} | null {
  if (hydration == null) return null;
  const dollar = hydration.enabled
    ? { mode: "capped" as const, total: hydration.total }
    : { mode: "unlimited" as const, total: 0 };
  const time = hydration.time_enabled
    ? { timeMode: "capped" as const, timeMinutes: hydration.time_total_minutes }
    : { timeMode: "unlimited" as const, timeMinutes: 0 };
  return { ...dollar, ...time };
}

/**
 * Serialize a SetupState budget selection into the ``BudgetConfig`` shape that
 * ``project.set_budget`` / ``session.set_budget`` accept on the wire. Mirrors
 * ``src/agentshore/config/models.py:BudgetConfig``.
 */
export function budgetSelectionToConfig(selection: {
  mode: "capped" | "unlimited";
  total: number;
  timeMode?: "capped" | "unlimited";
  timeMinutes?: number;
}): {
  enabled: boolean;
  total: number;
  time_enabled: boolean;
  time_total_minutes: number;
} {
  const dollar =
    selection.mode === "capped"
      ? { enabled: true, total: selection.total }
      : { enabled: false, total: 0.0 };
  const timeCapped = selection.timeMode === "capped";
  return {
    ...dollar,
    time_enabled: timeCapped,
    time_total_minutes: timeCapped ? (selection.timeMinutes ?? 0) : 0,
  };
}

export interface SelectProjectOptions {
  includeInspect?: boolean;
}

export async function selectProject(
  path: string,
  options: SelectProjectOptions = {},
): Promise<ProjectSelection> {
  return callJsonRpc<ProjectSelection>("project.select", {
    path,
    include_inspect: options.includeInspect ?? false,
  });
}

export async function deselectProject(): Promise<void> {
  await callJsonRpc<unknown>("project.deselect");
}

export async function listBranches(refresh = false): Promise<BranchRow[]> {
  const result = await callJsonRpc<BranchRow[] | null>("project.branches", { refresh });
  return result ?? [];
}

export async function setTargetBranch(name: string): Promise<{ target_branch: string }> {
  return callJsonRpc<{ target_branch: string }>("project.set_target_branch", { name });
}

/**
 * Persist seed material paths to ``intake.seed_paths`` in agentshore.yaml via
 * ``project.set_seed_paths``. An empty array clears the configured seed. Once
 * persisted, every start path (CLI, sidecar, Quick Start, TUI) picks the seed
 * up through the engine's ``_resolve_seed_path`` fallback — so the seed is no
 * longer a transient, drop-prone parameter.
 */
export async function setSeedPaths(
  seedPaths: string[],
): Promise<{ seed_paths: string[]; yaml_path: string }> {
  return callJsonRpc<{ seed_paths: string[]; yaml_path: string }>("project.set_seed_paths", {
    seed_paths: seedPaths,
  });
}

export type { ProjectBudgetInput } from "./budget";
/** @deprecated Use {@link ProjectBudgetInput} — this alias will be removed. */
export type { ProjectBudgetInput as BudgetRpcInput } from "./budget";

export interface BudgetRpcResult {
  budget: {
    enabled: boolean;
    total: number;
    warning_threshold: number;
    time_enabled: boolean;
    time_total_minutes: number;
  };
  yaml_path: string;
}

export async function setBudget(
  budget: import("./budget").ProjectBudgetInput,
): Promise<BudgetRpcResult> {
  return callJsonRpc<BudgetRpcResult>("project.set_budget", { budget });
}

/**
 * Payload/result for the optional timelapse-capture feature. Mirrors the
 * ``TimelapseConfig`` dataclass at ``src/agentshore/config/models.py``.
 */
export interface TimelapseRpcInput {
  enabled?: boolean;
  installed?: boolean;
}

export interface TimelapseRpcResult {
  timelapse: { enabled: boolean; installed: boolean };
  yaml_path: string;
}

export interface TimelapseInstallResult {
  success: boolean;
  message: string;
  installed: boolean;
  yaml_path?: string;
}

/** Persist the ``timelapse`` block (enabled/installed) to agentshore.yaml. */
export async function setTimelapse(timelapse: TimelapseRpcInput): Promise<TimelapseRpcResult> {
  return callJsonRpc<TimelapseRpcResult>("project.set_timelapse", { timelapse });
}

/**
 * Auto-install the timelapse-capture CLI + dependencies (ffmpeg, Node 24+,
 * Playwright Chromium). Long-running; on success the sidecar also persists
 * ``timelapse.installed = true``.
 */
export async function installTimelapse(): Promise<TimelapseInstallResult> {
  return callJsonRpc<TimelapseInstallResult>("project.install_timelapse", {});
}

/**
 * Persist the "only work issues opened by trusted identities" toggle to
 * ``trusted_ids.restrict_issues_to_trusted_authors`` in agentshore.yaml via
 * ``project.set_trusted_issue_enforcement``. The sidecar echoes the stored
 * boolean and the resolved yaml path.
 */
export async function setTrustedIssueEnforcement(
  enabled: boolean,
): Promise<{ enabled: boolean; yaml_path: string }> {
  return callJsonRpc<{ enabled: boolean; yaml_path: string }>(
    "project.set_trusted_issue_enforcement",
    { enabled },
  );
}

export async function inspectProject(): Promise<ProjectInspectResult> {
  return callJsonRpc<ProjectInspectResult>("project.inspect");
}
