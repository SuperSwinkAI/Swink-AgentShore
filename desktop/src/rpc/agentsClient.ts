import { callJsonRpc } from "./jsonrpc";

export interface TierModel {
  enabled?: boolean;
  model?: string;
  reasoning_effort?: string;
  max?: number;
}

export interface AgentRow {
  type: string;
  enabled: boolean;
  identity: string | null;
  tier_models: Record<string, TierModel>;
}

export interface AgentConfigurePatch {
  enabled?: boolean;
  identity?: string | null;
  tier_models?: Record<string, TierModel>;
}

export async function listAgents(): Promise<AgentRow[]> {
  const result = await callJsonRpc<AgentRow[] | null>("agents.list");
  return result ?? [];
}

export async function detectAgents(): Promise<string[]> {
  const result = await callJsonRpc<string[] | null>("agents.detect");
  return result ?? [];
}

export async function configureAgent(
  type: string,
  patch: AgentConfigurePatch,
): Promise<void> {
  await callJsonRpc<unknown>("agents.configure", { type, ...patch });
}

export interface CatalogTierDefault {
  model: string | null;
  reasoning_effort: string | null;
}

export interface AgentsCatalog {
  models: Record<string, string[]>;
  defaults: Record<string, Record<string, CatalogTierDefault>>;
  efforts: Record<string, string[]>;
}

export async function getAgentsCatalog(): Promise<AgentsCatalog> {
  return callJsonRpc<AgentsCatalog>("agents.catalog");
}

export type HarnessRefreshStatus =
  | "ok"
  | "unavailable"
  | "timeout"
  | "error"
  | "budget_exceeded"
  | "skipped";

export interface HarnessRefreshResult {
  status: HarnessRefreshStatus;
  models: string[];
  added: string[];
  removed: string[];
  detail: string;
  cost_usd: number;
}

export interface ModelRefreshSummary {
  harnesses: Record<string, HarnessRefreshResult>;
  unpriced_models: [string, string][];
  total_cost_usd: number;
  dry_run: boolean;
  any_changes: boolean;
}

export interface RefreshModelsParams {
  includeClaudeCode?: boolean;
  tier?: string;
  maxBudgetUsd?: number;
  dryRun?: boolean;
}

/**
 * Refresh the model catalog. By default only the three free harnesses
 * (Codex/Grok/Antigravity) are probed — no cost, no confirmation needed.
 * Pass `includeClaudeCode: true` only after the caller has already shown the
 * user a cost warning and gotten explicit confirmation: that path dispatches
 * a real, paid LLM agent call (typically $0.30-0.50).
 */
export async function refreshModels(
  params: RefreshModelsParams = {},
): Promise<ModelRefreshSummary> {
  return callJsonRpc<ModelRefreshSummary>("agents.refresh_models", {
    include_claude_code: params.includeClaudeCode ?? false,
    ...(params.tier !== undefined ? { tier: params.tier } : {}),
    ...(params.maxBudgetUsd !== undefined ? { max_budget_usd: params.maxBudgetUsd } : {}),
    dry_run: params.dryRun ?? false,
  });
}

