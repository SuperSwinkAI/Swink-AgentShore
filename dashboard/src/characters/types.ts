import { AGENT_REGISTRY } from "../agentRegistry";
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


  x: number;
  y: number;

  path: Tile[];
  pathIndex: number;
  targetState: CharacterState;
  targetDirection?: Direction | null;

  animFrame: number;
  animTimer: number;

  wanderTimer: number;


  reservedSeatKey: string | null;


  activePlayId: number | null;
  activePlayType: string | null;


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


export const AGENT_COLORS: Record<string, AgentColors> = Object.fromEntries(
  Object.entries(AGENT_REGISTRY).map(([key, entry]) => [
    key,
    { fill: entry.colorFill, label: entry.colorLabel },
  ]),
);
