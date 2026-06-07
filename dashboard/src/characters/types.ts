import type { Tile } from "../office/layout";

export enum CharacterState {
  IDLE = "idle",
  WALK = "walk",
  WORK = "work",
}

export enum Direction {
  DOWN = 0,
  UP = 1,
  RIGHT = 2,
  LEFT = 3,
}

export type AgentModelTier = "small" | "medium" | "large";

export const DEFAULT_AGENT_MODEL_TIER: AgentModelTier = "medium";

export function normalizeAgentModelTier(
  modelTier: string | null | undefined,
): AgentModelTier {
  const normalized = modelTier?.toLowerCase();
  if (
    normalized === "small" ||
    normalized === "medium" ||
    normalized === "large"
  ) {
    return normalized;
  }
  return DEFAULT_AGENT_MODEL_TIER;
}

export interface Character {
  agentId: string;
  agentType: string;
  modelTier?: AgentModelTier;
  displayName?: string;
  npcKind?: NpcKind;
  scale?: number;
  state: CharacterState;
  direction: Direction;

  // position in world pixels
  x: number;
  y: number;

  // walk path
  path: Tile[];
  pathIndex: number;
  targetState: CharacterState;
  targetDirection?: Direction | null;

  // animation
  animFrame: number;
  animTimer: number;

  // idle wander
  wanderTimer: number;

  // reserved seat key (for cleanup on reassign/removal)
  reservedSeatKey: string | null;

  // play_id of the most recently started play for this agent; used to ignore
  // stale play_completed events that arrive after a newer play_started
  activePlayId: number | null;
  activePlayType: string | null;

  // agent status from AgentShore
  status: string;

  bubble: CharacterBubble | null;
  bubbleUntil: number | null;
  opacity: number;
  despawning: boolean;
  despawnOnArrival: boolean;
}

export type CharacterBubbleKind =
  | "work"
  | "success"
  | "fail"
  | "feedback"
  | "error";

export type CharacterBubble =
  | CharacterBubbleKind
  | {
      text: string;
      tone?: CharacterBubbleKind;
    };

export enum NpcKind {
  MASTIFF = "mastiff",
  GERMAN_SHEPHERD = "german_shepherd",
  RUSSIAN_BLUE_CAT = "russian_blue_cat",
}

export interface NpcDefinition {
  id: string;
  name: string;
  kind: NpcKind;
  scale: number;
  startTile: Tile;
}

export interface AgentColors {
  fill: string;
  label: string;
}

export const AGENT_COLORS: Record<string, AgentColors> = {
  claude_code: { fill: "#E07B39", label: "C" },
  codex: { fill: "#F4D44D", label: "X" },
  gemini: { fill: "#4285F4", label: "G" },
  grok: { fill: "#14B8A6", label: "K" },
};
