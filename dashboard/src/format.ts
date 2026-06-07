import type { ActivePlay, AgentSnapshot } from "./types";

const COMPACT_PLAY_LABELS: Record<string, string> = {
  write_implementation_plan: "Write Plan",
};

export function formatPlayType(pt: string): string {
  const compactLabel = COMPACT_PLAY_LABELS[pt];
  if (compactLabel) return compactLabel;

  return pt
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .replace(/\b(Qa|Pr|Rl|Ppo)\b/g, (c) => c.toUpperCase());
}

export function formatAgentType(agentType: string): string {
  return agentType
    .replace("api_", "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function formatAgentClass(agent: AgentSnapshot | undefined): string {
  if (!agent) return "System";
  return `${formatModelTier(agent.model_tier)} ${formatAgentKind(agent.agent_type)}`;
}

export function shortAgentName(
  agent: AgentSnapshot | undefined,
  fallback = "Session",
): string {
  if (!agent) return fallback;

  const displayName = agent.display_name?.trim();
  if (displayName) {
    const prefixIndex = displayName.lastIndexOf(":");
    const instanceName =
      prefixIndex >= 0
        ? displayName.slice(prefixIndex + 1).trim()
        : displayName;
    if (instanceName) return instanceName;
  }

  return agent.agent_id || fallback;
}

export function displayAgentName(
  agent: AgentSnapshot | undefined,
  fallback = "Session",
): string {
  if (!agent) return fallback;
  return (
    agent.display_name ||
    agent.agent_type.replace("api_", "") ||
    agent.agent_id.slice(0, 8)
  );
}

export function formatPlayWithTarget(
  playType: string,
  context?: Partial<Pick<ActivePlay, "issue_number" | "pr_number">>,
): string {
  const label = formatPlayType(playType);
  const issueNumber = context?.issue_number ?? null;
  const prNumber = context?.pr_number ?? null;
  const targetNumber = preferredPlayTarget(playType, issueNumber, prNumber);
  return targetNumber === null ? label : `${label} ${targetNumber}`;
}

export function formatMoney(value: number): string {
  return `$${value.toFixed(2)}`;
}

export function formatPolicyMode(mode: string): string {
  return mode === "audit-replay" ? "audit replay" : "learning";
}

function formatModelTier(modelTier: string | null | undefined): string {
  return titleCase(modelTier ?? "medium");
}

function formatAgentKind(agentType: string): string {
  switch (agentType) {
    case "claude_code":
      return "Claude";
    case "codex":
      return "Codex";
    case "gemini":
      return "Gemini";
    case "grok":
      return "Grok";
    default:
      return titleCase(agentType.replace(/_/g, " "));
  }
}

function titleCase(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function preferredPlayTarget(
  playType: string,
  issueNumber: number | null,
  prNumber: number | null,
): number | null {
  if (
    (playType.includes("pr") ||
      playType.includes("review") ||
      playType.includes("merge")) &&
    prNumber !== null
  ) {
    return prNumber;
  }
  return issueNumber ?? prNumber;
}
