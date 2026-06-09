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

export interface ProjectInspectResult {
  path: string;
  repo_identity: RepoIdentity;
  branch: string | null;
  detected_tools: string[];
  agentshore_yaml: { path: string; raw?: string; error?: string } | null;
  beads_status: { initialised: boolean };
  prerequisites: { git: boolean; bd: boolean; gh: boolean };
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
