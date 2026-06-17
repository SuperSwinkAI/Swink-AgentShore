/**
 * Single source of truth for all per-agent-type metadata on the TypeScript
 * side.  Adding a new agent type requires ONE entry here; the AgentType union
 * in types.ts and every label/color/sprite lookup across the UI is derived
 * automatically.
 *
 * Adding an agent type:
 *  1. Add an entry to AGENT_REGISTRY below.
 *  2. Add the sprite PNG imports above the registry (following the existing
 *     pattern).  The AgentType union and all downstream maps update for free.
 */

import claudeLargeSpriteUrl from "./assets/agents/v2/claude-large-humanoid.png";
import claudeMediumSpriteUrl from "./assets/agents/v2/claude-medium-humanoid.png";
import claudeSmallSpriteUrl from "./assets/agents/v2/claude-small-ball.png";
import codexLargeSpriteUrl from "./assets/agents/v2/codex-large-humanoid.png";
import codexMediumSpriteUrl from "./assets/agents/v2/codex-medium-humanoid.png";
import codexSmallSpriteUrl from "./assets/agents/v2/codex-small-ball.png";
import geminiLargeSpriteUrl from "./assets/agents/v2/gemini-large-humanoid.png";
import geminiMediumSpriteUrl from "./assets/agents/v2/gemini-medium-humanoid.png";
import geminiSmallSpriteUrl from "./assets/agents/v2/gemini-small-ball.png";
import grokLargeSpriteUrl from "./assets/agents/v2/grok-large-humanoid.png";
import grokMediumSpriteUrl from "./assets/agents/v2/grok-medium-humanoid.png";
import grokSmallSpriteUrl from "./assets/agents/v2/grok-small-ball.png";
import type { AgentModelTier } from "./characters/types";

const V2_SPRITE_FRAME_WIDTH = 416;
const V2_SPRITE_FRAME_HEIGHT = 832;
const V2_SPRITE_SHEET_WIDTH = 2912;
const V2_SPRITE_SHEET_HEIGHT = 2496;

export interface AgentSpriteUrls {
  small: string;
  medium: string;
  large: string;
}

export interface AgentRegistryEntry {
  /** Human-readable label shown in the UI (e.g. select dropdowns, tables). */
  label: string;
  /** Fill colour used for placeholder agent rendering. */
  colorFill: string;
  /** Short letter label rendered inside the placeholder rectangle. */
  colorLabel: string;
  /**
   * Sprite sheet URLs per model tier.  Null for agent types without sprites
   * (e.g. API-only agents added in the future).
   */
  spriteUrls: AgentSpriteUrls | null;
}

/**
 * Canonical per-agent-type descriptor registry.
 *
 * The AgentType union (in types.ts) is derived via `keyof typeof AGENT_REGISTRY`
 * so it always matches exactly what is defined here.
 */
export const AGENT_REGISTRY = {
  claude_code: {
    label: "Claude Code",
    colorFill: "#E07B39",
    colorLabel: "C",
    spriteUrls: {
      small: claudeSmallSpriteUrl,
      medium: claudeMediumSpriteUrl,
      large: claudeLargeSpriteUrl,
    },
  },
  codex: {
    label: "Codex CLI",
    colorFill: "#F4D44D",
    colorLabel: "X",
    spriteUrls: {
      small: codexSmallSpriteUrl,
      medium: codexMediumSpriteUrl,
      large: codexLargeSpriteUrl,
    },
  },
  gemini: {
    label: "Gemini CLI",
    colorFill: "#4285F4",
    colorLabel: "G",
    spriteUrls: {
      small: geminiSmallSpriteUrl,
      medium: geminiMediumSpriteUrl,
      large: geminiLargeSpriteUrl,
    },
  },
  grok: {
    label: "Grok CLI",
    colorFill: "#14B8A6",
    colorLabel: "K",
    spriteUrls: {
      small: grokSmallSpriteUrl,
      medium: grokMediumSpriteUrl,
      large: grokLargeSpriteUrl,
    },
  },
  antigravity: {
    label: "Antigravity",
    colorFill: "#9334E6",
    colorLabel: "A",
    spriteUrls: {
      small: geminiSmallSpriteUrl,
      medium: geminiMediumSpriteUrl,
      large: geminiLargeSpriteUrl,
    },
  },
} as const satisfies Record<string, AgentRegistryEntry>;

/** The canonical AgentType union, always in sync with AGENT_REGISTRY. */
export type AgentType = keyof typeof AGENT_REGISTRY;

/** Sorted list of all AgentType values (useful for select options, etc.). */
export const AGENT_TYPES = Object.keys(AGENT_REGISTRY) as AgentType[];

/** Agent label map derived from the registry — no second definition needed. */
export function agentLabel(agentType: string): string {
  return (AGENT_REGISTRY as Record<string, AgentRegistryEntry | undefined>)[agentType]?.label
    ?? agentType;
}

/** Sprite spec helper shape (mirrors AgentSpriteSpec in sprites.ts). */
export interface AgentSpriteSpecFromRegistry {
  key: string;
  url: string;
  frameWidth: number;
  frameHeight: number;
  sheetWidth: number;
  sheetHeight: number;
}

function v2SpriteSpec(key: string, url: string): AgentSpriteSpecFromRegistry {
  return {
    key,
    url,
    frameWidth: V2_SPRITE_FRAME_WIDTH,
    frameHeight: V2_SPRITE_FRAME_HEIGHT,
    sheetWidth: V2_SPRITE_SHEET_WIDTH,
    sheetHeight: V2_SPRITE_SHEET_HEIGHT,
  };
}

/**
 * Build the per-tier sprite spec map for a given agent type, suitable for
 * use in the canvas renderer.  Returns null when the agent has no sprites.
 */
export function agentSpriteSpecsForType(
  agentType: string,
): Record<AgentModelTier, AgentSpriteSpecFromRegistry> | null {
  const entry = (AGENT_REGISTRY as Record<string, AgentRegistryEntry | undefined>)[agentType];
  if (!entry?.spriteUrls) return null;
  const urls = entry.spriteUrls;
  return {
    small: v2SpriteSpec(`${agentType}-small`, urls.small),
    medium: v2SpriteSpec(`${agentType}-medium`, urls.medium),
    large: v2SpriteSpec(`${agentType}-large`, urls.large),
  };
}
