import { describe, expect, it } from "vitest";
import { Camera } from "../src/engine/camera";
import { AXON_VERTICAL_SCALE, TILE_SIZE } from "../src/office/layout";
import {
  agentHeightUnitsForTier,
  agentSpriteSpecFor,
  agentVisibleFrameRatio,
  agentVisibleHeight,
  agentVisualSize,
  characterScreenBounds,
} from "../src/characters/sprites";
import {
  type Character,
  CharacterState,
  Direction,
  type AgentModelTier,
} from "../src/characters/types";

function makeCharacter(modelTier: AgentModelTier): Character {
  return {
    agentId: `agent-${modelTier}`,
    agentType: "codex",
    modelTier,
    state: CharacterState.IDLE,
    direction: Direction.DOWN,
    x: TILE_SIZE * 10,
    y: TILE_SIZE * 10,
    path: [],
    pathIndex: 0,
    targetState: CharacterState.IDLE,
    targetDirection: null,
    animFrame: 0,
    animTimer: 0,
    wanderTimer: 0,
    reservedSeatKey: null,
    activePlayId: null,
    activePlayType: null,
    status: "idle",
    bubble: null,
    bubbleUntil: null,
    opacity: 1,
    despawning: false,
    despawnOnArrival: false,
  };
}

describe("agent character sprites", () => {
  it("maps model tiers to the accepted v2 sprite-designed scale", () => {
    expect(agentHeightUnitsForTier("small")).toBeCloseTo((6 * 275) / 570, 6);
    expect(agentHeightUnitsForTier("medium")).toBe(6);
    expect(agentHeightUnitsForTier("large")).toBeCloseTo((6 * 741) / 570, 6);
    expect(agentHeightUnitsForTier(null)).toBe(6);
  });

  it("renders model tiers at their sprite-designed tile-math heights", () => {
    const zoom = 2;
    const pixelsPerFoot = TILE_SIZE * AXON_VERTICAL_SCALE * zoom;

    expect(agentVisibleHeight(zoom, 1, "small")).toBeCloseTo(
      agentHeightUnitsForTier("small") * pixelsPerFoot,
      6,
    );
    expect(agentVisibleHeight(zoom, 1, "medium")).toBeCloseTo(
      6 * pixelsPerFoot,
      6,
    );
    expect(agentVisibleHeight(zoom, 1, "large")).toBeCloseTo(
      agentHeightUnitsForTier("large") * pixelsPerFoot,
      6,
    );
  });

  it("scales all transparent v2 frames to the medium sprite baseline", () => {
    const zoom = 2;
    const pixelsPerFoot = TILE_SIZE * AXON_VERTICAL_SCALE * zoom;
    const mediumFrameHeight = agentVisualSize(
      zoom,
      1,
      "medium",
      "codex",
    ).height;

    for (const tier of ["small", "medium", "large"] as const) {
      const frameHeight = agentVisualSize(zoom, 1, tier, "codex").height;
      const visibleHeight = frameHeight * agentVisibleFrameRatio(tier, "codex");
      expect(frameHeight).toBeCloseTo(mediumFrameHeight, 6);
      expect(visibleHeight).toBeCloseTo(
        agentHeightUnitsForTier(tier) * pixelsPerFoot,
        6,
      );
    }
  });

  it("uses sprite rendering for Grok agents", () => {
    expect(agentSpriteSpecFor("grok", "small")).toMatchObject({
      key: "grok-small-ball",
    });
    expect(agentSpriteSpecFor("grok", "small")?.url).toContain(
      "grok-small-ball",
    );
    expect(agentSpriteSpecFor("grok", "medium")).toMatchObject({
      key: "grok-medium-humanoid",
    });
    expect(agentSpriteSpecFor("grok", "medium")?.url).toContain(
      "grok-medium-humanoid",
    );
    expect(agentSpriteSpecFor("grok", "large")).toMatchObject({
      key: "grok-large-humanoid",
    });
    expect(agentSpriteSpecFor("grok", "large")?.url).toContain(
      "grok-large-humanoid",
    );
  });

  it("uses the character model tier when deriving hit bounds", () => {
    const zoom = 1.5;
    const camera = new Camera();
    camera.zoom = zoom;

    const smallBounds = characterScreenBounds(
      makeCharacter("small"),
      zoom,
      camera,
    );
    const mediumBounds = characterScreenBounds(
      makeCharacter("medium"),
      zoom,
      camera,
    );
    const largeBounds = characterScreenBounds(
      makeCharacter("large"),
      zoom,
      camera,
    );
    const smallVisibleHeight =
      smallBounds.height * agentVisibleFrameRatio("small", "codex");
    const mediumVisibleHeight =
      mediumBounds.height * agentVisibleFrameRatio("medium", "codex");
    const largeVisibleHeight =
      largeBounds.height * agentVisibleFrameRatio("large", "codex");

    expect(smallVisibleHeight).toBeCloseTo(
      mediumVisibleHeight * (275 / 570),
      6,
    );
    expect(largeVisibleHeight).toBeCloseTo(
      mediumVisibleHeight * (741 / 570),
      6,
    );
  });
});
