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

export async function selectProject(path: string): Promise<ProjectSelection> {
  return callJsonRpc<ProjectSelection>("project.select", { path });
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

/**
 * Payload shape for ``project.set_budget`` — mirrors the ``BudgetConfig``
 * dataclass at ``src/agentshore/config/models.py:92``. ``warning_threshold``
 * is optional; the sidecar defaults it to 0.20 when omitted.
 */
export interface BudgetRpcInput {
  enabled: boolean;
  total: number;
  warning_threshold?: number;
}

export interface BudgetRpcResult {
  budget: {
    enabled: boolean;
    total: number;
    warning_threshold: number;
  };
  yaml_path: string;
}

export async function setBudget(budget: BudgetRpcInput): Promise<BudgetRpcResult> {
  return callJsonRpc<BudgetRpcResult>("project.set_budget", { budget });
}

export async function inspectProject(): Promise<ProjectInspectResult> {
  return callJsonRpc<ProjectInspectResult>("project.inspect");
}
