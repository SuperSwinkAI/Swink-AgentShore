import { callJsonRpc } from "./jsonrpc";

export interface TierModel {
  enabled?: boolean;
  model?: string;
  reasoning_effort?: string;
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
}

export async function getAgentsCatalog(): Promise<AgentsCatalog> {
  return callJsonRpc<AgentsCatalog>("agents.catalog");
}

export interface AgentSpawnLimits {
  max_per_config: number;
}

export async function getAgentSpawnLimits(): Promise<AgentSpawnLimits> {
  return callJsonRpc<AgentSpawnLimits>("agents.get_spawn_limits");
}

export async function setAgentSpawnLimits(patch: Partial<AgentSpawnLimits>): Promise<void> {
  await callJsonRpc<unknown>("agents.set_spawn_limits", patch);
}
